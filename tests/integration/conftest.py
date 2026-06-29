"""Shared fixtures for integration tests.

All fixtures use in-memory fakes - no live Deadline connection needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from deadline_tools.stall_detector import JobSnapshot, StallDetector, StallHistory

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _make_job_api(job_id="job-001", name="test_job", progress=50,
                  output_dir="", stat=1, worker="render-node-01",
                  rendering=True):
    # Stat is a TOP-LEVEL field; Stat=1 == Active. RenderingChunks > 0 marks
    # the job as actively rendering (vs merely queued). Worker is in 'Mach'.
    total = 100
    remaining = max(total - progress, 0)
    return {
        "_id": job_id,
        "Stat": stat,
        "Mach": worker,
        "CompletedChunks": progress,
        "RenderingChunks": 1 if rendering else 0,
        "QueuedChunks": (remaining - 1 if rendering else remaining),
        "PendingChunks": 0,
        "SuspendedChunks": 0,
        "FailedChunks": 0,
        "Props": {
            "Name": name,
            "Comp": progress,
            "Tasks": total,
            "OutDir": [output_dir] if output_dir else [],
        },
    }


@pytest.fixture()
def fake_con():
    """Deadline connection mock with a single rendering job."""
    con = MagicMock()
    con.Jobs.GetJobs.return_value = [_make_job_api()]
    con.Jobs.GetJob.return_value = _make_job_api()
    con.Tasks.GetJobTasks.return_value = [{"Slave": "render-node-01"}]
    con.Jobs.GetJobMachineLimit.return_value = {"SlaveList": []}
    return con


@pytest.fixture()
def detector_with_stale_baseline(fake_con, tmp_path):
    """StallDetector pre-loaded with a 25-minute-old baseline (empty output dir)."""
    detector = StallDetector(con=fake_con, stall_threshold_min=20)
    old_time = _now() - timedelta(minutes=25)
    output_dir = str(tmp_path)

    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001",
        name="test_job",
        progress=50.0,
        output_dir=output_dir,
        worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    # Make the fake con return matching output_dir
    fake_con.Jobs.GetJobs.return_value = [
        _make_job_api(output_dir=output_dir)
    ]
    return detector
