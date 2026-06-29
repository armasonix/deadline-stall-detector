"""Entry point. Loads .env from CWD before importing anything else."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Look for .env in CWD first, then in project root (one level up from this package)
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            break
except ImportError:
    pass

from deadline_tools.monitor_cli import main

if __name__ == "__main__":
    main()
