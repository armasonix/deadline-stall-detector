"""Unit tests for recovery.handle_stall() — без реального Deadline."""
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from deadline_tools.recovery import handle_stall
from deadline_tools.stall_detector import JobSnapshot, StallHistory
from datetime import datetime


def _make_history(job_id="job-001", stall_count=1, worker="render-node-01"):
    snap = JobSnapshot(
        job_id=job_id, name="test_job", progress=50.0,
        output_dir="", worker=worker, timestamp=datetime.utcnow()
    )
    h = StallHistory(job_id=job_id, stall_count=stall_count)
    h.last_snapshot = snap
    if worker:
        h.failed_workers = [worker]
    return h


def _make_job_dict(job_id="job-001", name="test_job"):
    return {"Props": {"Name": name}, "_id": job_id}


def _make_con():
    con = MagicMock()
    con.Jobs.GetJobMachineBlacklist.return_value = []
    return con


def _make_notifier():
    return MagicMock()


# ─────────────────────────────────────────────────────────────────────────────

def test_tier1_requeue():
    """stall_count=1 → RequeueJob вызван, warn отправлен."""
    con = _make_con()
    notifier = _make_notifier()
    history = _make_history(stall_count=1)

    action = handle_stall(con, history, _make_job_dict(), notifier)

    assert action == "requeued"
    con.Jobs.RequeueJob.assert_called_once_with("job-001")
    notifier.warn.assert_called_once()
    con.Jobs.SuspendJob.assert_not_called()


def test_tier2_blacklist_and_requeue():
    """stall_count=2 → SetJobMachineBlacklist + RequeueJob вызваны."""
    con = _make_con()
    notifier = _make_notifier()
    history = _make_history(stall_count=2, worker="render-node-01")

    action = handle_stall(con, history, _make_job_dict(), notifier)

    assert action == "requeued+blacklisted"
    con.Jobs.SetJobMachineBlacklist.assert_called_once_with("job-001", ["render-node-01"])
    con.Jobs.RequeueJob.assert_called_once_with("job-001")
    notifier.warn.assert_called_once()


def test_tier3_suspend():
    """stall_count=3 → SuspendJob вызван, critical отправлен."""
    con = _make_con()
    notifier = _make_notifier()
    history = _make_history(stall_count=3)

    action = handle_stall(con, history, _make_job_dict(), notifier)

    assert action == "suspended"
    con.Jobs.SuspendJob.assert_called_once_with("job-001")
    notifier.critical.assert_called_once()
    con.Jobs.RequeueJob.assert_not_called()


def test_tier2_no_worker_skips_blacklist():
    """stall_count=2, worker=None → blacklist не вызывается, requeue всё равно."""
    con = _make_con()
    notifier = _make_notifier()
    history = _make_history(stall_count=2, worker=None)

    action = handle_stall(con, history, _make_job_dict(), notifier)

    assert action == "requeued+blacklisted"
    con.Jobs.SetJobMachineBlacklist.assert_not_called()
    con.Jobs.RequeueJob.assert_called_once()
