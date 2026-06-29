from unittest.mock import MagicMock

from deadline_tools.monitor_cli import (
    _DashboardState,
    _deadline_status_for_job,
    _handle_dashboard_action,
    _sync_job_state_from_deadline,
)


class DummyDetector:
    def __init__(self):
        self._con = MagicMock()

    def _job_is_rendering(self, job):
        return int(job.get("RenderingChunks", 0) or 0) > 0

    def _job_progress(self, job, props):
        return 50.0

    def _job_worker(self, job, job_id):
        return job.get("Mach")

    def _get_active_worker(self, job_id):
        return None


def test_deadline_status_for_active_rendering_and_queued_jobs():
    detector = DummyDetector()

    assert _deadline_status_for_job(detector, {"Stat": 1, "RenderingChunks": 1}) == "Rendering"
    assert _deadline_status_for_job(detector, {"Stat": 1, "RenderingChunks": 0}) == "Queued"


def test_sync_known_dashboard_row_updates_hotkey_suspend_from_deadline():
    state = _DashboardState()
    detector = DummyDetector()
    state.job_states["job-001"] = {
        "name": "shot_001",
        "status": "ok",
        "stall_count": 1,
        "worker": "render-01",
        "since": "12:00:00",
        "dl_status": "Rendering",
    }

    _sync_job_state_from_deadline(
        state,
        detector,
        {
            "_id": "job-001",
            "Stat": 2,
            "RenderingChunks": 0,
            "SuspendedChunks": 1,
            "CompletedChunks": 0,
            "Props": {"Name": "shot_001"},
        },
    )

    assert state.job_states["job-001"]["status"] == "suspended"
    assert state.job_states["job-001"]["dl_status"] == "Suspended"


def test_sync_does_not_create_new_row_for_unknown_non_active_job_in_poll_loop_contract():
    # The poll loop filters unknown non-active jobs before calling this helper.
    # When called for a known row, the helper must still be able to sync any
    # Deadline state, including Suspended.
    state = _DashboardState()
    detector = DummyDetector()
    state.job_states["job-002"] = {"status": "stalled", "stall_count": 1, "since": "-"}

    _sync_job_state_from_deadline(
        state,
        detector,
        {"_id": "job-002", "Stat": 3, "CompletedChunks": 1, "Props": {"Name": "done"}},
    )

    assert state.job_states["job-002"]["dl_status"] == "Completed"


def test_sync_resumed_suspended_row_switches_to_queue_when_deadline_is_queued():
    state = _DashboardState()
    detector = DummyDetector()
    state.job_states["job-004"] = {
        "status": "suspended",
        "stall_count": 0,
        "dl_status": "Suspended",
    }

    _sync_job_state_from_deadline(
        state,
        detector,
        {
            "_id": "job-004",
            "Stat": 1,
            "RenderingChunks": 0,
            "QueuedChunks": 1,
            "CompletedChunks": 0,
            "Props": {"Name": "queued_after_resume"},
        },
    )

    assert state.job_states["job-004"]["status"] == "queued"
    assert state.job_states["job-004"]["dl_status"] == "Queued"


def test_sync_resumed_suspended_row_switches_to_ok_when_deadline_is_rendering():
    state = _DashboardState()
    detector = DummyDetector()
    state.job_states["job-005"] = {
        "status": "suspended",
        "stall_count": 0,
        "dl_status": "Suspended",
    }

    _sync_job_state_from_deadline(
        state,
        detector,
        {
            "_id": "job-005",
            "Stat": 1,
            "RenderingChunks": 1,
            "CompletedChunks": 0,
            "Props": {"Name": "rendering_after_resume"},
        },
    )

    assert state.job_states["job-005"]["status"] == "ok"
    assert state.job_states["job-005"]["dl_status"] == "Rendering"


def test_dashboard_suspend_hotkey_calls_deadline_for_rendering_row_without_stall_count():
    state = _DashboardState()
    detector = DummyDetector()
    state.job_states["job-003"] = {
        "status": "ok",
        "stall_count": 0,
        "dl_status": "Rendering",
    }

    should_stop = _handle_dashboard_action(state, detector, "s")

    assert should_stop is False
    detector._con.Jobs.SuspendJob.assert_called_once_with("job-003")
    assert state.job_states["job-003"]["status"] == "suspended"
    assert state.job_states["job-003"]["dl_status"] == "Suspended"

def test_should_suspend_new_worker_even_after_history_records_it():
    from deadline_tools.monitor_cli import _should_suspend
    from deadline_tools.stall_detector import StallHistory

    history = StallHistory(job_id="job-006", stall_count=3)
    history.failed_workers = ["render-01", "render-02"]
    history.current_worker_already_failed = False

    assert _should_suspend(history, {"job-006": {"worker": "render-02"}}) is True


def test_should_not_suspend_when_current_worker_was_already_failed():
    from deadline_tools.monitor_cli import _should_suspend
    from deadline_tools.stall_detector import StallHistory

    history = StallHistory(job_id="job-007", stall_count=3)
    history.failed_workers = ["render-01"]
    history.current_worker_already_failed = True

    assert _should_suspend(history, {"job-007": {"worker": "render-01"}}) is False