"""Unit tests for StallDetector.check() — без зависимости от Deadline."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from deadline_tools.stall_detector import JobSnapshot, StallDetector, StallHistory


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_job(job_id="job-001", name="test_job", progress=50.0,
              output_dir="", worker="render-node-01"):
    """Фейковый dict в формате Deadline Jobs API."""
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


# ── тесты ────────────────────────────────────────────────────────────────────

def test_first_run_no_stall():
    """Первый запуск — нет истории, нечего сравнивать → пустой список."""
    con = _make_con(jobs=[_make_job()])
    detector = StallDetector(con=con, stall_threshold_min=20)

    result = detector.check()

    assert result == [], "Первый запуск не должен возвращать зависания"
    assert "job-001" in detector._snapshots


def test_stalled_no_progress_no_files():
    """Снапшот 25 минут назад, прогресс не изменился, файлов нет → stall."""
    con = _make_con(jobs=[_make_job(progress=50.0, output_dir="")])
    detector = StallDetector(con=con, stall_threshold_min=20)

    # Устанавливаем baseline с timestamp 25 минут назад
    old_time = datetime.utcnow() - timedelta(minutes=25)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001",
        name="test_job",
        progress=50.0,       # прогресс не изменился
        output_dir="",       # нет output_dir → _new_files_exist вернёт True
        worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    # output_dir пустой → _new_files_exist возвращает True (недоступна),
    # значит зависание не будет зафиксировано. Используем несуществующую папку.
    con2 = _make_con(jobs=[_make_job(progress=50.0, output_dir="C:/nonexistent_12345")])
    detector.con = con2

    result = detector.check()

    assert len(result) == 1
    assert result[0].job_id == "job-001"
    assert result[0].stall_count == 1


def test_not_stalled_progress_moved():
    """Снапшот 25 минут назад, прогресс +15% → не зависший."""
    con = _make_con(jobs=[_make_job(progress=65.0)])
    detector = StallDetector(con=con, stall_threshold_min=20)

    old_time = datetime.utcnow() - timedelta(minutes=25)
    detector._snapshots["job-001"] = JobSnapshot(
        job_id="job-001",
        name="test_job",
        progress=50.0,       # было 50%
        output_dir="",
        worker="render-node-01",
        timestamp=old_time,
    )
    detector._history["job-001"] = StallHistory(job_id="job-001")

    result = detector.check()

    assert result == [], "Прогресс двигается — зависания нет"


def test_stall_count_increments():
    """Два последовательных stall → stall_count = 2."""
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
    """Прошло только 5 минут при threshold=20 → пропускаем проверку."""
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

    assert result == [], "Порог не достигнут — зависание не должно регистрироваться"
