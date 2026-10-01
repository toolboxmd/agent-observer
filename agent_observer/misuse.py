"""Chromeria thread misuse (#42): deep spawn chains and cross-thread messages.

Reads only the ``t3_spawns`` and ``t3_messages`` mirrors written by the T3
adapter at sync. Two signals, both factual and never judged here:

- a thread more than ``MAX_DEPTH`` spawn levels below its root, with its
  full chain from the root;
- a ``message_thread`` delivery into a thread that is not the sender's own
  child (Chromeria attributes exactly those), with sender, target, their
  relation and time.

Roles come from Prism's ``<role>-<hex>`` child id suffix and stay unknown
otherwise. Stdlib only.
"""

from __future__ import annotations

import re
import sqlite3

from .adapters.t3 import parent_thread, thread_root
from .ingest import iso_ts

MAX_DEPTH = 2
ROLES = ("planner", "dispatcher", "worker", "reviewer", "retry", "escalation")
ROLE_SUFFIX_RE = re.compile(r"^(%s)-[0-9a-f]+$" % "|".join(ROLES))


def role_of(thread_id: str) -> str | None:
    """The Prism role named in a child thread id, or None when unknown."""
    if parent_thread(thread_id) is None:
        return None
    match = ROLE_SUFFIX_RE.match(thread_id[thread_id.rfind(".") + 1:])
    return match.group(1) if match else None


def chain(thread_id: str) -> list:
    """Every thread from the root down to ``thread_id``."""
    out = [thread_id]
    parent = parent_thread(thread_id)
    while parent is not None:
        out.append(parent)
        parent = parent_thread(parent)
    return list(reversed(out))


def relation(sender: str, target: str) -> str:
    """How a message target relates to its sender in the spawn tree."""
    if target == parent_thread(sender):
        return "child to parent"
    if target in chain(sender):
        return "to ancestor"
    if sender in chain(target):
        return "to grandchild or deeper"
    if thread_root(sender) == thread_root(target):
        return "within tree"
    return "across trees"


def _in_window(stamp, since, until) -> bool | None:
    """True or False for a dated row; None when the row has no time."""
    when = iso_ts(stamp)
    if when is None:
        return None
    return (since is None or when >= since) and (until is None or when < until)


def _tables(con: sqlite3.Connection) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def report(con: sqlite3.Connection, since: float | None = None,
           until: float | None = None) -> dict:
    """Deep spawns and cross-thread messages inside [since, until)."""
    windowed = since is not None or until is not None
    out = {"window": {"since": since, "until": until},
           "max_depth": MAX_DEPTH, "imported": True,
           "deep_spawns": [], "cross_messages": [], "undated_excluded": 0}
    if not {"t3_spawns", "t3_messages"} <= _tables(con):
        out["imported"] = False
        return out
    for row in con.execute(
            "SELECT thread_id, depth, created_at FROM t3_spawns"
            " WHERE depth>? ORDER BY created_at, thread_id", (MAX_DEPTH,)):
        inside = _in_window(row["created_at"], since, until)
        if inside is None and windowed:
            out["undated_excluded"] += 1
        if not inside and windowed:
            continue
        out["deep_spawns"].append({
            "thread_id": row["thread_id"], "depth": row["depth"],
            "role": role_of(row["thread_id"]),
            "created_at": row["created_at"],
            "chain": [{"thread_id": t, "role": role_of(t)}
                      for t in chain(row["thread_id"])]})
    for row in con.execute(
            "SELECT sender_thread_id, target_thread_id, sent_at"
            " FROM t3_messages ORDER BY sent_at, message_id"):
        inside = _in_window(row["sent_at"], since, until)
        if inside is None and windowed:
            out["undated_excluded"] += 1
        if not inside and windowed:
            continue
        sender, target = row["sender_thread_id"], row["target_thread_id"]
        out["cross_messages"].append({
            "sender": sender, "sender_role": role_of(sender),
            "target": target, "target_role": role_of(target),
            "relation": relation(sender, target), "sent_at": row["sent_at"]})
    return out


def render(result: dict) -> str:
    """Plain text; empty sections say so explicitly."""
    if not result["imported"]:
        return ("no T3 spawn or message data in this ledger;"
                " run agent-observer sync --harness t3 first")
    lines = [f"spawn chains deeper than {result['max_depth']} levels:"
             f" {len(result['deep_spawns'])}"]
    if not result["deep_spawns"]:
        lines.append("  none")
    for spawn in result["deep_spawns"]:
        lines.append(f"- depth {spawn['depth']} {spawn['thread_id']}"
                     f" role={spawn['role'] or 'unknown'}"
                     f" at {spawn['created_at'] or 'unknown'}")
        lines.append("  chain: " + " > ".join(
            f"{t['thread_id']} ({t['role'] or 'unknown'})"
            for t in spawn["chain"]))
    lines.append("messages into a thread other than the sender's own child:"
                 f" {len(result['cross_messages'])}")
    if not result["cross_messages"]:
        lines.append("  none")
    for msg in result["cross_messages"]:
        lines.append(f"- {msg['sent_at'] or 'unknown'} {msg['relation']}:"
                     f" {msg['sender']} ({msg['sender_role'] or 'unknown'})"
                     f" -> {msg['target']} ({msg['target_role'] or 'unknown'})")
    if result["undated_excluded"]:
        lines.append(f"rows without a time left out of the window:"
                     f" {result['undated_excluded']}")
    return "\n".join(lines)
