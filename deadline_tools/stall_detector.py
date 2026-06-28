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
    timestamp: datetime = field(default_factory=_now)


@dataclass
class StallHistory:
    """Per-job stall history. stall_count drives the escalation tier."""
    job_id: str
    stall_count: int = 0
    failed_workers: List[str] = field(default_factory=list)
    last_snapshot: Optional[JobSnapshot] = None


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

            progress_moved = snap.progress > prev.progress
            new_files = self._new_files_exist(snap.output_dir, prev.timestamp)

            if progress_moved or new_files:
                # Progress detected - reset counter, update snapshot
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

    def _fetch_rendering_jobs(self) -> List[JobSnapshot]:
        """Fetch jobs with Rendering status from Deadline (Stat=3 in API v10)."""
        try:
            jobs = self.con.Jobs.GetJobs()
        except Exception as exc:
            log.error("Failed to fetch jobs from Deadline: %s", exc)
            return []

        result = []
        for job in jobs:
            props = job.get("Props", {})
            if props.get("Stat", -1) != 3:
                continue

            job_id = job.get("_id", "")
            output_dirs = props.get("OutDir", [])
            output_dir = output_dirs[0] if output_dirs else ""

            completed = props.get("Comp", 0)
            total = max(props.get("Tasks", 1), 1)
            progress = round(completed / total * 100, 2)

            worker = self._get_active_worker(job_id)

            result.append(JobSnapshot(
                job_id=job_id,
                name=props.get("Name", job_id),
                progress=progress,
                output_dir=output_dir,
                worker=worker,
            ))

        return result

    def _get_active_worker(self, job_id: str) -> Optional[str]:
        """Return the name of the worker currently rendering a task for this job."""
        try:
            for task in self.con.Tasks.GetJobTasks(job_id):
                if task.get("Stat", "") == "Rendering":
                    return task.get("SlaveRend") or None
        except Exception:
            pass
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
