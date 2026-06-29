"""Event CSV logger.

Appends one row per recovery action to logs/stall_events.csv.
Columns: timestamp, job_id, job_name, event, worker, stall_count
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path(os.environ.get("STALL_LOG_DIR", "logs"))
DEFAULT_CSV     = DEFAULT_LOG_DIR / "stall_events.csv"

_FIELDS = ["timestamp", "job_id", "job_name", "event", "worker", "stall_count"]


def _ensure(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_FIELDS).writeheader()


def record(
    event: str,
    job_id: str,
    job_name: str,
    worker: str | None = None,
    stall_count: int = 0,
    csv_path: Path = DEFAULT_CSV,
) -> None:
    """Append one event row."""
    _ensure(csv_path)
    row = {
        "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job_id":      job_id,
        "job_name":    job_name,
        "event":       event,
        "worker":      worker or "",
        "stall_count": stall_count,
    }
    try:
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_FIELDS).writerow(row)
    except OSError as exc:
        log.error("event_log: failed to write %s: %s", csv_path, exc)
