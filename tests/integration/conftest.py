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
                  output_dir="", stat=3):
    return {
        "_id": job_id,
        "Props": {
            "Stat": stat,
            "Name": name,
            "Comp": progress,
            "Tasks": 100,
            "OutDir": [output_dir] if output_dir else [],
        },
    }


@pytest.fixture()
def fake_con():
    """Deadline connection mock with a single stalled rendering job."""
    con = MagicMock()
    con.Jobs.GetJobs.return_value = [_make_job_api()]
    con.Jobs.GetJob.return_value = _make_job_api()
    con.Tasks.GetJobTasks.return_value = []
    con.Jobs.GetJobMachineBlacklist.return_value = []
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
