"""Application version helpers.

The release version is centralized in ``pyproject.toml``.  Installed builds use
package metadata; source-tree runs fall back to reading the same pyproject value
so dashboard/watchdog headers do not need a second hard-coded version string.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_PACKAGE_NAME = "deadline-stall-detector"
_FALLBACK_VERSION = "0.0.0"


def _version_from_pyproject() -> str | None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                return stripped.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def get_version() -> str:
    """Return the application version from installed metadata or pyproject."""
    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _version_from_pyproject() or _FALLBACK_VERSION


APP_VERSION = get_version()
APP_VERSION_LABEL = f"v.{APP_VERSION}"