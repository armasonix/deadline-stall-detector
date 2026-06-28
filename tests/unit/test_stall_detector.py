"""Unit tests for StallDetector.check() - no Deadline dependency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from deadline_tools.stall_detector import JobSnapshot, StallDetector, StallHistory

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _make_job(job_id="job-001", name="test_job", progress=50.0, output_dir="",
              worker="render-node-01"):
    """Fake job dict matching the Deadline Jobs API shape."""
    return {
        "_id": job_id,
        "Props": {
            "Stat": 3,
            "Name": name,
            "Comp": int(progress),
            "Tasks": 100,
            "OutDir": [output_dir] if output_dir else [],
        },
        "MachineName": worker,
    }


def _make_con(jobs=None):
    con = MagicMock()
    con.Jobs.GetJobs.return_value = jobs or []
    con.Tasks.GetJobTasks.return_value = []
    return con


def _set_baseline(detector, job_id="job-001", progress=50.0, output_dir="",
                  worker="render-node-01", minutes_ago=25):
    """Plant a snapshot and history entry as if check() ran N minutes ago."""
    old_time = _now() - timedelta(minutes=minutes_ago)
    detector._snapshots[job_id] = JobSnapshot(
        job_id=job_id,
        name="test_job",
        progress=progress,
        output_dir=output_dir,
        worker=worker,
        timestamp=old_time,
    )
    detector._history[job_id] = StallHistory(job_id=job_id)


# ---- tests ------------------------------------------------------------------

def test_first_run_no_stall():
    """First call - no history, nothing to compare -> empty result."""
    con = _make_con(jobs=[_make_job()])
    detector = StallDetector(con=con, stall_threshold_min=20)

    result = detector.check()

    assert result == []
    assert "job-001" in detector._snapshots


def test_stalled_no_progress_no_files(tmp_path):
    """Progress unchanged, output dir exists but empty -> stall detected."""
    empty_dir = str(tmp_path)  # exists, no files inside
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir=empty_dir)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=empty_dir)

    result = detector.check()

    assert len(result) == 1
    assert result[0].job_id == "job-001"
    assert result[0].stall_count == 1


def test_not_stalled_progress_moved():
    """Progress increased by 15% -> no stall."""
    con = _make_con(jobs=[_make_job(progress=65.0)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0)

    result = detector.check()

    assert result == []


def test_stall_count_increments(tmp_path):
    """Second consecutive stall -> stall_count becomes 2."""
    empty_dir = str(tmp_path)
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir=empty_dir)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=empty_dir)
    detector._history["job-001"].stall_count = 1

    result = detector.check()

    assert len(result) == 1
    assert result[0].stall_count == 2


def test_threshold_not_reached_yet():
    """Only 5 minutes elapsed with threshold=20 -> check skipped."""
    con = _make_con(jobs=[_make_job(progress=50.0)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, minutes_ago=5)

    result = detector.check()

    assert result == []


def test_not_stalled_new_file_written(tmp_path):
    """Progress frozen but a new file appeared in output_dir -> no stall."""
    output_dir = str(tmp_path)
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir=output_dir)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=output_dir)

    # Write a file after the baseline timestamp
    (tmp_path / "frame_0001.exr").write_bytes(b"fake")

    result = detector.check()

    assert result == []


def test_stall_suppressed_when_output_dir_missing():
    """output_dir does not exist -> file signal suppressed -> no stall."""
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir="C:/does_not_exist_xyz")])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir="C:/does_not_exist_xyz")

    result = detector.check()

    assert result == []
