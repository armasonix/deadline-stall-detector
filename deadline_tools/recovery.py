"""Three-tier recovery logic for stalled Deadline jobs."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deadline_tools.stall_detector import StallHistory
    from deadline_tools.notifier import TelegramNotifier

log = logging.getLogger(__name__)


def handle_stall(
    con,
    history: "StallHistory",
    job_dict: dict,
    notifier: "TelegramNotifier | None" = None,
) -> str:
    """Escalate based on stall_count. Returns action string."""
    job_id: str = history.job_id
    job_name: str = job_dict.get("Props", {}).get("Name", job_id)
    # Worker comes from the live job_dict, not from StallHistory
    # (StallHistory.last_snapshot.worker can be None when API returns empty)
    worker: str | None = (
        job_dict.get("MachineName")
        or (history.last_snapshot.worker if history.last_snapshot else None)
    )
    count: int = history.stall_count

    if count >= 3:
        log.error("SUSPEND job=%s name=%s stall_count=%d", job_id, job_name, count)
        con.Jobs.SuspendJob(job_id)
        if notifier:
            notifier.critical(
                f"🚨 SCENE ISSUE: *{job_name}* — suspended after {count} stalls. "
                "Manual review needed."
            )
        return "suspended"

    if count == 2:
        log.warning(
            "BLACKLIST+REQUEUE job=%s name=%s worker=%s", job_id, job_name, worker
        )
        if worker:
            # Get current blacklist, append, set back
            try:
                machine_limit = con.Jobs.GetJobMachineLimit(job_id)
                existing: list[str] = list(machine_limit.get("SlaveList", []) or [])
            except Exception:
                existing = []
            if worker not in existing:
                existing.append(worker)
            # limit=0 = no limit, whiteListFlag=False = blacklist
            con.Jobs.SetJobMachineLimit(job_id, 0, existing, False)
        con.Jobs.RequeueJob(job_id)
        if notifier:
            notifier.warn(
                f"⚠️⚠️ STALLED AGAIN: *{job_name}* — blacklisting `{worker}` + requeue"
            )
        return "requeued+blacklisted"

    # count == 1
    log.warning("REQUEUE job=%s name=%s", job_id, job_name)
    con.Jobs.RequeueJob(job_id)
    if notifier:
        notifier.warn(f"⚠️ STALLED: *{job_name}* — requeue attempt {count}")
    return "requeued"
