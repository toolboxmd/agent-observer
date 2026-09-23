"""Source bookkeeping shared by the file adapters.

Append-only JSONL logs resume at the offset the previous import reached when
the bytes just before that offset are unchanged; otherwise the whole file is
read again. Either way every row lands under its natural key, so re-reading
never adds usage twice. A trailing line without a newline is left for the
next import because a live harness may still be writing it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time

from .db import now

TAIL_BYTES = 4096


def _tail_sha(fh, offset: int) -> str:
    start = max(0, offset - TAIL_BYTES)
    fh.seek(start)
    return hashlib.sha256(fh.read(offset - start)).hexdigest()


class JsonlSource:
    """One append-only JSONL file being imported."""

    def __init__(self, con: sqlite3.Connection, harness: str, path: str,
                 full: bool = False):
        self.con = con
        self.harness = harness
        self.path = path
        self.size = os.path.getsize(path)
        self.started = time.monotonic()
        row = con.execute(
            "SELECT * FROM sources WHERE harness=? AND path=?",
            (harness, path)).fetchone()
        self.row = row
        self.start_offset = 0
        self.unchanged = False
        if row is not None and not full:
            offset = row["read_offset"] or 0
            if 0 < offset <= self.size and row["tail_sha256"]:
                with open(path, "rb") as fh:
                    if _tail_sha(fh, offset) == row["tail_sha256"]:
                        self.start_offset = offset
                        self.unchanged = offset == self.size
        if row is None:
            con.execute(
                "INSERT INTO sources(harness, path, sha256, imported_at)"
                " VALUES(?,?,?,?)", (harness, path, "", now()))
            row = con.execute(
                "SELECT * FROM sources WHERE harness=? AND path=?",
                (harness, path)).fetchone()
            self.row = row
        self.source_id = row["id"]
        self.end_offset = self.start_offset

    @property
    def incremental(self) -> bool:
        return self.start_offset > 0

    def records(self):
        """Yield (ordinal, obj, raw_line) for complete lines after the start.

        obj is None when the line is not valid JSON; the caller records the
        error. Ordinals count lines from the start of the file.
        """
        with open(self.path, "rb") as fh:
            fh.seek(self.start_offset)
            ordinal = self.row["ordinal_max"] + 1 if self.incremental else 0
            position = self.start_offset
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                position += len(raw)
                text = raw.decode("utf-8", "replace")
                if text.strip():
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        obj = None
                    yield ordinal, obj, text
                    ordinal += 1
                self.end_offset = position
                self.last_ordinal = ordinal - 1

    def error(self, ordinal, message: str, line: str = "") -> None:
        # Privacy fails closed: the error column holds only a fixed safe
        # category derived from the leading token of the message. Never
        # persist str(exc), exception text, type values, IDs or record
        # values here; structural detail stays in line_excerpt only.
        self.con.execute(
            "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
            " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
            (self.harness, self.path, ordinal, _safe_category(message),
             _redacted_excerpt(line, message), now()))

    def finish(self, session_id: str | None = None,
               thread_id: str | None = None,
               cli_version: str | None = None) -> dict:
        with open(self.path, "rb") as fh:
            tail = _tail_sha(fh, self.end_offset) if self.end_offset else None
        fingerprint = hashlib.sha256(
            f"{self.end_offset}:{tail}".encode()).hexdigest()
        ordinal_max = getattr(self, "last_ordinal", self.row["ordinal_max"])
        self.con.execute(
            "UPDATE sources SET sha256=?, size_bytes=?, read_offset=?,"
            " tail_sha256=?, raw_bytes=?, ordinal_max=?, imported_at=?,"
            " import_ms=?, session_id=COALESCE(?, session_id),"
            " thread_id=COALESCE(?, thread_id),"
            " cli_version=COALESCE(?, cli_version) WHERE id=?",
            (fingerprint, self.size, self.end_offset, tail, self.end_offset,
             ordinal_max, now(),
             int((time.monotonic() - self.started) * 1000),
             session_id, thread_id, cli_version, self.source_id))
        return {"source_id": self.source_id, "sha256": fingerprint,
                "ordinal_max": ordinal_max, "incremental": self.incremental,
                "unchanged": self.unchanged}


def _safe_category(message: str) -> str:
    """Fixed safe error category for import_errors.error.

    The category is the leading token of the message (text before the
    first colon, first whitespace-separated token), lowercased and
    restricted to [a-z_] with at most 40 characters. Anything else
    fails closed to "import_error", so exception text, type values,
    IDs and other record values never reach the ledger.
    """
    raw = (message or "").split(":")[0].strip().split()
    token = raw[0].lower()[:40] if raw else ""
    if token and re.fullmatch(r"[a-z_]+", token):
        return token
    return "import_error"


def _redacted_excerpt(line: str, message: str) -> str:
    """Structural metadata only, never raw line text or record values.

    Contract rules 6 and 7: malformed lines may carry tool output, file
    contents or preference contents, so nothing from the line body or
    its values is stored. The excerpt holds the safe error category
    plus, when the line parses as a JSON object, its sanitized sorted
    top-level key names and the JSON shape, capped at 200 chars.
    Type, subtype, ID and other values are never stored.
    """
    category = _safe_category(message)
    if not line or not line.strip():
        return category[:200]
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return category[:200]
    if isinstance(obj, dict):
        try:
            keys = sorted(str(k) for k in obj.keys())
        except Exception:
            return category[:200]
        safe_keys = []
        for key in keys:
            cleaned = "".join(
                c if (c.isalnum() or c in ("_", "-", ".", "/")) else "_"
                for c in key)[:40]
            if cleaned:
                safe_keys.append(cleaned)
        parts = [category, f"keys={','.join(safe_keys)[:120]}"]
        return " ".join(parts)[:200]
    if isinstance(obj, list):
        return f"{category} json_type=list"[:200]
    return f"{category} json_type={type(obj).__name__}"[:200]


def insert_event(con: sqlite3.Connection, stats: dict, *, source_id: int,
                 session_key: str, family: str, native_id, ordinal=None,
                 ts=None, turn_id=None, name=None, target=None, status=None,
                 duration_ms=None, size_bytes=None, truncated=None,
                 fingerprint=None, detail=None) -> None:
    """Insert one event under its natural key; a repeat is a no-op."""
    if not native_id:
        raise ValueError(f"{family} event missing native identity")
    cur = con.execute(
        "INSERT OR IGNORE INTO events(source_id, session_key, ordinal_num, ts,"
        " family, native_id, turn_id, name, target, status, duration_ms,"
        " size_bytes, truncated, fingerprint, detail_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, session_key, ordinal, ts, family, str(native_id), turn_id,
         name, target, status, duration_ms, size_bytes, truncated, fingerprint,
         json.dumps(detail, sort_keys=True, default=str)[:4000]
         if detail else None))
    if cur.rowcount == 0:
        stats["events_duplicate"] = stats.get("events_duplicate", 0) + 1
    else:
        stats["events_inserted"] = stats.get("events_inserted", 0) + 1


def fingerprint(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def iso_ts(value):
    """ISO-8601 or epoch (s or ms) to epoch seconds; unknown stays None."""
    import datetime
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
