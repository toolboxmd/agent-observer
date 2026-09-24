"""Native source adapters, one per harness.

Each adapter module exposes:
- HARNESS: its name, also the natural-key prefix;
- CAPABILITIES: (family, supported, detail) rows for `trace --capabilities`;
- sync(con, root=None, full=False, source=None) -> dict: import everything
  it finds under root (or the single source), idempotently.
"""

from __future__ import annotations

import importlib

ORDER = ("codex", "claude", "opencode", "grok", "router")


def available() -> dict:
    """Adapters present in this build, by harness name."""
    found = {}
    for name in ORDER:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                continue
            raise
        if hasattr(module, "sync"):
            found[name] = module
    return found


def capabilities() -> list[dict]:
    rows = []
    for name, module in available().items():
        for family, supported, detail in getattr(module, "CAPABILITIES", []):
            rows.append({"harness": name, "family": family,
                         "supported": supported, "detail": detail})
    return rows
