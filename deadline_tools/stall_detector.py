"""Stall Detector — core logic for deadline-stall-detector.

Polls Deadline Web Service, detects stalled render jobs,
automatically requeues tasks and blacklists bad workers.

Usage:
    python -m deadline_tools.stall_detector --config config.yaml
"""
from __future__ import annotations

import argparse
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import yaml

from .connection import get_connection, ping_webservice

log = logging.getLogger(__name__)


@dataclass
class JobSnapshot:
    """Lightweight snapshot of a rendering job at a single poll."""
    job_id: str
    name: str
    status: str
    completed_chunks: int
    total_chunks: int
    worker: Optional[str] = None
    captured_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StallRecord:
    """Tracks how many consecutive polls a job has shown no progress."""
    job_id: str
    stall_count: int = 0
    last_completed: int = 0
    last_seen: datetime = field(default_factory=datetime.utcnow)


class StallDetector:
    """
    Monitors active Deadline jobs and reacts to stalls.

    Escalation ladder:
        1. WARN      — stall detected, logged
        2. REQUEUE   — task requeued to different worker
        3. BLACKLIST — worker removed from pools, job suspended
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.det = config["detector"]
        self.con = None
        self._stall_records: Dict[str, StallRecord] = {}
        self._worker_strikes: Dict[str, int] = defaultdict(int)
        self._blacklisted: set = set()

    def run(self):
        """Main loop — polls indefinitely until KeyboardInterrupt."""
        self.con = get_connection()
        if not ping_webservice(self.con):
            raise ConnectionError("Deadline Web Service is not reachable.")

        log.info("Stall detector started. Poll interval: %ds", self.det["poll_interval"])
        try:
            while True:
                self._tick()
                time.sleep(self.det["poll_interval"])
        except KeyboardInterrupt:
            log.info("Stall detector stopped.")

    def run_once(self) -> List[str]:
        """Single poll — useful for testing. Returns list of stalled job IDs."""
        if self.con is None:
            self.con = get_connection()
        return self._tick()

    def _tick(self) -> List[str]:
        snapshots = self._fetch_active_jobs()
        stalled_ids = []

        for snap in snapshots:
            record = self._stall_records.setdefault(
                snap.job_id,
                StallRecord(job_id=snap.job_id, last_completed=snap.completed_chunks)
            )

            if snap.completed_chunks > record.last_completed:
                record.stall_count = 0
                record.last_completed = snap.completed_chunks
                log.debug("Job %s (%s): progress %d/%d",
                          snap.job_id, snap.name,
                          snap.completed_chunks, snap.total_chunks)
            else:
                record.stall_count += 1
                record.last_seen = datetime.utcnow()
                log.warning(
                    "Job %s (%s): no progress for %d poll(s). Worker: %s",
                    snap.job_id, snap.name, record.stall_count, snap.worker
                )
                if record.stall_count >= self.det["stall_threshold_polls"]:
                    stalled_ids.append(snap.job_id)
                    self._escalate(snap, record)

        active_ids = {s.job_id for s in snapshots}
        for jid in [j for j in self._stall_records if j not in active_ids]:
            del self._stall_records[jid]

        return stalled_ids

    def _fetch_active_jobs(self) -> List[JobSnapshot]:
        watch = self.det.get("watch_statuses", ["Rendering"])
        try:
            jobs = self.con.Jobs.GetJobs()
        except Exception as exc:
            log.error("Failed to fetch jobs: %s", exc)
            return []

        snapshots = []
        for job in jobs:
            status = job.get("Props", {}).get("Stat", "")
            if status not in watch:
                continue
            job_id = job.get("_id", "")
            snapshots.append(JobSnapshot(
                job_id=job_id,
                name=job.get("Props", {}).get("Name", job_id),
                status=status,
                completed_chunks=job.get("Props", {}).get("Comp", 0),
                total_chunks=job.get("Props", {}).get("Tasks", 1),
                worker=self._get_active_worker(job_id),
            ))
        return snapshots

    def _get_active_worker(self, job_id: str) -> Optional[str]:
        try:
            for task in self.con.Tasks.GetJobTasks(job_id):
                if task.get("Stat", "") == "Rendering":
                    return task.get("SlaveRend", None)
        except Exception:
            pass
        return None

    def _escalate(self, snap: JobSnapshot, record: StallRecord):
        strikes = record.stall_count - self.det["stall_threshold_polls"]
        if strikes == 0:
            log.warning("[TIER-1] Requeuing stalled task for job %s", snap.job_id)
            self._requeue_task(snap)
        elif strikes == 1 and snap.worker:
            log.error("[TIER-2] Blacklisting worker %s (job %s)", snap.worker, snap.job_id)
            self._blacklist_worker(snap.worker)
        elif strikes >= 2:
            log.critical("[TIER-3] Suspending job %s — manual intervention required.", snap.job_id)
            self._suspend_job(snap.job_id)

    def _requeue_task(self, snap: JobSnapshot):
        try:
            self.con.Jobs.RequeueJob(snap.job_id)
            log.info("Requeued job %s", snap.job_id)
        except Exception as exc:
            log.error("Failed to requeue job %s: %s", snap.job_id, exc)

    def _blacklist_worker(self, worker: str):
        if worker in self._blacklisted:
            return
        pools = self.det.get("blacklist_pools", [])
        try:
            info = self.con.Slaves.GetSlaveInfo(worker)
            current = info.get("Props", {}).get("Pools", [])
            self.con.Slaves.SaveSlaveInfo(worker, {"Props": {"Pools": [p for p in current if p not in pools]}})
            self._blacklisted.add(worker)
            log.warning("Worker %s removed from pools %s", worker, pools)
        except Exception as exc:
            log.error("Failed to blacklist worker %s: %s", worker, exc)

    def _suspend_job(self, job_id: str):
        try:
            self.con.Jobs.SuspendJob(job_id)
            log.critical("Job %s suspended. Awaiting manual review.", job_id)
        except Exception as exc:
            log.error("Failed to suspend job %s: %s", job_id, exc)


def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO"))
    log_file = cfg.get("logging", {}).get("file", "")
    handlers = [logging.StreamHandler()]
    if log_file:
        import os
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main():
    parser = argparse.ArgumentParser(description="Deadline Stall Detector")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    cfg = _load_config(args.config)
    _setup_logging(cfg)
    StallDetector(cfg).run()


if __name__ == "__main__":
    main()
