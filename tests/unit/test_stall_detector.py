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
              worker="render-node-01", rendering=True):
    """Fake job dict matching the real Deadline Jobs API shape.

    Stat is a TOP-LEVEL field. Stat=1 == Active (the only state the detector
    considers). Whether a worker is actively rendering is determined by the
    chunk counters: RenderingChunks > 0 means rendering, == 0 means queued.
    The worker name lives in the top-level 'Mach' field.
    """
    total = 100
    comp = int(progress)
    remaining = max(total - comp, 0)
    return {
        "_id": job_id,
        "Stat": 1,
        "Mach": worker,
        "CompletedChunks": comp,
        "RenderingChunks": 1 if rendering else 0,
        "QueuedChunks": (remaining - 1 if rendering else remaining),
        "PendingChunks": 0,
        "SuspendedChunks": 0,
        "FailedChunks": 0,
        "Props": {
            "Name": name,
            "Comp": comp,
            "Tasks": total,
            "OutDir": [output_dir] if output_dir else [],
        },
    }


def _make_con(jobs=None, tasks=None):
    con = MagicMock()
    con.Jobs.GetJobs.return_value = jobs or []
    con.Tasks.GetJobTasks.return_value = (
        tasks if tasks is not None else [{"Slave": "render-node-01"}]
    )
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


def test_rendering_chunks_marks_job_as_rendering(tmp_path):
    """Regression: a job with RenderingChunks=1 and a worker in 'Mach' is
    actively rendering (NOT queued) and must be eligible to stall.
    Mirrors the real Deadline payload (Mach set, MachineName absent)."""
    empty_dir = str(tmp_path)
    job = {
        "_id": "6a428000",
        "Stat": 1,
        "Mach": "DESKTOP-C8KN1E3",
        "MachineName": None,
        "CompletedChunks": 0,
        "RenderingChunks": 1,
        "QueuedChunks": 0,
        "PendingChunks": 0,
        "SuspendedChunks": 0,
        "FailedChunks": 0,
        "Props": {"Name": "ep01-sq01-sh070", "Tasks": 1,
                  "OutDir": [empty_dir]},
    }
    con = _make_con(jobs=[job])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, job_id="6a428000", progress=0.0, output_dir=empty_dir)

    result = detector.check()

    assert len(result) == 1
    assert result[0].job_id == "6a428000"
    assert result[0].last_snapshot.worker == "DESKTOP-C8KN1E3"


def test_queued_job_does_not_stall(tmp_path):
    """Active job with NO rendering task (queued / blacklisted off the only
    worker) must never accrue a stall, even with frozen progress and an empty
    output dir. This is the single-machine blacklist case."""
    empty_dir = str(tmp_path)
    # RenderingChunks == 0 -> the job is queued, not rendering.
    con = _make_con(
        jobs=[_make_job(progress=50.0, output_dir=empty_dir, rendering=False)],
    )
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=empty_dir)

    result = detector.check()

    assert result == []


def test_current_worker_already_failed_tracks_prior_blacklist_state(tmp_path):
    """A third stall on a new worker must still be eligible for suspension;
    only a stall on a worker that was already in failed_workers before this
    check should be treated as the single-worker/no-fresh-worker case."""
    empty_dir = str(tmp_path)
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir=empty_dir, worker="render-node-02")])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=empty_dir, worker="render-node-02")
    detector._history["job-001"].stall_count = 2
    detector._history["job-001"].failed_workers = ["render-node-01"]

    result = detector.check()

    assert len(result) == 1
    assert result[0].stall_count == 3
    assert result[0].failed_workers == ["render-node-01", "render-node-02"]
    assert result[0].current_worker_already_failed is False

def test_not_stalled_progress_changed_downwards(tmp_path):
    """Progress rollback/requeue is still active work, not a silent stall."""
    output_dir = str(tmp_path)
    con = _make_con(jobs=[_make_job(progress=40.0, output_dir=output_dir)])
    detector = StallDetector(con=con, stall_threshold_min=20)
    _set_baseline(detector, progress=50.0, output_dir=output_dir)

    result = detector.check()

    assert result == []

