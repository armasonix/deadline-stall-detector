"""Unit tests for StallDetector.check() - without a dependency on Deadline."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from deadline_tools.stall_detector import JobSnapshot, StallDetector, StallHistory


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_job(job_id="job-001", name="test_job", progress=50.0,
              output_dir="", worker="render-node-01"):
    """A mock dictionary in the Deadline Jobs API format."""
    return {
        "_id": job_id,
        "Props": {
            "Stat": 3,   # Rendering
            "Name": name,
            "Comp": int(progress),
            "Tasks": 100,
            "OutDir": [output_dir] if output_dir else [],
        },
        "MachineName": worker,
    }


def _make_con(jobs=None, tasks=None):
    """Мок DeadlineCon."""
    con = MagicMock()
    con.Jobs.GetJobs.return_value = jobs or []
    con.Tasks.GetJobTasks.return_value = tasks or []
    return con


# ── tests ────────────────────────────────────────────────────────────────────

def test_first_run_no_stall():
    """First launch - no history, nothing to compare against -> empty list."""
    con = _make_con(jobs=[_make_job()])
    detector = StallDetector(con=con, stall_threshold_min=20)

    result = detector.check()

    assert result == [], "The first launch should not result in a hang."
    assert "job-001" in detector._snapshots


def test_stalled_no_progress_no_files():
    """Snapshot 25 minutes ago, progress unchanged, no files -> stall."""
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir="")])
    detector = StallDetector(con=con, stall_threshold_min=20)

    # Set a baseline with a timestamp from 25 minutes ago.
    old_time = datetime.utcnow() - timedelta(minutes=25)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001",
        name="test_job",
        progress=50.0,       # Progress has not changed.
        output_dir="",       # If output_dir is missing, _new_files_exist will return True.
        worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    # output_dir is empty -> _new_files_exist returns True (unavailable),
    # meaning the freeze won't be recorded. We use a non-existent folder.
    con2 = _make_con(jobs=[_make_job(progress=50.0, output_dir="C:/nonexistent_12345")])
    detector.con = con2

    result = detector.check()

    assert len(result) == 1
    assert result[0].job_id == "job-001"
    assert result[0].stall_count == 1


def test_not_stalled_progress_moved():
    """Snapshot 25 minutes ago, +15% progress -> not hung."""
    con = _make_con(jobs=[_make_job(progress=65.0)])
    detector = StallDetector(con=con, stall_threshold_min=20)

    old_time = datetime.utcnow() - timedelta(minutes=25)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001",
        name="test_job",
        progress=50.0,       # It was 50%
        output_dir="",
        worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    result = detector.check()

    assert result == [], "Progress is moving forward - no freezing."


def test_stall_count_increments():
    """Two consecutive stalls -> stall_count = 2."""
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir="C:/nonexistent_12345")])
    detector = StallDetector(con=con, stall_threshold_min=20)

    old_time = datetime.utcnow() - timedelta(minutes=25)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001", name="test_job", progress=50.0,
        output_dir="C:/nonexistent_12345", worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001", stall_count=1)

    result = detector.check()

    assert len(result) == 1
    assert result[0].stall_count == 2


def test_threshold_not_reached_yet():
    """Only 5 minutes have passed with threshold=20 -> skipping the check."""
    con = _make_con(jobs=[_make_job(progress=50.0)])
    detector = StallDetector(con=con, stall_threshold_min=20)

    recent_time = datetime.utcnow() - timedelta(minutes=5)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001", name="test_job", progress=50.0,
        output_dir="", worker="render-node-01",
        timestamp=recent_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    result = detector.check()

    assert result == [], "Threshold not reached - hang should not be logged."
