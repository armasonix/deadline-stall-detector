"""Stall Detector - JobSnapshot, StallHistory, StallDetector.check()

Double hang signal:
  1. The job's progress has not changed since the last snapshot.
  2. No new files in output_dir within the stall_threshold_min period.

Detection only - recovery logic has been moved to recovery.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class JobSnapshot:
    """A snapshot of a rendering job at the moment of polling."""
    job_id: str
    name: str
    progress: float          # 0.0 – 100.0
    output_dir: str
    worker: Optional[str]
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StallHistory:
    """The story of a single job's hangs. `stall_count` determines the escalation tier."""
    job_id: str
    stall_count: int = 0
    failed_workers: List[str] = field(default_factory=list)
    last_snapshot: Optional[JobSnapshot] = None


@dataclass
class StallDetector:
    """
    Hang detector. Does not call the Deadline API directly -
    It accepts `con` as an external dependency so that it can be mocked in tests.
    """
    con: object
    stall_threshold_min: int = 20

    _snapshots: Dict[str, JobSnapshot] = field(default_factory=dict)
    _history: Dict[str, StallHistory] = field(default_factory=dict)

    def check(self) -> List[StallHistory]:
        """
        One check loop. Returns a list of StallHistory jobs,
        for which a stall has been recorded (stall_count increased).
        """
        current_jobs = self._fetch_rendering_jobs()
        stalled: List[StallHistory] = []
        now = datetime.utcnow()

        for snap in current_jobs:
            prev = self._snapshots.get(snap.job_id)

            if prev is None:
                # For the first time seeing job, record a baseline and do not trigger a detection.
                self._snapshots[snap.job_id] = snap
                self._history.setdefault(snap.job_id, StallHistory(job_id=snap.job_id))
                log.debug("Baseline captured for job %s (%s)", snap.job_id, snap.name)
                continue

            elapsed = now - prev.timestamp
            if elapsed < timedelta(minutes=self.stall_threshold_min):
                # Not enough time has passed yet - skipping.
                continue

            progress_moved = snap.progress > prev.progress
            new_files = self._new_files_exist(snap.output_dir, prev.timestamp)

            if progress_moved or new_files:
                # Progress made - resetting the counter, updating the snapshot.
                history = self._history[snap.job_id]
                if history.stall_count > 0:
                    log.info("Job %s recovered (progress=%.1f%%)", snap.job_id, snap.progress)
                    history.stall_count = 0
                self._snapshots[snap.job_id] = snap
            else:
                # Both signals-no progress + no files-result in a stall.
                history = self._history[snap.job_id]
                history.stall_count += 1
                history.last_snapshot = snap

                # Store the worker if it is new.
                if snap.worker and snap.worker not in history.failed_workers:
                    history.failed_workers.append(snap.worker)

                log.warning(
                    "STALL detected: job=%s name=%s stall_count=%d worker=%s",
                    snap.job_id, snap.name, history.stall_count, snap.worker
                )
                stalled.append(history)
                # Updating the snapshot to avoid recalculating for the same period.
                self._snapshots[snap.job_id] = snap

        # Clearing the history of completed jobs
        active_ids = {s.job_id for s in current_jobs}
        for jid in list(self._snapshots.keys()):
            if jid not in active_ids:
                del self._snapshots[jid]
                self._history.pop(jid, None)

        return stalled

    # ── private ──────────────────────────────────────────────────────────────

    def _fetch_rendering_jobs(self) -> List[JobSnapshot]:
        """Get a list of jobs with "Rendering" status from Deadline."""
        try:
            jobs = self.con.Jobs.GetJobs()
        except Exception as exc:
            log.error("Failed to fetch jobs from Deadline: %s", exc)
            return []

        result = []
        for job in jobs:
            props = job.get("Props", {})
            # Stat=3 -> Rendering в Deadline 10.x
            if props.get("Stat", -1) != 3:
                continue

            job_id = job.get("_id", "")
            output_dirs = props.get("OutDir", [])
            output_dir = output_dirs[0] if output_dirs else ""

            # Progress: completed tasks / total tasks * 100
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
        """Return the name of the worker currently rendering the job task."""
        try:
            for task in self.con.Tasks.GetJobTasks(job_id):
                if task.get("Stat", "") == "Rendering":
                    return task.get("SlaveRend") or None
        except Exception:
            pass
        return None

    def _new_files_exist(self, output_dir: str, since: datetime) -> bool:
        """
        Check whether new files have appeared in output_dir since the specified time.
        Returns True if the directory is inaccessible (this is not considered a hang).
        """
        if not output_dir:
            return True  # no output_dir -> do not block detection

        try:
            with os.scandir(output_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
                    if mtime > since:
                        return True
            return False
        except (FileNotFoundError, PermissionError, OSError):
            # Directory inaccessible - assume files exist; do not block.
            log.debug("Output dir not accessible: %s", output_dir)
            return True
