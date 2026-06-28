"""CLI entry point - rich dashboard + watchdog polling loop.

Usage:
    python -m deadline_tools          # uses config.yaml in CWD
    python -m deadline_tools --config path/to/config.yaml
    python -m deadline_tools --once   # single check, then exit (useful for cron)

Environment overrides (all optional, override config.yaml values):
    DEADLINE_HOST          DEADLINE_PORT
    DEADLINE_REPO_PATH     DEADLINE_POLL_INTERVAL
    TELEGRAM_BOT_TOKEN     TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from deadline_tools.connection import get_connection, ping_webservice
from deadline_tools.notifier import TelegramNotifier
from deadline_tools.recovery import handle_stall
from deadline_tools.stall_detector import StallDetector

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich import box
    _RICH = True
except ImportError:
    _RICH = False

log = logging.getLogger(__name__)
_STOP = False


def _handle_signal(sig, frame):  # noqa: ANN001
    global _STOP
    _STOP = True
    print("\nShutting down...")


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _load_config(path: Optional[str]) -> dict:
    cfg_path = Path(path) if path else Path("config.yaml")
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _build_table(detector: StallDetector) -> "Table":
    """Render current snapshot state as a rich Table."""
    table = Table(
        title="Deadline Stall Monitor",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    table.add_column("Job ID", style="dim", no_wrap=True)
    table.add_column("Name")
    table.add_column("Progress", justify="right")
    table.add_column("Worker")
    table.add_column("Stalls", justify="right")

    for snap in detector._snapshots.values():
        history = detector._history.get(snap.job_id)
        stall_count = history.stall_count if history else 0
        stall_color = "red" if stall_count >= 2 else ("yellow" if stall_count == 1 else "green")
        table.add_row(
            snap.job_id,
            snap.name,
            f"{snap.progress:.1f}%",
            snap.worker or "-",
            f"[{stall_color}]{stall_count}[/{stall_color}]",
        )

    return table


def run(config_path: Optional[str] = None, once: bool = False) -> None:
    cfg = _load_config(config_path)

    poll_interval: int = int(cfg.get("poll_interval_sec", 60))
    stall_threshold: int = int(cfg.get("stall_threshold_min", 20))

    logging.basicConfig(
        level=cfg.get("log_level", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    con = get_connection()
    if not ping_webservice(con):
        log.error("Cannot reach Deadline WebService. Check host/port settings.")
        sys.exit(1)

    notifier = TelegramNotifier()
    detector = StallDetector(con=con, stall_threshold_min=stall_threshold)

    console = Console() if _RICH else None
    log.info("Monitor started (poll=%ds, threshold=%dmin)", poll_interval, stall_threshold)

    while not _STOP:
        try:
            stalled = detector.check()
        except Exception as exc:
            log.error("check() failed: %s", exc)
            stalled = []

        for history in stalled:
            job_id = history.job_id
            try:
                job_dict = con.Jobs.GetJob(job_id)
            except Exception:
                job_dict = {"_id": job_id, "Props": {"Name": job_id}}

            action = handle_stall(con, history, job_dict, notifier)
            log.info("Recovery action for %s: %s", job_id, action)

        if console and _RICH:
            console.clear()
            console.print(_build_table(detector))

        if once:
            break

        time.sleep(poll_interval)

    log.info("Monitor stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deadline Stall Monitor")
    parser.add_argument("--config", metavar="PATH", help="Path to config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="Run a single check cycle and exit")
    args = parser.parse_args()
    run(config_path=args.config, once=args.once)


if __name__ == "__main__":
    main()
