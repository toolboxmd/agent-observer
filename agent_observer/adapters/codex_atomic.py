"""Source observations for task attribution of Codex token_usage_record atoms."""

import hashlib
import json
import os
import sqlite3
from collections import defaultdict

from .. import db, lookup, native_response
from ..ingest import iso_ts


COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens", "turn_total_tokens", "thread_total_tokens")


def _signature(observation):
    # The turn and counters come from the atom. The surrounding model context
    # belongs to the source position and may differ for an exact repeat.
    measured = observation["measurement"]
    if measured:
        values = json.loads(measured)
        measured = tuple(values[k] for k in COUNTERS)
    return observation["session_key"], observation["input_id"], measured


def _prefix_digest(path, size):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while size:
            chunk = stream.read(min(size, 1024 * 1024))
            if not chunk:
                return None
            digest.update(chunk)
            size -= len(chunk)
    return digest.hexdigest()


def relocated_source(src):
    """Find an incomplete ledger source whose verified archive is this rollout."""
    path = os.path.abspath(src.path)
    if (not any(part in path for part in ("/sessions/", "/archived_sessions/"))
            or not db._has_table(src.con, "codex_incomplete_sources")):
        return None
    matches = []
    for row in src.con.execute("""SELECT s.*,i.* FROM codex_incomplete_sources i
            JOIN sources s ON s.id=i.source_id WHERE s.id!=? AND s.read_offset>0""", (src.source_id,)):
        if os.path.basename(row["path"]) != os.path.basename(path) or not row["tail_sha256"]:
            continue
        resolved = lookup.resolve_source(row)
        if resolved["reason"] != "relocated" or os.path.abspath(resolved["path"]) != path:
            continue
        row = dict(row)
        if row.get("prefix_bytes") is not None:
            if not row["prefix_digest"] or _prefix_digest(path, row["prefix_bytes"]) != row["prefix_digest"]:
                continue
        elif row["ino"] != src.ino:
            # Legacy receipts did not hash the torn line. Matching complete
            # records in a different file cannot identify its missing tail.
            continue
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


def source_columns(con, table):
    """Columns of ``table`` that name a ``sources`` row."""
    refs = {fk[3] for fk in con.execute(f'PRAGMA foreign_key_list("{table}")') if fk[2] == "sources"}
    return refs | {c[1] for c in con.execute(f'PRAGMA table_info("{table}")') if c[1] == "source_id"}


class UnclassifiedTable(sqlite3.DatabaseError):
    """A table names a source but has no key that says how to fold its rows."""


def _fold_action(con, table, column):
    """How a folded source's rows in ``table`` are handed to the adopted row.

    A row whose primary or unique key includes the source column belongs to
    that source's import and is deleted: the replay writes it again. A row
    with a key of its own is re-pointed. A table with no primary or unique key
    cannot be classified, so the fold stops instead of guessing: deleting
    could lose rows the replay never writes again, and re-pointing could
    duplicate rows it does.
    """
    keys = [[c[1] for c in sorted(con.execute(f'PRAGMA table_info("{table}")'), key=lambda c: c[5]) if c[5]]]
    for index in con.execute(f'PRAGMA index_list("{table}")'):
        if index[2]:
            keys.append([c[2] for c in con.execute(f'PRAGMA index_info("{index[1]}")')])
    keys = [k for k in keys if k]
    if not keys:
        raise UnclassifiedTable(f"cannot fold {table}.{column}: the table has no primary or unique key")
    return "delete" if any(column in k for k in keys) else "repoint"


def adopt_moved_source(con, path):
    """Make the moved file's older source row the one that stands for ``path``.

    Before context version 8 an import recorded no atomic observation, so its
    responses keep that source until the file is replayed. Codex later moves a
    finished rollout to ``archived_sessions`` with the same inode and bytes.
    When exactly one such row's recorded inode, name, size and the hash of the
    4 KiB before its read offset identify this file at its new path, that row
    takes the new path, as if the file had never moved. The import that
    follows then replays the row in place, so every version-, path- and
    thread-dependent rule applies to it unchanged. A row from before context
    versions is not adopted.

    A released version may already have imported the new path as its own
    source. That source is folded into the adopted row by the schema (see
    ``_fold_action``) and removed, and its Codex import errors at the new path
    are dropped for the replay to record again. Four things cannot return to
    the in-place state, so they stay as the separate import left them:

    - Its AUTOINCREMENT event and import-error ids are never reused, so
      ``event show`` and trace ids stay stable.
    - Rows only it added stay with the adopted row, since assignments,
      delegations and intervention evidence reference user messages.
    - Session-keyed rows without a source column stay, since nothing names
      their writer.
    - Checkout diagnostics it captured for events the older import lacked
      stay. The replay cannot capture them again, because the records are
      not new, and checkout diagnostics cannot grant ownership
      (``placement.read_placements``).
    """
    full = os.path.abspath(path)
    if not any(part in full for part in ("/sessions/", "/archived_sessions/")):
        return None
    try:
        ino = os.stat(path).st_ino
    except OSError:
        return None
    matches = []
    for row in _moved_before_atoms(con, path, ino):
        if os.path.basename(row["path"]) != os.path.basename(full):
            continue
        resolved = lookup.resolve_source(row)
        if resolved["reason"] == "relocated" and os.path.abspath(resolved["path"]) == full:
            matches.append(row)
    if len(matches) != 1:
        return None
    kept, old_path = matches[0]["id"], matches[0]["path"]
    current = con.execute("SELECT id FROM sources WHERE harness='codex' AND path=?", (path,)).fetchone()
    if current is not None:
        gone = current[0]
        tables = [t for t, in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name!='sources' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        # Classify every table before the first write.
        plan = [(table, column, _fold_action(con, table, column))
                for table in tables for column in sorted(source_columns(con, table))]
        for table, column, action in plan:
            if action == "delete":
                con.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (gone,))
            else:
                con.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?', (kept, gone))
        con.execute("DELETE FROM import_errors WHERE harness='codex' AND source_path=?", (path,))
        con.execute("DELETE FROM sources WHERE id=?", (gone,))
    con.execute("UPDATE import_errors SET source_path=? WHERE harness='codex' AND source_path=?", (path, old_path))
    con.execute("UPDATE sources SET path=? WHERE id=?", (path, kept))
    return kept


def _moved_before_atoms(con, path, ino):
    return con.execute("""SELECT * FROM sources WHERE harness='codex' AND ino=? AND path!=? AND read_offset>0
        AND tail_sha256 IS NOT NULL AND codex_context_version<8""", (ino, path)).fetchall()


def prepare(r):
    con = r.con
    con.execute("""CREATE TABLE IF NOT EXISTS codex_atomic_observations (
        source_id INTEGER NOT NULL REFERENCES sources(id), session_key TEXT NOT NULL, response_id TEXT NOT NULL,
        input_id TEXT NOT NULL, measurement TEXT NOT NULL, position INTEGER NOT NULL, ts REAL, effort TEXT,
        thread_id TEXT, root_turn_id TEXT, session_id TEXT,
        PRIMARY KEY(source_id,session_key,response_id,input_id,measurement)) WITHOUT ROWID""")
    con.execute("CREATE INDEX IF NOT EXISTS codex_atomic_response ON codex_atomic_observations(response_id,session_key)")
    con.execute("CREATE INDEX IF NOT EXISTS codex_atomic_session ON codex_atomic_observations(session_key,response_id)")
    con.execute("""CREATE TABLE IF NOT EXISTS codex_incomplete_sources (
        source_id INTEGER PRIMARY KEY REFERENCES sources(id),session_key TEXT NOT NULL,legacy_dispute INTEGER NOT NULL,
        replay INTEGER NOT NULL DEFAULT 1, prefix_bytes INTEGER, prefix_digest TEXT)""")
    columns = {c[1] for c in con.execute("PRAGMA table_info(codex_incomplete_sources)")}
    for key, declaration in (("replay", "INTEGER NOT NULL DEFAULT 1"), ("prefix_bytes", "INTEGER"), ("prefix_digest", "TEXT")):
        if key not in columns:
            con.execute(f"ALTER TABLE codex_incomplete_sources ADD COLUMN {key} {declaration}")
    r.atomic_ids = set()
    r.unreadable_atomic = False
    if not r.src.incremental:
        con.execute("CREATE TEMP TABLE prior_codex_atoms AS SELECT * FROM codex_atomic_observations WHERE source_id=?",
                    (r.src.source_id,))
        # A previous parser recorded the literal turn only after its counters
        # passed validation. On a damaged upgrade that turn is negative evidence.
        if r.src.row["codex_context_version"] == 7 and db._has_table(con, "native_response_inputs"):
            con.execute("""INSERT INTO prior_codex_atoms SELECT source_id,session_key,response_id,input_id,'',-1,NULL,NULL,NULL,NULL,NULL
                FROM native_response_inputs WHERE source_id=?""", (r.src.source_id,))
        con.execute("DELETE FROM codex_atomic_observations WHERE source_id=?", (r.src.source_id,))


def observe(r, obj, counters):
    p = obj["payload"]
    mid = native_response.identifier(p.get("response_id"))
    if not mid:
        r.unreadable_atomic = True
        return
    rid = "codex:" + mid
    turn = native_response.identifier(p.get("turn_id")) or ""
    if not (r.thread_id or r.session_id) or p.get("thread_id") not in (None, r.thread_id):
        counters = None
    payload = json.dumps(dict(counters, model=r.model), sort_keys=True) if counters is not None else ""
    r.atomic_ids.add(rid)
    r.con.execute("INSERT OR IGNORE INTO codex_atomic_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (r.src.source_id, r.session_key, rid, turn, payload, r.record_position,
                   iso_ts(obj.get("timestamp")), r.effort,
                   *(native_response.identifier(p.get(k)) for k in ("thread_id", "root_turn_id", "session_id"))))


def finish(r, predecessor):
    con, src = r.con, r.src
    incomplete = r.unreadable_atomic or src.incomplete_record or src.end_offset < src.size
    if incomplete:
        legacy = (src.row["codex_context_version"] == 7 and con.execute(
            "SELECT 1 FROM import_errors WHERE harness='codex' AND source_path=? AND error='usage_conflict' LIMIT 1",
            (src.path,)).fetchone() is not None)
        replay = r.unreadable_atomic or (not src.incremental and con.execute(
            "SELECT 1 FROM prior_codex_atoms LIMIT 1").fetchone() is not None)
        con.execute("INSERT INTO codex_incomplete_sources(source_id,session_key,legacy_dispute,replay,"
                    "prefix_bytes,prefix_digest) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                    "session_key=excluded.session_key,legacy_dispute=MAX(legacy_dispute,excluded.legacy_dispute),"
                    "replay=excluded.replay,prefix_bytes=excluded.prefix_bytes,prefix_digest=excluded.prefix_digest",
                    (src.source_id, r.session_key, legacy, replay, src.size, _prefix_digest(src.path, src.size)))
    else:
        if predecessor is not None and predecessor["session_key"] == r.session_key and (
                predecessor.get("prefix_digest") or _historical_atoms_survive(con, predecessor["id"], src.source_id)):
            # The archive was read from the start. Its source now owns the current
            # observations, including withdrawals. Keep the old raw ledger locators.
            old = predecessor["id"]
            r.atomic_ids.update(rid for rid, in con.execute(
                "SELECT DISTINCT response_id FROM codex_atomic_observations WHERE source_id=?", (old,)))
            con.execute("DELETE FROM codex_atomic_observations WHERE source_id=?", (old,))
            con.execute("DELETE FROM native_response_inputs WHERE source_id=?", (old,))
            con.execute("DELETE FROM codex_incomplete_sources WHERE source_id=?", (old,))
            con.execute("UPDATE sources SET codex_context_offset=NULL WHERE id=?", (old,))
        cleared = con.execute("DELETE FROM codex_incomplete_sources WHERE source_id=?", (src.source_id,)).rowcount
        if cleared and src.incremental:
            # Another source may have withdrawn a conflicting copy while this
            # tail was unfinished. Revisit its stored atoms without rereading the file.
            r.atomic_ids.update(rid for rid, in con.execute(
                "SELECT DISTINCT response_id FROM codex_atomic_observations WHERE source_id=?", (src.source_id,)))
    if not src.incremental:
        r.atomic_ids.update(rid for rid, in con.execute("SELECT DISTINCT response_id FROM prior_codex_atoms"))
        if incomplete:
            con.execute("INSERT OR IGNORE INTO codex_atomic_observations SELECT * FROM prior_codex_atoms")
        con.execute("DROP TABLE temp.prior_codex_atoms")
    for rid in r.atomic_ids:
        _reconcile(con, rid, src.source_id)


def _historical_atoms_survive(con, original, archive):
    # Older incomplete receipts have only the tail hash at the read offset.
    # A same-name copy must additionally account for every observed atom.
    def observations(source_id):
        return {(o["response_id"], _signature(o)) for o in con.execute(
            "SELECT * FROM codex_atomic_observations WHERE source_id=?", (source_id,))}
    previous = observations(original)
    return bool(previous) and previous <= observations(archive)


def _reconcile(con, rid, refreshing):
    copies = con.execute("""SELECT o.*,i.source_id AS incomplete FROM codex_atomic_observations o
        JOIN sources s ON s.id=o.source_id LEFT JOIN codex_incomplete_sources i ON i.source_id=o.source_id
        WHERE o.response_id=? ORDER BY s.path,o.position""", (rid,)).fetchall()
    signatures = {_signature(o) for o in copies}
    if len(signatures) != 1 or not copies[0]["measurement"] or any(o["incomplete"] for o in copies):
        return
    first = copies[0]
    row = con.execute("SELECT r.*,s.codex_context_version AS version FROM responses r JOIN sources s "
                      "ON s.id=r.source_id WHERE r.response_id=?", (rid,)).fetchone()
    if row is None or row["session_key"] != first["session_key"]:
        return
    source = next((o for o in copies if o["source_id"] == row["source_id"]), first)
    if source["source_id"] != row["source_id"] and row["source_id"] != refreshing and (row["version"] or 0) < 8:
        return  # The original pre-upgrade source has not been replayed.
    measured = json.loads(source["measurement"])
    same_source = source["source_id"] == row["source_id"]
    if (same_source and len({o["source_id"] for o in copies}) == 1
            and any(row[k] is not None and row[k] != measured[k] for k in COUNTERS)):
        # One rewritten native atom cannot independently revise immutable
        # counters. The task view withholds it while the raw ledger keeps it.
        return
    turn = "codex:" + source["input_id"] if source["input_id"] else None
    if not same_source or row["turn_id"] != turn or any(row[k] != measured[k] for k in COUNTERS):
        con.execute("UPDATE responses SET " + ",".join(f"{k}=?" for k in COUNTERS) +
                    ",source_id=?,turn_id=?,ordinal_num=?,ts=?,model=?,effort=?,thread_id=?,root_turn_id=?,session_id=? "
                    "WHERE response_id=?",
                    (*(measured[k] for k in COUNTERS), source["source_id"], turn,
                     source["position"], source["ts"], row["model"] if same_source else measured["model"],
                     row["effort"] if same_source else source["effort"], source["thread_id"],
                     source["root_turn_id"], source["session_id"], rid))
    if source["input_id"]:
        for o in copies:
            native_response.record(con, o["source_id"], o["session_key"], rid, o["input_id"])


def unmeasured_locations(con, session_key):
    """Identified native atoms without a usage row in this session."""
    if db._has_table(con, "codex_atomic_observations"):
        yield from con.execute(
            "SELECT o.response_id,o.source_id,NULLIF(o.position,-1) AS ordinal_num,o.ts,"
            " CASE WHEN o.input_id<>'' THEN 'codex:'||o.input_id END AS turn_id"
            " FROM codex_atomic_observations o JOIN sources s ON s.id=o.source_id"
            " WHERE o.session_key=? AND NOT EXISTS (SELECT 1 FROM responses r"
            " WHERE r.session_key=o.session_key AND r.response_id=o.response_id)"
            " ORDER BY s.path,o.position", (session_key,))


def agreed_turn_ids(con, session_key):
    """Recorded turn identity can agree even when a measurement is disputed."""
    if not db._has_table(con, "codex_atomic_observations"):
        return {}
    return dict(con.execute(
        "SELECT o.response_id,'codex:'||MIN(o.input_id)"
        " FROM codex_atomic_observations o"
        " WHERE o.response_id IN (SELECT response_id FROM codex_atomic_observations WHERE session_key=?)"
        " GROUP BY o.response_id"
        " HAVING MIN(o.session_key)=? AND MAX(o.session_key)=?"
        " AND MIN(o.input_id)<>'' AND MIN(o.input_id)=MAX(o.input_id)",
        (session_key, session_key, session_key)))


def disputes(con, session_key, semantics):
    """Only current agreeing source measurements can support positive ownership."""
    if "codex_context_version" not in {c[1] for c in con.execute("PRAGMA table_info(sources)")}:
        return {}, False, {}
    legacy = con.execute("""SELECT 1 FROM sources s JOIN import_errors e ON e.source_path=s.path
        WHERE s.harness='codex' AND e.harness='codex' AND e.error='usage_conflict' AND s.codex_context_version=7
        AND 'codex:'||COALESCE(s.thread_id,s.session_id)=? LIMIT 1""", (session_key,)).fetchone() is not None
    historical = {}
    if legacy:
        historical = {rid: set() for rid, in con.execute("SELECT response_id FROM responses WHERE session_key=? "
                                                         "AND semantics=?", (session_key, semantics))}
    if not db._has_table(con, "codex_atomic_observations"):
        return historical, legacy, {}
    groups = defaultdict(list)
    columns = ",".join("r." + k for k in COUNTERS)
    for o in con.execute("SELECT o.*,r.source_id AS measured_source,r.turn_id AS measured_turn," + columns + """
            FROM codex_atomic_observations o
            LEFT JOIN responses r ON r.response_id=o.response_id WHERE o.session_key=? OR o.response_id IN (
            SELECT response_id FROM responses WHERE session_key=? AND semantics=?)""",
            (session_key, session_key, semantics)):
        groups[o["response_id"]].append(o)
    conflicts = historical
    partial = {}
    for rid, copies in groups.items():
        variants = {_signature(o) for o in copies}
        if (len(variants) > 1 or not copies[0]["measurement"] or
                copies[0]["measured_source"] not in {o["source_id"] for o in copies}):
            conflicts[rid] = {o["input_id"] for o in copies if o["input_id"]}
        else:
            values = json.loads(copies[0]["measurement"])
            turn = copies[0]["input_id"]
            inputs = {turn} if turn else set()
            if (copies[0]["measured_turn"] != ("codex:" + turn if turn else None)
                    or any(values[k] != copies[0][k] for k in COUNTERS)):
                conflicts[rid] = inputs
            elif any(values[k] is None for k in ("input_tokens", "output_tokens", "total_tokens")):
                partial[rid] = inputs
    for rid, in con.execute("""SELECT r.response_id FROM responses r JOIN sources s ON s.id=r.source_id
            WHERE r.session_key=? AND r.semantics=? AND s.codex_context_version>=8 AND NOT EXISTS (
            SELECT 1 FROM codex_atomic_observations o WHERE o.response_id=r.response_id AND o.session_key=r.session_key)""",
            (session_key, semantics)):
        conflicts[rid] = set()
    incomplete, unidentified = con.execute("SELECT COUNT(*),MAX(legacy_dispute) FROM codex_incomplete_sources "
                                           "WHERE session_key=?", (session_key,)).fetchone()
    if unidentified:
        for rid, copies in groups.items():
            conflicts[rid] = {o["input_id"] for o in copies if o["input_id"]}
    return conflicts, bool(incomplete or legacy), partial
