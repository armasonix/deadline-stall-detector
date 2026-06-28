"""Integration tests - full stall detection and recovery cycle.

No live Deadline connection required. Uses conftest fixtures with in-memory fakes.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from unittest.mock import MagicMock

from deadline_tools.recovery import handle_stall


UTC = timezone.utc


def test_stall_detected_and_requeued(detector_with_stale_baseline, fake_con):
    """Full cycle: detector finds stall, recovery tier 1 requeues the job."""
    notifier = MagicMock()

    stalled = detector_with_stale_baseline.check()

    assert len(stalled) == 1
    history = stalled[0]
    assert history.job_id == "job-001"
    assert history.stall_count == 1

    job_dict = fake_con.Jobs.GetJob("job-001")
    action = handle_stall(fake_con, history, job_dict, notifier)

    assert action == "requeued"
    fake_con.Jobs.RequeueJob.assert_called_once_with("job-001")
    notifier.warn.assert_called_once()


def test_second_stall_blacklists_worker(detector_with_stale_baseline, fake_con):
    """Two stalls in a row -> tier 2: worker blacklisted + requeue."""
    notifier = MagicMock()

    # First stall
    stalled = detector_with_stale_baseline.check()
    assert stalled[0].stall_count == 1

    # Plant a known worker into failed_workers so tier 2 has something to blacklist
    stalled[0].last_snapshot.worker = "render-node-01"
    stalled[0].failed_workers = ["render-node-01"]

    # Roll back snapshot timestamp to simulate another 25-minute gap
    snap = detector_with_stale_baseline._snapshots["job-001"]
    snap.timestamp = snap.timestamp - timedelta(minutes=25)

    # Second stall
    stalled2 = detector_with_stale_baseline.check()
    assert len(stalled2) == 1
    assert stalled2[0].stall_count == 2

    job_dict = {"_id": "job-001", "Props": {"Name": "test_job"},
                "MachineName": "render-node-01"}
    action = handle_stall(fake_con, stalled2[0], job_dict, notifier)

    assert action == "requeued+blacklisted"
    fake_con.Jobs.SetJobMachineBlacklist.assert_called_once()
    fake_con.Jobs.RequeueJob.assert_called()
