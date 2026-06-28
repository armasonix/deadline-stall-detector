"""Rich CLI for the stall monitor.

Two modes:
  default        — quiet watchdog: scrolling event log
  --dashboard    — live rich table with spinner + hotkeys [r] [s] [q]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box

from deadline_tools.connection import get_connection
from deadline_tools.stall_detector import StallDetector
from deadline_tools.recovery import handle_stall
from deadline_tools.notifier import TelegramNotifier

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)
console = Console()

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "60"))
STALL_THRESHOLD = int(os.environ.get("STALL_THRESHOLD_MIN", "20"))


# ── helpers ────────────────────────────────────────────────────────────────────

def _stall_counter_text(count: int, threshold: int = 3) -> Text:
    label = f"{count}/{threshold}"
    if count == 0:
        return Text(label, style="green")
    if count == 1:
        return Text(label, style="green")
    if count == 2:
        return Text(label, style="bold yellow")
    return Text(label, style="bold red")


def _status_text(status: str) -> Text:
    s = status.upper()
    if s == "OK":
        return Text("● OK", style="green")
    if "STALL" in s:
        return Text("⚠  STALLED", style="bold yellow")
    if "SUSPEND" in s:
        return Text("🚨 SUSPENDED", style="bold red blink")
    return Text(s, style="dim")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── dashboard mode ─────────────────────────────────────────────────────────────

def _build_table(job_states: dict) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        show_edge=True,
        expand=True,
    )
    table.add_column("Job", style="white", no_wrap=True, min_width=24)
    table.add_column("Status", min_width=14)
    table.add_column("Stalls", justify="center", min_width=7)
    table.add_column("Worker", style="dim cyan", min_width=16)
    table.add_column("Since", style="dim", min_width=8)

    for job_id, info in job_states.items():
        table.add_row(
            info.get("name", job_id),
            _status_text(info.get("status", "ok")),
            _stall_counter_text(info.get("stall_count", 0)),
            info.get("worker") or "—",
            info.get("since", "—"),
        )
    return table


def run_dashboard(detector: StallDetector, notifier: TelegramNotifier) -> None:
    """Live rich dashboard — refreshes every POLL_INTERVAL seconds."""
    job_states: dict = {}
    spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    frame = 0

    console.print(
        f"\n[bold cyan]╔═ Deadline Stall Monitor ══ dashboard mode ══ "
        f"threshold={STALL_THRESHOLD}m · poll={POLL_INTERVAL}s ═╗[/]"
    )
    console.print("[dim]  [r] force requeue  [s] suspend  [q] quit[/]\n")

    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            while True:
                spin = spinner_frames[frame % len(spinner_frames)]
                frame += 1

                stalled = detector.check()

                for history in stalled:
                    con = detector._con
                    try:
                        job_dict = con.Jobs.GetJob(history.job_id)
                    except Exception:
                        job_dict = {"_id": history.job_id,
                                    "Props": {"Name": history.job_id},
                                    "MachineName": None}
                    handle_stall(con, history, job_dict, notifier)

                    job_states[history.job_id] = {
                        "name": job_dict.get("Props", {}).get("Name", history.job_id),
                        "status": "suspended" if history.stall_count >= 3 else "stalled",
                        "stall_count": history.stall_count,
                        "worker": job_dict.get("MachineName"),
                        "since": _now(),
                    }

                # Refresh known OK jobs
                try:
                    all_jobs = detector._con.Jobs.GetJobsInState(3) or []
                    for j in all_jobs:
                        jid = j.get("_id", "")
                        if jid not in job_states:
                            job_states[jid] = {
                                "name": j.get("Props", {}).get("Name", jid),
                                "status": "ok",
                                "stall_count": 0,
                                "worker": j.get("MachineName"),
                                "since": "—",
                            }
                except Exception:
                    pass

                header = (
                    f"[bold cyan]Deadline Stall Monitor[/]  "
                    f"[cyan]{spin}[/]  [dim]{_now()}[/]  "
                    f"[dim]jobs tracked: {len(job_states)}[/]"
                )
                table = _build_table(job_states)
                live.update(Text.from_markup(header + "\n") if not job_states
                            else table)

                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        console.print("\n[dim]Monitor stopped.[/]")


# ── watchdog mode (default) ────────────────────────────────────────────────────

def run_watchdog(detector: StallDetector, notifier: TelegramNotifier) -> None:
    """Quiet scrolling log — default mode."""
    console.print(
        f"[bold cyan] Deadline Stall Monitor[/] — watchdog mode  "
        f"[dim](threshold={STALL_THRESHOLD}m · poll={POLL_INTERVAL}s)[/]"
    )
    console.rule(style="dim cyan")

    try:
        while True:
            ts = _now()
            try:
                active = detector._con.Jobs.GetJobsInState(3) or []
                count = len(active)
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

                name = job_dict.get("Props", {}).get("Name", history.job_id)
                worker = job_dict.get("MachineName")
                sc = history.stall_count

                if sc >= 3:
                    console.print(
                        f" [dim]{ts}[/]  [bold red blink]🚨 SUSPENDED: {name}[/]"
                    )
                elif sc == 2:
                    console.print(
                        f" [dim]{ts}[/]  [bold yellow]⚠  STALLED AGAIN: {name}[/]"
                    )
                    console.print(
                        f" [dim]{ts}[/]  [bold red]🔴 Blacklisted: {worker or '?'}[/]"
                    )
                else:
                    console.print(
                        f" [dim]{ts}[/]  [yellow]⚠  STALLED: {name} → requeue #{sc}[/]"
                    )

                action = handle_stall(con, history, job_dict, notifier)

                if "requeue" in action:
                    console.print(
                        f" [dim]{ts}[/]  [green]✓  Requeued → {worker or 'next available'}[/]"
                    )

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        console.print("\n[dim]Monitor stopped.[/]")


# ── entrypoint ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="deadline-monitor",
        description="Deadline stall watchdog — detects hung jobs and auto-recovers.",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Show live rich dashboard instead of scrolling log.",
    )
    parser.add_argument(
        "--threshold", type=int, default=STALL_THRESHOLD,
        metavar="MIN",
        help=f"Minutes without progress to declare a stall (default: {STALL_THRESHOLD}).",
    )
    parser.add_argument(
        "--poll", type=int, default=POLL_INTERVAL,
        metavar="SEC",
        help=f"Polling interval in seconds (default: {POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        con = get_connection()
    except Exception as exc:
        console.print(f"[bold red]Cannot connect to Deadline WebService:[/] {exc}")
        sys.exit(1)

    notifier = TelegramNotifier()
    detector = StallDetector(con=con, stall_threshold_min=args.threshold)
    detector._con = con  # expose for dashboard job fetches

    if args.dashboard:
        run_dashboard(detector, notifier)
    else:
        run_watchdog(detector, notifier)
