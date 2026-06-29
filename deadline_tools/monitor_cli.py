"""Rich CLI for the stall monitor.

Two modes:
  default        - quiet watchdog: scrolling event log
  --dashboard    - live rich table, spinner always rotating, no scroll
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from rich.console import Console, Group
from rich.live import Live
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
    info   = job_states.get(history.job_id, {})
    worker = info.get("worker")
    return worker not in failed


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

        # --- refresh active jobs + progress ---
        try:
            raw = detector._con.Jobs.GetJobs() or []
            for j in raw:
                if j.get("Stat", -1) != 1:
                    continue
                jid   = j.get("_id", "")
                props = j.get("Props", {}) or {}
                name  = props.get("Name", jid)

                # Progress + state come from the job's authoritative chunk
                # counters, not from parsing per-task status codes (which vary
                # between Deadline versions).
                pct = detector._job_progress(j, props)

                # Stat=1 is Active = idle (Queued) OR Rendering. The Deadline
                # docs say: use RenderingChunks to tell them apart.
                is_rendering = detector._job_is_rendering(j)
                worker = detector._job_worker(j, jid) or detector._get_active_worker(jid)

                stat = j.get("Stat", -1)
                if stat == 1:
                    dl_status = "Rendering" if is_rendering else "Queued"
                else:
                    stat_map = {2: "Suspended", 3: "Completed",
                                4: "Failed", 6: "Pending"}
                    dl_status = stat_map.get(stat, "?")

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
                    js["pct"]       = pct
                    js["dl_status"] = dl_status
                    if worker:
                        js["worker"] = worker

                    # Keep the displayed status in sync with Deadline, but never
                    # downgrade an active stall/suspend escalation that the
                    # recovery logic set this cycle.
                    cur = js.get("status", "ok")
                    if cur not in ("stalled", "suspended"):
                        if dl_status == "Suspended":
                            js["status"] = "suspended"
                        elif dl_status == "Rendering":
                            js["status"] = "ok"
                        else:
                            # Pending / Queued / etc.
                            js["status"] = "queued"

        except Exception as exc:
            log.error("Job refresh failed: %s", exc)

        state.last_poll_ts = time.monotonic()

        elapsed   = time.monotonic() - t0
        remaining = _RUNTIME_POLL - elapsed
        while remaining > 0 and not stop.is_set():
            time.sleep(min(0.2, remaining))
            remaining = _RUNTIME_POLL - (time.monotonic() - t0)


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
    # emit real control codes (cursor moves + alternate screen) even when it
    # cannot auto-detect a TTY - this is the case under Git Bash / MinTTY and
    # some PowerShell hosts, where the previous auto-detected fallback printed
    # the header again on every refresh (the "many headers" bug).
    dash_console = Console(force_terminal=True)

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
        f"[bold cyan]+= Deadline Stall Monitor ==[/]"
        f"[dim]  threshold={STALL_THRESHOLD}m  poll={POLL_INTERVAL}s  [/]"
        f"[bold cyan]=+[/]"
        f"    [dim][ [r] requeue  [s] suspend  [q] quit ][/]"
    )

    try:
        with Live(
            console=dash_console,
            refresh_per_second=int(1 / TICK),
            screen=True,        # alternate screen buffer: exactly one frame
            transient=False,
            redirect_stdout=True,   # swallow stray prints from any thread
            redirect_stderr=True,   # keep logs out of the live region
        ) as live:
            while True:
                with state.lock:
                    while state.action_queue:
                        key = state.action_queue.pop(0)
                        if key == "q":
                            stop_evt.set()
                            raise KeyboardInterrupt
                        elif key == "r":
                            for jid, info in list(state.job_states.items()):
                                if info.get("stall_count", 0) > 0:
                                    try:
                                        detector._con.Jobs.RequeueJob(jid)
                                        info["status"]      = "ok"
                                        info["stall_count"] = 0
                                    except Exception:
                                        pass
                        elif key == "s":
                            for jid, info in list(state.job_states.items()):
                                if info.get("stall_count", 0) > 0:
                                    try:
                                        detector._con.Jobs.SuspendJob(jid)
                                        info["status"] = "suspended"
                                    except Exception:
                                        pass

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

                live.update(Panel(
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
        f"[bold cyan] Deadline Stall Monitor[/] — watchdog mode  "
        f"[dim](threshold={STALL_THRESHOLD}m * poll={POLL_INTERVAL}s)[/]"
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
