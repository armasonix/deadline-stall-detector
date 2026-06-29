"""Stall Detector - JobSnapshot, StallHistory, StallDetector.check()

Dual stall signal:
  1. Job progress has not changed since the previous snapshot
  2. No new files written to output_dir within the stall_threshold_min window

Detection only - recovery actions live in recovery.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class JobSnapshot:
    """Single poll snapshot of a rendering job."""
    job_id: str
    name: str
    progress: float          # 0.0 - 100.0
    output_dir: str
    worker: Optional[str]
    is_rendering: bool = True  # at least one task is actively Rendering
    timestamp: datetime = field(default_factory=_now)


@dataclass
class StallHistory:
    """Per-job stall history. stall_count drives the escalation tier."""
    job_id: str
    stall_count: int = 0
    failed_workers: List[str] = field(default_factory=list)
    last_snapshot: Optional[JobSnapshot] = None
    current_worker_already_failed: bool = False


@dataclass
class StallDetector:
    """
    Stall detector. Does not call the Deadline API directly -
    accepts con from outside so it can be mocked in tests.
    """
    con: object
    stall_threshold_min: int = 20

    _snapshots: Dict[str, JobSnapshot] = field(default_factory=dict)
    _history: Dict[str, StallHistory] = field(default_factory=dict)

    def check(self) -> List[StallHistory]:
        """
        One check cycle. Returns a list of StallHistory entries for jobs
        where a stall was detected (stall_count incremented).
        """
        current_jobs = self._fetch_rendering_jobs()
        stalled: List[StallHistory] = []
        now = _now()

        for snap in current_jobs:
            # A job that is Active (Stat=1) but has no task in the Rendering
            # state is queued/pending - nobody is actually rendering it right
            # now (e.g. it was blacklisted off the only worker and is waiting
            # for a free machine). Such a job has no progress and writes no
            # files by definition, so it must NOT accrue stall counts.
            if not snap.is_rendering:
                # Keep its baseline fresh so it does not instantly trip a stall
                # the moment a worker finally picks it up again.
                self._snapshots[snap.job_id] = snap
                self._history.setdefault(snap.job_id, StallHistory(job_id=snap.job_id))
                log.debug("Job %s is queued/not-rendering - skipping stall check", snap.job_id)
                continue

            prev = self._snapshots.get(snap.job_id)

            if prev is None:
                # First time we see this job - capture baseline, do not detect
                self._snapshots[snap.job_id] = snap
                self._history.setdefault(snap.job_id, StallHistory(job_id=snap.job_id))
                log.debug("Baseline captured for job %s (%s)", snap.job_id, snap.name)
                continue

            elapsed = now - prev.timestamp
            if elapsed < timedelta(minutes=self.stall_threshold_min):
                # Not enough time has passed yet
                continue

            progress_moved = snap.progress != prev.progress
            new_files = self._new_files_exist(snap.output_dir, prev.timestamp)

            if progress_moved or new_files:
                # Progress changed or output appeared - reset counter, update snapshot
                history = self._history[snap.job_id]
                if history.stall_count > 0:
                    log.info("Job %s recovered (progress=%.1f%%)", snap.job_id, snap.progress)
                    history.stall_count = 0
                self._snapshots[snap.job_id] = snap
            else:
                # Both signals fire: no progress + no new files -> stall
                history = self._history[snap.job_id]
                history.stall_count += 1
                history.last_snapshot = snap
                history.current_worker_already_failed = (
                    bool(snap.worker) and snap.worker in history.failed_workers
                )

                if snap.worker and snap.worker not in history.failed_workers:
                    history.failed_workers.append(snap.worker)

                log.warning(
                    "STALL detected: job=%s name=%s stall_count=%d worker=%s",
                    snap.job_id, snap.name, history.stall_count, snap.worker
                )
                stalled.append(history)
                self._snapshots[snap.job_id] = snap

        # Clean up history for jobs that are no longer active
        active_ids = {s.job_id for s in current_jobs}
        for jid in list(self._snapshots.keys()):
            if jid not in active_ids:
                del self._snapshots[jid]
                self._history.pop(jid, None)

        return stalled

    # -- private --------------------------------------------------------------

    # Deadline 10 Job.Stat codes (top-level field, NOT inside Props):
    #   1 = Active   (queued / rendering / pending)
    #   2 = Suspended
    #   3 = Completed
    #   4 = Failed
    #   6 = Pending
    _ACTIVE_STAT = 1

    def _fetch_rendering_jobs(self) -> List[JobSnapshot]:
        """Fetch Active jobs (Stat=1) as candidates for stall checking."""
        try:
            jobs = self.con.Jobs.GetJobs()
        except Exception as exc:
            log.error("Failed to fetch jobs from Deadline: %s", exc)
            return []

        result = []
        for job in jobs:
            stat = job.get("Stat", job.get("Props", {}).get("Stat", -1))
            if stat != self._ACTIVE_STAT:
                continue

            props = job.get("Props", {}) or {}
            job_id = job.get("_id", "")

            output_dirs = job.get("OutDir") or props.get("OutDir") or []
            output_dir = output_dirs[0] if output_dirs else ""

            progress = self._job_progress(job, props)
            is_rendering = self._job_is_rendering(job)
            worker = self._job_worker(job, job_id)

            log.debug(
                "Job snapshot: id=%s name=%s stat=%s progress=%.1f%% out=%s "
                "worker=%s rendering=%s rchunks=%s",
                job_id, props.get("Name", job_id), stat, progress, output_dir,
                worker, is_rendering, job.get("RenderingChunks"),
            )

            result.append(JobSnapshot(
                job_id=job_id,
                name=props.get("Name", job_id),
                progress=progress,
                output_dir=output_dir,
                worker=worker,
                is_rendering=is_rendering,
            ))

        return result

    @staticmethod
    def _job_progress(job: dict, props: dict) -> float:
        """Progress %, preferring the authoritative chunk counters.

        Deadline tracks per-chunk state on the job. CompletedChunks/total is
        the real progress; fall back to Props.Comp only when chunk counters
        are unavailable.
        """
        comp = job.get("CompletedChunks")
        rendering = job.get("RenderingChunks", 0) or 0
        queued    = job.get("QueuedChunks", 0) or 0
        pending   = job.get("PendingChunks", 0) or 0
        suspended = job.get("SuspendedChunks", 0) or 0
        failed    = job.get("FailedChunks", 0) or 0

        if comp is not None:
            total_chunks = (comp + rendering + queued + pending
                            + suspended + failed)
            if total_chunks > 0:
                return round(comp / total_chunks * 100, 2)

        # Fallback: Props.Comp over Props.Tasks
        comp = comp if comp is not None else props.get("Comp", 0) or 0
        total = max(int(props.get("Tasks", 1) or 1), 1)
        return round(comp / total * 100, 2)

    @staticmethod
    def _job_is_rendering(job: dict) -> bool:
        """Authoritative 'is a worker actively rendering this job right now?'

        Per the Deadline REST docs: an Active job (Stat=1) is either idle
        (queued) or rendering; use RenderingChunks to tell them apart.
        RenderingChunks > 0 means at least one chunk is actively rendering.
        """
        try:
            return int(job.get("RenderingChunks", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _job_worker(job: dict, job_id: str):
        """Worker rendering this job, from the job dict (top-level 'Mach')."""
        return job.get("Mach") or job.get("MachineName") or None

    def _get_active_worker(self, job_id: str) -> Optional[str]:
        """Return the name of a worker assigned to a task of this job.

        This is a fallback used only when the job dict does not carry a worker
        name. The authoritative source is the job's own 'Mach' field
        (see _job_worker); task worker codes vary across Deadline versions, so
        we simply return the first task 'Slave' we find.
        """
        try:
            tasks = self.con.Tasks.GetJobTasks(job_id)
        except Exception as exc:
            log.debug("GetJobTasks(%s) failed: %s", job_id, exc)
            return None

        # tasks may be a list or {"Tasks": [...]} dict depending on API version
        if isinstance(tasks, dict):
            tasks = tasks.get("Tasks", []) or []

        for task in tasks:
            worker = (
                task.get("Slave")
                or task.get("SlaveRend")
                or task.get("Worker")
                or task.get("RendSlave")
                or task.get("Mach")
            )
            if worker:
                return worker
        return None

    def _new_files_exist(self, output_dir: str, since: datetime) -> bool:
        """
        Check whether any new files appeared in output_dir after 'since'.

        Returns False (stall signal active) when the directory exists but
        is empty or has no files newer than 'since'.

        Returns True (stall signal suppressed) only when the directory path
        is empty/unknown or is genuinely inaccessible due to a system error,
        so we do not false-positive on network share outages.
        """
        if not output_dir:
            # No output dir configured - suppress the file signal
            return True

        try:
            found_new = False
            with os.scandir(output_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                    if mtime > since:
                        found_new = True
                        break
            return found_new
        except (PermissionError, OSError):
            # Directory inaccessible (network share down etc.) - suppress signal
            log.debug("Output dir not accessible: %s", output_dir)
            return True
