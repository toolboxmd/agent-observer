"""Chromeria bare-mode native record roots.

A bare Codex or Grok thread runs with its own native home. Chromeria
publishes where its records live: each bare launch writes a
``system_notice`` turn item whose ``bareReceipt.nativeRecord`` names the
harness and ``root`` (``<home>/sessions``). Default sync reads those
roots from the T3 state database the T3 adapter reads, next to the
normal home. Sessions stay keyed by native id, so a root also imported
through an explicit ``--root`` counts once.

Every receipt is accounted for. A root that is missing on this machine,
unreadable, empty or not recorded is a gap in the report, never a failed
source and never a silent zero. No receipts means bare mode is unused; no
state database or an unreadable one is reported as such.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Callable

from . import t3

TURN_ITEMS = "orchestration_v2_projection_turn_items"
# Chromeria ids every receipt ``bare-receipt:<provider thread>:<native id>``
# (RunExecutionService.writeBareReceipt). The primary-key range reads only
# receipts; nothing indexes ``type``, so filtering on it alone scans every
# turn item.
RECEIPTS = (
    f"SELECT json_extract(payload_json, '$.bareReceipt.nativeRecord.harness'),"
    f" json_extract(payload_json, '$.bareReceipt.nativeRecord.root')"
    f" FROM {TURN_ITEMS} WHERE turn_item_id >= 'bare-receipt:'"
    f" AND turn_item_id < 'bare-receipt;' AND type = 'system_notice'"
    f" AND json_extract(payload_json, '$.bareReceipt') IS NOT NULL")

# Receipts read in the current sync or health run, by state database path.
# None outside one_read(): every read is fresh, so a receipt committed to
# the WAL since an earlier run is never missed.
_run: dict | None = None


@contextmanager
def one_read():
    """Read each state database's receipts at most once inside this block."""
    global _run
    outer = _run
    if outer is None:
        _run = {}
    try:
        yield
    finally:
        if outer is None:
            _run = None


def _receipts(path: str) -> dict:
    """Recorded roots per harness, once per one_read() block."""
    if _run is not None and path in _run:
        return _run[path]
    roots: dict = {}
    native = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in native.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if TURN_ITEMS in tables:
            for harness, root in native.execute(RECEIPTS):
                roots.setdefault(harness, []).append(root)
    finally:
        native.close()
    if _run is not None:
        _run[path] = roots
    return roots


def receipt_roots(harness: str) -> tuple[list, dict]:
    """Recorded roots (None when a receipt names none) and the base report."""
    path = t3.state_path()
    report = {"state_db": path, "state": "unused", "receipts": 0, "roots": 0,
              "gaps": []}
    if not os.path.exists(path):
        report["state"] = "no T3 state"
        return [], report
    try:
        rows = _receipts(path).get(harness, [])
    except sqlite3.Error as exc:
        report["state"] = "unreadable T3 state"
        report["error"] = str(exc)
        return [], report
    report["receipts"] = len(rows)
    if rows:
        report["state"] = "used"
    return rows, report


def discover(harness: str,
             paths_under: Callable[[str], list[str]]) -> tuple[list[str], dict]:
    """Record paths under every receipt root for harness, plus the report.

    paths_under(root) lists one root's records. Roots are canonical, so
    aliases of one directory are read once.
    """
    rows, report = receipt_roots(harness)
    roots, gaps = [], []
    for root in rows:
        if not isinstance(root, str) or not root:
            gaps.append({"root": None, "reason": "root not recorded"})
            continue
        real = os.path.realpath(root)
        if real in roots or any(g["root"] == real for g in gaps):
            continue
        try:
            os.listdir(real)
        except FileNotFoundError:
            gaps.append({"root": real, "reason": "missing on this machine"})
            continue
        except OSError:
            gaps.append({"root": real, "reason": "unreadable"})
            continue
        roots.append(real)
    paths = []
    for root in roots:
        found = paths_under(root)
        if found:
            paths += found
        else:
            gaps.append({"root": root, "reason": "empty"})
    report["roots"] = len(roots) - sum(g["reason"] == "empty" for g in gaps)
    report["gaps"] = gaps
    return paths, report
