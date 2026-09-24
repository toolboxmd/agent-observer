"""Source bookkeeping shared by the file adapters.

Append-only JSONL logs resume at the offset the previous import reached when
the bytes just before that offset are unchanged; otherwise the whole file is
read again. Either way every row lands under its natural key, so re-reading
never adds usage twice. A trailing line without a newline is left for the
next import because a live harness may still be writing it.

Change detection is cheap on purpose: the stored file size, mtime_ns and
inode plus the hash of the final 4 KiB before the recorded offset decide
whether the prefix is intact. A same-size prefix rewrite changes the mtime,
an inode replacement changes the inode, and a shrink changes the size, so
each forces a full re-read from offset zero instead of skipping rewritten
bytes. Adapters recheck size and mtime (plus inode) immediately before any
unchanged fast-path return so an append racing the first check is imported
on that sync instead of skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time

from . import privacy
from .db import now

TAIL_BYTES = 4096


class MissingNativeId(ValueError):
    """An event without native identity: quarantined as missing_id."""


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
        st = os.stat(path)
        self.size = st.st_size
        self.mtime_ns = st.st_mtime_ns
        self.ino = st.st_ino
        self.full = full
        self.started = time.monotonic()
        row = con.execute(
            "SELECT * FROM sources WHERE harness=? AND path=?",
            (harness, path)).fetchone()
        self.row = row
        self.start_offset = 0
        self.unchanged = False
        stored_version = (row["privacy_version"]
                          if row is not None and "privacy_version" in row.keys()
                          else None)
        # Rule 3: a source imported under older privacy rules is fully
        # re-imported; existing rows are updated in place and that source's
        # import_errors are replaced instead of duplicated.
        self.privacy_stale = stored_version != privacy.PRIVACY_VERSION
        if row is not None and self.privacy_stale:
            con.execute(
                "DELETE FROM import_errors WHERE harness=? AND source_path=?",
                (harness, path))
        if row is not None and not full and not self.privacy_stale:
            self.start_offset, self.unchanged = self._check_at(
                row, self.size, self.mtime_ns, self.ino)
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

    def _check_at(self, row, size: int, mtime_ns: int,
                  ino: int) -> tuple[int, bool]:
        """Decide (start_offset, unchanged) for one stat snapshot.

        Pure comparison helper shared by __init__ and recheck_unchanged so
        both use the same invalidation rule. Missing stored mtime/inode
        (ledgers imported before they were recorded) fails closed to a
        full re-read. Any mismatch forces start_offset 0 except a grown
        file whose recorded prefix still validates, which resumes
        incrementally at that offset.
        """
        try:
            offset = row["read_offset"] or 0
        except (KeyError, TypeError, IndexError):
            return 0, False
        try:
            stored_tail = row["tail_sha256"]
        except (KeyError, TypeError, IndexError):
            stored_tail = None
        try:
            stored_size = row["size_bytes"]
        except (KeyError, TypeError, IndexError):
            stored_size = None
        try:
            stored_mtime = (row["mtime_ns"]
                            if "mtime_ns" in row.keys() else None)
        except (TypeError, IndexError):
            stored_mtime = None
        try:
            stored_ino = (row["ino"] if "ino" in row.keys() else None)
        except (TypeError, IndexError):
            stored_ino = None
        if (not isinstance(offset, int) or offset <= 0 or not stored_tail
                or stored_mtime is None or stored_ino is None
                or not isinstance(stored_size, int)):
            return 0, False
        if ino != stored_ino:
            # Same path, replaced file: never skip bytes.
            return 0, False
        if size < offset:
            # Truncated: the recorded offset is past EOF.
            return 0, False
        if size < stored_size:
            # Shrank since the last import: full re-read.
            return 0, False
        if size == stored_size and mtime_ns != stored_mtime:
            # Same-size rewrite (the changed prefix may sit outside the
            # recorded tail): full re-read without trusting the old offset.
            return 0, False
        with open(self.path, "rb") as fh:
            if _tail_sha(fh, offset) != stored_tail:
                return 0, False
        if size == stored_size and mtime_ns == stored_mtime:
            return offset, offset == size
        if size > stored_size and size >= offset:
            # Grown with an intact prefix: resume incrementally.
            return offset, False
        return 0, False

    def recheck_unchanged(self) -> bool:
        """Re-stat and revalidate immediately before a fast-path return.

        Deterministic seam for the append racing the first check: adapters
        call this right before returning unchanged, and tests wrap it to
        append one valid record between the initial check and this one.
        Refreshes size/mtime/ino and the start decision; a raced append
        (or same-size rewrite) clears unchanged so the caller falls
        through to the import path with the corrected offset. No SQL.
        """
        if self.full or self.privacy_stale or self.row is None:
            self.start_offset = 0
            self.unchanged = False
            return False
        try:
            st = os.stat(self.path)
        except OSError:
            self.start_offset = 0
            self.unchanged = False
            return False
        self.size = st.st_size
        self.mtime_ns = st.st_mtime_ns
        self.ino = st.st_ino
        try:
            self.start_offset, self.unchanged = self._check_at(
                self.row, self.size, self.mtime_ns, self.ino)
        except OSError:
            self.start_offset = 0
            self.unchanged = False
            return False
        return self.unchanged

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

    def error(self, ordinal, category: str, line: str = "") -> None:
        # Privacy rules 4 and 5 via agent_observer/privacy.py: the error
        # column holds exactly one closed category (anything else maps to
        # the fallback), and the excerpt holds only sorted top-level key
        # names. The same record re-read under the same version never adds
        # a duplicate row. Deduplication is NULL-safe: a record whose
        # native ordinal is absent (None) is keyed with IS NULL, so callers
        # must still prefer the structural source ordinal they were
        # yielded, which is never None.
        safe = privacy.error_category(category)
        if ordinal is None:
            exists = self.con.execute(
                "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
                " AND ordinal_num IS NULL AND error=?",
                (self.harness, self.path, safe)).fetchone()
        else:
            exists = self.con.execute(
                "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
                " AND ordinal_num=? AND error=?",
                (self.harness, self.path, ordinal, safe)).fetchone()
        if exists is not None:
            return
        self.con.execute(
            "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
            " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
            (self.harness, self.path, ordinal, safe,
             privacy.line_excerpt(line), now()))

    def finish(self, session_id: str | None = None,
               thread_id: str | None = None,
               cli_version: str | None = None,
               thread_source: str | None = None) -> dict:
        # Fresh stat at finish: the file may have grown while records were
        # parsed, so the persisted size/mtime/ino describe the current file,
        # not the import-start snapshot. Only offsets and hashes persist;
        # no file contents reach the ledger.
        try:
            st = os.stat(self.path)
            self.size = st.st_size
            self.mtime_ns = st.st_mtime_ns
            self.ino = st.st_ino
        except OSError:
            pass
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
            " cli_version=COALESCE(?, cli_version),"
            " thread_source=COALESCE(?, thread_source),"
            " privacy_version=?, mtime_ns=?, ino=? WHERE id=?",
            (fingerprint, self.size, self.end_offset, tail, self.end_offset,
             ordinal_max, now(),
             int((time.monotonic() - self.started) * 1000),
             session_id, thread_id, cli_version, thread_source,
             privacy.PRIVACY_VERSION, self.mtime_ns, self.ino,
             self.source_id))
        return {"source_id": self.source_id, "sha256": fingerprint,
                "ordinal_max": ordinal_max, "incremental": self.incremental,
                "unchanged": self.unchanged}


def insert_event(con: sqlite3.Connection, stats: dict, *, source_id: int,
                 session_key: str, family: str, native_id, ordinal=None,
                 ts=None, turn_id=None, name=None, target=None, status=None,
                 duration_ms=None, size_bytes=None, truncated=None,
                 fingerprint=None, detail=None, update: bool = False) -> None:
    """Insert one event under its natural key; a repeat is a no-op.

    Every protected field passes through agent_observer/privacy.py rule 6:
    native_id must be a non-empty string of the family-expected type (never
    stringified; a wrong type raises MissingNativeId and is quarantined as
    missing_id), name and status must belong to the family's closed sets,
    and only allowlisted detail keys with correctly typed values persist.
    When update is true (a source re-imported under newer privacy rules),
    an existing row's name, target, status and detail are corrected in
    place instead of kept stale.
    """
    native = privacy.filter_native_id(family, native_id)
    if not native:
        raise MissingNativeId(f"{family} event missing native identity")
    safe_name = privacy.filter_event_name(family, name)
    safe_status = privacy.filter_event_status(family, status)
    safe_target = privacy.filter_target(target, family)
    filtered = privacy.filter_detail(family, detail)
    # The size bound applies before serialization: shrinking list values
    # (only paths can grow large) keeps detail_json always valid JSON.
    # Slicing serialized JSON could leave an invalid truncated value.
    payload = json.dumps(filtered, sort_keys=True) if filtered else None
    while payload is not None and len(payload) > privacy.DETAIL_JSON_CHARS:
        big = [key for key in filtered
               if isinstance(filtered[key], list) and filtered[key]]
        if not big:
            payload = None
            break
        key = max(big, key=lambda k: len(filtered[k]))
        filtered[key] = filtered[key][:len(filtered[key]) // 2]
        if not filtered[key]:
            del filtered[key]
        payload = json.dumps(filtered, sort_keys=True) if filtered else None
    cur = con.execute(
        "INSERT OR IGNORE INTO events(source_id, session_key, ordinal_num, ts,"
        " family, native_id, turn_id, name, target, status, duration_ms,"
        " size_bytes, truncated, fingerprint, detail_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, session_key, ordinal, ts, family, native, turn_id,
         safe_name, safe_target, safe_status, duration_ms, size_bytes,
         truncated, fingerprint, payload))
    if cur.rowcount == 0:
        if update:
            con.execute(
                "UPDATE events SET name=?, target=?, status=?, detail_json=?"
                " WHERE session_key=? AND family=? AND native_id=?",
                (safe_name, safe_target, safe_status, payload, session_key,
                 family, native))
            stats["events_updated"] = stats.get("events_updated", 0) + 1
        else:
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
