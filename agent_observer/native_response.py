"""Literal, source-scoped input identities for native usage rows."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from . import db


def identifier(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,200}", value):
        return value
    return None


def prepare(con, source_id, *, replace):
    """An adapter rebuilds only its own source, in its import transaction."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS native_response_inputs (
            session_key TEXT NOT NULL, response_id TEXT NOT NULL,
            input_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
            PRIMARY KEY(session_key, response_id, input_id, source_id)
        ) WITHOUT ROWID""")
    con.execute("CREATE INDEX IF NOT EXISTS native_response_source"
                " ON native_response_inputs(source_id)")
    if replace:
        con.execute("DELETE FROM native_response_inputs WHERE source_id=?", (source_id,))


def record(con, source_id, session_key, response_id, input_id):
    con.execute("INSERT OR IGNORE INTO native_response_inputs"
                "(session_key,response_id,input_id,source_id) VALUES(?,?,?,?)",
                (session_key, response_id, input_id, source_id))


def inputs(con: sqlite3.Connection, session_key: str) -> dict[str, set[str]]:
    """Keys are ledger response ids. Values are the adapter's unprefixed ids.

    An absent key has no proved input. Multiple candidates must be reconciled
    by the consumer. Reading an old ledger never creates adapter tables.
    """
    if session_key.startswith("claude:"):
        from .adapters import claude
        return claude.response_inputs(con, session_key)
    if session_key.startswith("grok:"):
        from .adapters import grok
        return grok.response_inputs(con, session_key)
    found = defaultdict(set)
    if db._has_table(con, "native_response_inputs"):
        for response_id, input_id in con.execute(
            "SELECT DISTINCT i.response_id,i.input_id FROM native_response_inputs i"
            " JOIN responses r USING(session_key,response_id)"
            " WHERE i.session_key=? AND r.is_overlap=0", (session_key,)
        ):
            found[response_id].add(input_id)
    if session_key.startswith("codex:"):
        from .adapters import codex
        for response_id, turn_id in codex.legacy_response_inputs(con, session_key):
            found[response_id].add(turn_id)
    return dict(found)


def prepare_events(con, source_id, *, replace):
    con.execute("""
        CREATE TABLE IF NOT EXISTS native_response_events (
            session_key TEXT NOT NULL, family TEXT NOT NULL, native_id TEXT NOT NULL,
            response_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
            PRIMARY KEY(session_key, family, native_id, response_id, source_id)
        ) WITHOUT ROWID""")
    con.execute("CREATE INDEX IF NOT EXISTS native_response_event_source ON native_response_events(source_id)")
    if replace:
        con.execute("DELETE FROM native_response_events WHERE source_id=?", (source_id,))


def record_event(con, source_id, session_key, family, native_id, response_id):
    con.execute("INSERT OR IGNORE INTO native_response_events"
                "(session_key,family,native_id,response_id,source_id) VALUES(?,?,?,?,?)",
                (session_key, family, native_id, response_id, source_id))


def event_responses(con, session_key):
    """Return literal event associations, including competing copies and unmatched calls."""
    edges = defaultdict(set)
    if db._has_table(con, "native_response_events"):
        for family, native_id, rid in con.execute(
                "SELECT DISTINCT family,native_id,response_id FROM native_response_events WHERE session_key=?",
                (session_key,)):
            edges[family, native_id].add(rid)
    claude = session_key.startswith("claude:")
    legacy_text = defaultdict(set)
    if claude and db._has_table(con, "claude_response_ancestry"):
        # Pre-event-edge ledgers still retain the UUID of each text block.
        for source, uuid, rid in con.execute(
                "SELECT a.source_id,a.uuid,a.response_id FROM claude_response_ancestry a "
                "JOIN sources s ON s.id=a.source_id WHERE a.session_key=? AND a.response_id<>'' "
                "AND s.claude_usage_version<7", (session_key,)):
            legacy_text[source, uuid].add(rid)
    for event, source, family, native_id in con.execute(
            "SELECT id,source_id,family,native_id FROM events WHERE session_key=?", (session_key,)):
        if claude and family == "assistant_message":
            yield event, edges[family, native_id] | legacy_text[source, native_id.rpartition(":")[0]]
        elif claude and family in {"tool_result", "permission", "skill_invoke", "file_change"}:
            yield event, edges["tool_call", native_id]
        elif claude and family in {"read", "skill_read"} and native_id.endswith(":read"):
            yield event, edges["tool_call", native_id[:-5]]
        else:
            yield event, edges[family, native_id]
