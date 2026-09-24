"""Agent Observer: local SQLite ledger for agent execution evidence.

Stdlib only. No runtime dependency on ccusage.
"""

from pathlib import Path as _Path


def _read_version() -> str:
    """The package version, read from the repository VERSION file so the
    two can never drift apart."""
    try:
        return (_Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8").strip()
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()
SCHEMA_VERSION = 2
EVENT_CONTRACT_VERSION = 2
CAPTURE_CONTRACT_VERSION = 1
