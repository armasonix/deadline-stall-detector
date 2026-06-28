from __future__ import annotations
import os
import sys
from pathlib import Path


def get_connection():
    """
    Returns a live DeadlineCon instance.

    Env vars:
        DEADLINE_REPO_PATH  — path to DeadlineRepository root
        DEADLINE_HOST       — hostname or IP (default: localhost)
        DEADLINE_PORT       — WebService port (default: 8082)
    """
    repo_path = os.environ.get("DEADLINE_REPO_PATH", r"C:\DeadlineRepository10")
    api_path = str(Path(repo_path) / "api" / "python")

    if api_path not in sys.path:
        sys.path.insert(0, api_path)

    try:
        import Deadline.DeadlineConnect as Connect
    except ImportError as e:
        raise ImportError(
            f"Deadline Python API not found at: {api_path}\n"
            f"Set DEADLINE_REPO_PATH to your repository root.\n"
            f"Original error: {e}"
        )

    host = os.environ.get("DEADLINE_HOST", "localhost")
    port = int(os.environ.get("DEADLINE_PORT", "8082"))
    return Connect.DeadlineCon(host, port)


def ping_webservice(con) -> bool:
    """Quick health check — returns True if WebService responds."""
    try:
        con.Slaves.GetSlaveNames()
        return True
    except Exception:
        return False
