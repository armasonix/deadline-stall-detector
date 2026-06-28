"""Recovery actions — три тира эскалации.

handle_stall(con, history, job, notifier) → action_string

  stall_count == 1  →  requeue + warn
  stall_count == 2  →  requeue + SetJobMachineBlacklist + warn
  stall_count >= 3  →  SuspendJob + critical
"""
from __future__ import annotations

import logging
from typing import Optional

from .stall_detector import StallHistory

log = logging.getLogger(__name__)


def handle_stall(con, history: StallHistory, job: dict, notifier) -> str:
    """
    Выполнить recovery-действие согласно тиру эскалации.
    Возвращает строку действия для логирования.
    """
    job_name = job.get("Props", {}).get("Name", history.job_id)
    worker: Optional[str] = history.last_snapshot.worker if history.last_snapshot else None

    if history.stall_count == 1:
        _requeue(con, history.job_id, job_name)
        notifier.warn(f"STALLED: {job_name} — requeue attempt 1")
        return "requeued"

    elif history.stall_count == 2:
        if worker:
            _blacklist_worker(con, history.job_id, worker, job_name)
        _requeue(con, history.job_id, job_name)
        notifier.warn(
            f"STALLED AGAIN: {job_name} — blacklisted {worker or 'unknown'}, requeue attempt 2"
        )
        return "requeued+blacklisted"

    else:  # stall_count >= 3
        _suspend(con, history.job_id, job_name)
        notifier.critical(
            f"SCENE ISSUE: {job_name} — suspended after {history.stall_count} workers. "
            f"Manual review needed."
        )
        return "suspended"


# ── private helpers ───────────────────────────────────────────────────────────

def _requeue(con, job_id: str, job_name: str):
    try:
        con.Jobs.RequeueJob(job_id)
        log.info("Requeued job %s (%s)", job_id, job_name)
    except Exception as exc:
        log.error("RequeueJob failed for %s: %s", job_id, exc)


def _blacklist_worker(con, job_id: str, worker: str, job_name: str):
    """Добавить воркера в per-job machine blacklist (не трогает глобальные пулы)."""
    try:
        current = con.Jobs.GetJobMachineBlacklist(job_id) or []
        if worker not in current:
            con.Jobs.SetJobMachineBlacklist(job_id, current + [worker])
            log.warning("Blacklisted worker %s for job %s (%s)", worker, job_id, job_name)
    except Exception as exc:
        log.error("SetJobMachineBlacklist failed for %s / %s: %s", job_id, worker, exc)


def _suspend(con, job_id: str, job_name: str):
    try:
        con.Jobs.SuspendJob(job_id)
        log.critical("Suspended job %s (%s)", job_id, job_name)
    except Exception as exc:
        log.error("SuspendJob failed for %s: %s", job_id, exc)
