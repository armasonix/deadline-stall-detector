"""Rich CLI for the stall monitor.

Two modes:
  default        - quiet watchdog: scrolling event log
  --dashboard    - live rich table, spinner always rotating, no scroll
"""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

try:
    import readchar as _readchar
    _READCHAR_OK = True
except ImportError:
    _READCHAR_OK = False

from deadline_tools.connection import get_connection
from deadline_tools.stall_detector import StallDetector
from deadline_tools.recovery import handle_stall
from deadline_tools.notifier import TelegramNotifier
from deadline_tools.version import APP_VERSION_LABEL

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)
console = Console()

POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL_SEC",   "60"))
STALL_THRESHOLD = int(os.environ.get("STALL_THRESHOLD_MIN", "20"))
_RUNTIME_POLL   = POLL_INTERVAL

_SPINNER = ["|", "/", "-", "\\"]
TICK     = 0.25   # UI refresh: 4 fps, fully independent from poll


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _stall_counter_text(count: int, threshold: int = 3) -> Text:
    label = f"{count}/{threshold}"
    if count >= 3:
        return Text(label, style="bold red")
    if count == 2:
        return Text(label, style="bold yellow")
    return Text(label, style="green")


def _status_text(status: str) -> Text:
    s = status.upper()
    if s == "OK":
        return Text("[ OK  ]", style="green")
    if "STALL" in s:
        return Text("[STALL]", style="bold yellow")
    if "SUSPEND" in s:
        return Text("[SUSP ]", style="bold red")
    if "QUEUE" in s:
        return Text("[QUEUE]", style="cyan")
    return Text(s, style="dim")


# A job earns a spinning indicator only while a worker is actively rendering it.
_RENDERING_DL_STATUSES = {"rendering"}


def _is_rendering(info: dict) -> bool:
    """True only when Deadline reports the job as actively Rendering.

    Suspended / Queued / Completed / Failed jobs are not rendering and must
    show a static marker instead of a spinner.
    """
    return str(info.get("dl_status", "")).lower() in _RENDERING_DL_STATUSES


def _should_suspend(history, job_states: dict) -> bool:
    """Return False when all known workers for this job are already blacklisted."""
    failed = set(history.failed_workers)
    if not failed:
        return True
    info = job_states.get(history.job_id, {})
    worker = info.get("worker")
    return not (worker in failed and history.current_worker_already_failed)


# ---------------------------------------------------------------------------
# background worker thread
# ---------------------------------------------------------------------------

class _DashboardState:
    """Shared mutable state between the background poll thread and the UI thread."""

    def __init__(self) -> None:
        self.lock          = threading.Lock()
        self.job_states: dict = {}
        self.last_poll_ts  = 0.0
        self.action_queue: list = []

def _deadline_status_for_job(detector: StallDetector, job: dict) -> str:
    """Return the display status reported by Deadline for a raw job dict."""
    stat = job.get("Stat", -1)
    if stat == 1:
        return "Rendering" if detector._job_is_rendering(job) else "Queued"

    stat_map = {
        2: "Suspended",
        3: "Completed",
        4: "Failed",
        6: "Pending",
    }
    return stat_map.get(stat, "?")


def _sync_job_state_from_deadline(
    state: _DashboardState,
    detector: StallDetector,
    job: dict,
) -> None:
    """Update the dashboard row from Deadline's authoritative job state.

    Rows can be changed by dashboard hotkeys between poll cycles.  The refresh
    pass must therefore update already-known non-active jobs too; otherwise a
    job suspended with the ``S`` hotkey can remain visually stuck as Rendering.
    """
    jid = job.get("_id", "")
    if not jid:
        return

    props = job.get("Props", {}) or {}
    name = props.get("Name", jid)
    pct = detector._job_progress(job, props)
    worker = detector._job_worker(job, jid) or detector._get_active_worker(jid)
    dl_status = _deadline_status_for_job(detector, job)

    with state.lock:
        if jid not in state.job_states:
            state.job_states[jid] = {
                "name":        name,
                "status":      "ok",
                "stall_count": 0,
                "worker":      worker,
                "since":       "-",
            }
        js = state.job_states[jid]
        js["name"] = name
        js["pct"] = pct
        js["dl_status"] = dl_status
        if worker:
            js["worker"] = worker

        # Keep automatic stall escalation visible, but always allow Deadline's
        # authoritative state transitions into and out of Suspended.  This lets
        # a job suspended from the dashboard switch back to QUEUE/OK after an
        # operator resumes it from Deadline Monitor.
        cur = js.get("status", "ok")
        if dl_status == "Suspended":
            js["status"] = "suspended"
        elif cur == "suspended" and dl_status == "Rendering":
            js["status"] = "ok"
        elif cur == "suspended" and dl_status == "Queued":
            js["status"] = "queued"
        elif cur not in ("stalled", "suspended"):
            if dl_status == "Rendering":
                js["status"] = "ok"
            elif dl_status == "Queued":
                js["status"] = "queued"
            else:
                js["status"] = dl_status.lower() if dl_status != "?" else "queued"

def _poll_worker(
    state: _DashboardState,
    detector: StallDetector,
    notifier: TelegramNotifier,
    stop: threading.Event,
) -> None:
    """Runs in a daemon thread. Polls Deadline every RUNTIME_POLL seconds."""
    time.sleep(0.5)
    while not stop.is_set():
        t0 = time.monotonic()

        # --- stall check ---
        try:
            stalled = detector.check()
        except Exception as exc:
            log.error("detector.check() failed: %s", exc)
            stalled = []

        with state.lock:
            for history in stalled:
                con = detector._con
                try:
                    job_dict = con.Jobs.GetJob(history.job_id)
                except Exception:
                    job_dict = {
                        "_id":   history.job_id,
                        "Props": {"Name": history.job_id},
                        "MachineName": None,
                    }

                if history.stall_count >= 3 and not _should_suspend(history, state.job_states):
                    log.info(
                        "Skipping tier-3 suspend for %s: all workers blacklisted.",
                        history.job_id,
                    )
                    continue

                handle_stall(con, history, job_dict, notifier)
                # Update in place so progress/dl_status set by the refresh pass
                # are preserved (do not blow away the whole row).
                js = state.job_states.setdefault(history.job_id, {})
                js.update({
                    "name":        job_dict.get("Props", {}).get("Name", history.job_id),
                    "status":      "suspended" if history.stall_count >= 3 else "stalled",
                    "stall_count": history.stall_count,
                    "worker": (
                        job_dict.get("Mach")
                        or job_dict.get("MachineName")
                        or (history.last_snapshot.worker if history.last_snapshot else None)
                    ),
                    "since": _now(),
                })

        # --- refresh jobs + progress ---
        try:
            raw = detector._con.Jobs.GetJobs() or []
            for j in raw:
                # New dashboard rows are created for Active jobs only, but rows
                # already visible in the dashboard must keep tracking Deadline
                # after a hotkey action changes them to Suspended/Completed/etc.
                jid = j.get("_id", "")
                if j.get("Stat", -1) != 1 and jid not in state.job_states:
                    continue
                _sync_job_state_from_deadline(state, detector, j)

        except Exception as exc:
            log.error("Job refresh failed: %s", exc)

        state.last_poll_ts = time.monotonic()

        elapsed   = time.monotonic() - t0
        remaining = _RUNTIME_POLL - elapsed
        while remaining > 0 and not stop.is_set():
            time.sleep(min(0.2, remaining))
            remaining = _RUNTIME_POLL - (time.monotonic() - t0)


def _is_manual_action_candidate(info: dict) -> bool:
    """Return True for rows that a dashboard hotkey may act on.

    Prefer explicit stalled rows, but also allow currently rendering/queued rows.
    This keeps the dashboard hotkeys useful when an operator wants to manually
    intervene before the automatic stall counter has advanced.
    """
    if info.get("stall_count", 0) > 0:
        return True
    return str(info.get("dl_status", "")).lower() in {"rendering", "queued"}


def _handle_dashboard_action(
    state: _DashboardState,
    detector: StallDetector,
    key: str,
) -> bool:
    """Handle one dashboard hotkey action.

    Returns True when the dashboard should stop.  The caller owns
    ``state.lock``; keeping this logic in a helper lets unit tests exercise the
    actual hotkey behavior instead of only testing the refresh path.
    """
    if key == "q":
        return True

    if key == "r":
        for jid, info in list(state.job_states.items()):
            if _is_manual_action_candidate(info):
                try:
                    detector._con.Jobs.RequeueJob(jid)
                    info["status"] = "ok"
                    info["stall_count"] = 0
                    info["dl_status"] = "Queued"
                except Exception as exc:
                    log.error("Dashboard requeue failed for %s: %s", jid, exc)
        return False

    if key == "s":
        for jid, info in list(state.job_states.items()):
            if _is_manual_action_candidate(info):
                try:
                    detector._con.Jobs.SuspendJob(jid)
                    info["status"] = "suspended"
                    info["dl_status"] = "Suspended"
                except Exception as exc:
                    log.error("Dashboard suspend failed for %s: %s", jid, exc)
        return False

    return False

# ---------------------------------------------------------------------------
# hotkey thread
# ---------------------------------------------------------------------------

def _hotkey_listener(action_queue: list, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            key = _readchar.readchar()
            if key in ("r", "R", "s", "S", "q", "Q"):
                action_queue.append(key.lower())
        except Exception:
            break


# ---------------------------------------------------------------------------
# table builder
# ---------------------------------------------------------------------------

def _build_table(job_states: dict, frame: int) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        show_edge=True,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("",         width=1,  no_wrap=True)
    table.add_column("Job",      style="white", no_wrap=True, min_width=20)
    table.add_column("Status",   min_width=9)
    table.add_column("Stalls",   justify="center", min_width=7)
    table.add_column("Progress", min_width=22)
    table.add_column("Worker",   style="dim cyan", min_width=16)
    table.add_column("Since",    style="dim", min_width=8)

    spin = _SPINNER[frame % len(_SPINNER)]

    for job_id, info in job_states.items():
        pct       = info.get("pct", 0.0)
        dl_status = info.get("dl_status", "?")

        # Spinner only for actively rendering jobs; everything else is static.
        marker = Text(spin, style="cyan") if _is_rendering(info) else Text(".", style="dim")

        bar_len = 10
        filled  = int(bar_len * pct / 100)
        bar     = "[" + "#" * filled + "." * (bar_len - filled) + f"] {pct:5.1f}%"
        prog_cell = Text.from_markup(f"{bar}\n[dim]{dl_status}[/]")

        table.add_row(
            marker,
            info.get("name", job_id),
            _status_text(info.get("status", "ok")),
            _stall_counter_text(info.get("stall_count", 0)),
            prog_cell,
            info.get("worker") or "-",
            info.get("since",  "-"),
        )

    return table


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def run_dashboard(detector: StallDetector, notifier: TelegramNotifier) -> None:
    state    = _DashboardState()
    stop_evt = threading.Event()
    frame    = 0

    # A dedicated console for the dashboard. force_terminal=True makes Rich
    # emit real control codes even when it cannot auto-detect a TTY (for
    # example Git Bash / MinTTY and some PowerShell hosts). The dashboard is
    # intentionally redrawn as one full-screen frame below instead of relying
    # on Rich Live's line-diff refresh: clearing the alternate screen on every
    # tick is a little more brute-force, but it prevents stale header rows from
    # surviving after stall events or terminal resize redraws.
    dash_console = Console(force_terminal=True, file=sys.__stdout__)

    threading.Thread(
        target=_poll_worker,
        args=(state, detector, notifier, stop_evt),
        daemon=True,
    ).start()

    if _READCHAR_OK:
        threading.Thread(
            target=_hotkey_listener,
            args=(state.action_queue, stop_evt),
            daemon=True,
        ).start()

    HEADER = (
        f"[bold cyan]+= Deadline Stall Monitor {APP_VERSION_LABEL} ==[/]"
        f"[dim]  threshold={STALL_THRESHOLD}m  poll={POLL_INTERVAL}s  [/]"
        f"[bold cyan]=+[/]"
        f"    [dim][ [r] requeue  [s] suspend  [q] quit ][/]"
    )

    try:
        # Alternate screen keeps the shell scrollback clean. Each iteration
        # clears from a known home position before printing the next complete
        # frame, so terminal resizes and bursty stall updates cannot leave
        # duplicate headers behind.
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            dash_console.screen(hide_cursor=True),
        ):
            while True:
                with state.lock:
                    while state.action_queue:
                        key = state.action_queue.pop(0)
                        if _handle_dashboard_action(state, detector, key):
                            stop_evt.set()
                            raise KeyboardInterrupt

                next_poll = max(0, int(_RUNTIME_POLL - (time.monotonic() - state.last_poll_ts)))

                with state.lock:
                    snap = dict(state.job_states)

                status_line = (
                    f"[dim]{_now()}[/]"
                    f"    [dim]next poll in {next_poll}s[/]"
                )

                body = (
                    _build_table(snap, frame)
                    if snap
                    else Text.from_markup("[dim]  Waiting for jobs...[/]")
                )

                dash_console.clear(home=True)
                dash_console.print(Panel(
                    Group(
                        Text.from_markup(HEADER),
                        Rule(style="dim cyan"),
                        Text.from_markup(status_line),
                        Rule(style="dim"),
                        body,
                    ),
                    border_style="cyan",
                    padding=(0, 1),
                ))

                frame += 1
                time.sleep(TICK)

    except KeyboardInterrupt:
        pass
    finally:
        # We are back on the normal screen buffer here.
        console.print("[dim]Monitor stopped.[/]")


# ---------------------------------------------------------------------------
# watchdog (default)
# ---------------------------------------------------------------------------

def run_watchdog(detector: StallDetector, notifier: TelegramNotifier) -> None:
    console.print(
        f"[bold cyan] Deadline Stall Monitor {APP_VERSION_LABEL}[/] — watchdog mode  "
        f"[dim](threshold={STALL_THRESHOLD}m, poll={POLL_INTERVAL}s)[/]"
    )
    console.rule(style="dim cyan")

    try:
        while True:
            ts = _now()
            try:
                raw   = detector._con.Jobs.GetJobs() or []
                count = sum(1 for j in raw if j.get("Stat", -1) == 1)
            except Exception:
                count = "?"

            console.print(f" [dim]{ts}[/]  Monitoring [cyan]{count}[/] active jobs...")

            stalled = detector.check()
            for history in stalled:
                con = detector._con
                try:
                    job_dict = con.Jobs.GetJob(history.job_id)
                except Exception:
                    job_dict = {"_id": history.job_id,
                                "Props": {"Name": history.job_id},
                                "MachineName": None}

                name   = job_dict.get("Props", {}).get("Name", history.job_id)
                worker = (
                    job_dict.get("Mach")
                    or job_dict.get("MachineName")
                    or (history.last_snapshot.worker if history.last_snapshot else None)
                )
                sc = history.stall_count

                if sc >= 3:
                    if (
                        worker
                        and worker in history.failed_workers
                        and history.current_worker_already_failed
                    ):
                        log.info(
                            "Skipping tier-3 suspend for %s: worker %s is already blacklisted.",
                            history.job_id,
                            worker,
                        )
                        console.print(
                            f" [dim]{ts}[/]  [cyan][QUEUE]: {name} remains queued; "
                            f"worker {worker} already blacklisted[/]"
                        )
                        continue
                    console.print(f" [dim]{ts}[/]  [bold red][SUSP ]: {name}[/]")
                elif sc == 2:
                    console.print(f" [dim]{ts}[/]  [bold yellow][STALL] AGAIN: {name}[/]")
                    console.print(f" [dim]{ts}[/]  [bold red][BLKLST] worker={worker or '?'}[/]")
                else:
                    console.print(f" [dim]{ts}[/]  [yellow][STALL]: {name} requeue #{sc}[/]")

                action = handle_stall(con, history, job_dict, notifier)
                if "requeue" in action:
                    console.print(
                        f" [dim]{ts}[/]  [green][REQUE ] -> {worker or 'next available'}[/]"
                    )

            time.sleep(_RUNTIME_POLL)

    except KeyboardInterrupt:
        console.print("\n[dim]Monitor stopped.[/]")


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    global POLL_INTERVAL, STALL_THRESHOLD, _RUNTIME_POLL

    parser = argparse.ArgumentParser(
        prog="deadline-monitor",
        description="Deadline stall watchdog.",
    )
    parser.add_argument("--dashboard",  action="store_true")
    parser.add_argument("--threshold",  type=int, default=STALL_THRESHOLD, metavar="MIN")
    parser.add_argument("--poll",       type=int, default=POLL_INTERVAL,   metavar="SEC")
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    POLL_INTERVAL   = args.poll
    STALL_THRESHOLD = args.threshold
    _RUNTIME_POLL   = args.poll

    # Always log to stderr. In dashboard mode the Live region redirects
    # stderr so log lines are captured instead of tearing the single frame.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    try:
        con = get_connection()
    except Exception as exc:
        console.print(f"[bold red]Cannot connect to Deadline WebService:[/] {exc}")
        sys.exit(1)

    notifier = TelegramNotifier()
    detector = StallDetector(con=con, stall_threshold_min=args.threshold)
    detector._con = con

    if args.dashboard:
        run_dashboard(detector, notifier)
    else:
        run_watchdog(detector, notifier)
