"""Integration tests — full stall/recovery cycle, no live Deadline needed."""
from __future__ import annotations

from datetime import timedelta, timezone
from unittest.mock import MagicMock

from deadline_tools.recovery import handle_stall

UTC = timezone.utc


def test_stall_detected_and_requeued(detector_with_stale_baseline, fake_con):
    notifier = MagicMock()

    stalled = detector_with_stale_baseline.check()

    assert len(stalled) == 1
    assert stalled[0].job_id == "job-001"
    assert stalled[0].stall_count == 1

    job_dict = {"_id": "job-001", "Props": {"Name": "test_job"}, "MachineName": None}
    action = handle_stall(fake_con, stalled[0], job_dict, notifier)

    assert action == "requeued"
    fake_con.Jobs.RequeueJob.assert_called_once_with("job-001")
    notifier.warn.assert_called_once()


def test_second_stall_blacklists_worker(detector_with_stale_baseline, fake_con):
    """Two stalls -> tier 2: worker blacklisted + requeue."""
    notifier = MagicMock()

    # First stall
    stalled = detector_with_stale_baseline.check()
    assert stalled[0].stall_count == 1

    # Simulate 25-min gap
    snap = detector_with_stale_baseline._snapshots["job-001"]
    snap.timestamp = snap.timestamp - timedelta(minutes=25)

    # Second stall
    stalled2 = detector_with_stale_baseline.check()
    assert stalled2[0].stall_count == 2

    # job_dict carries the worker — as it would from the real API
    job_dict = {
        "_id": "job-001",
        "Props": {"Name": "test_job"},
        "MachineName": "render-node-01",
    }
    fake_con.Jobs.GetJobMachineLimit.return_value = {"SlaveList": []}

    action = handle_stall(fake_con, stalled2[0], job_dict, notifier)

    assert action == "requeued+blacklisted"
    # limit=0 (no cap), blacklist the worker, whitelistFlag=False
    fake_con.Jobs.SetJobMachineLimit.assert_called_once_with(
        "job-001", 0, ["render-node-01"], False
    )
    fake_con.Jobs.RequeueJob.assert_called()
