"""Source observations for task attribution of Codex token_usage_record atoms."""

import json
from collections import defaultdict

from .. import db, native_response
from ..ingest import iso_ts


FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens", "turn_total_tokens", "thread_total_tokens", "model")


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
        source_id INTEGER PRIMARY KEY REFERENCES sources(id),session_key TEXT NOT NULL,legacy_dispute INTEGER NOT NULL)""")
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


def finish(r):
    con, src = r.con, r.src
    incomplete = r.unreadable_atomic or src.incomplete_record or src.end_offset < src.size
    if incomplete:
        legacy = (src.row["codex_context_version"] == 7 and con.execute(
            "SELECT 1 FROM import_errors WHERE harness='codex' AND source_path=? AND error='usage_conflict' LIMIT 1",
            (src.path,)).fetchone() is not None)
        con.execute("INSERT INTO codex_incomplete_sources VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                    "legacy_dispute=MAX(legacy_dispute,excluded.legacy_dispute)", (src.source_id, r.session_key, legacy))
    elif not src.incremental:
        con.execute("DELETE FROM codex_incomplete_sources WHERE source_id=?", (src.source_id,))
    if not src.incremental:
        r.atomic_ids.update(rid for rid, in con.execute("SELECT DISTINCT response_id FROM prior_codex_atoms"))
        if incomplete:
            con.execute("INSERT OR IGNORE INTO codex_atomic_observations SELECT * FROM prior_codex_atoms")
        con.execute("DROP TABLE temp.prior_codex_atoms")
    for rid in r.atomic_ids:
        _reconcile(con, rid, src.source_id)


def _reconcile(con, rid, refreshing):
    copies = con.execute("""SELECT o.*,i.source_id AS incomplete FROM codex_atomic_observations o
        JOIN sources s ON s.id=o.source_id LEFT JOIN codex_incomplete_sources i ON i.source_id=o.source_id
        WHERE o.response_id=? ORDER BY s.path,o.position""", (rid,)).fetchall()
    signatures = {(o["session_key"], o["input_id"], o["measurement"]) for o in copies}
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
    turn = "codex:" + source["input_id"] if source["input_id"] else None
    if (row["source_id"] != source["source_id"] or row["turn_id"] != turn or
            any(row[k] != measured[k] for k in FIELDS)):
        con.execute("UPDATE responses SET " + ",".join(f"{k}=?" for k in FIELDS) +
                    ",source_id=?,turn_id=?,ordinal_num=?,ts=?,effort=?,thread_id=?,root_turn_id=?,session_id=? "
                    "WHERE response_id=?",
                    (*(measured[k] for k in FIELDS), source["source_id"], turn,
                     source["position"], source["ts"], source["effort"], source["thread_id"],
                     source["root_turn_id"], source["session_id"], rid))
    if source["input_id"]:
        for o in copies:
            native_response.record(con, o["source_id"], o["session_key"], rid, o["input_id"])


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
    columns = ",".join("r." + k for k in FIELDS)
    for o in con.execute("SELECT o.*,r.source_id AS measured_source,r.turn_id AS measured_turn," + columns + """
            FROM codex_atomic_observations o
            LEFT JOIN responses r ON r.response_id=o.response_id WHERE o.session_key=? OR o.response_id IN (
            SELECT response_id FROM responses WHERE session_key=? AND semantics=?)""",
            (session_key, session_key, semantics)):
        groups[o["response_id"]].append(o)
    conflicts = historical
    partial = {}
    for rid, copies in groups.items():
        variants = {(o["session_key"], o["input_id"], o["measurement"]) for o in copies}
        if (len(variants) > 1 or not copies[0]["measurement"] or
                copies[0]["measured_source"] not in {o["source_id"] for o in copies}):
            conflicts[rid] = {o["input_id"] for o in copies if o["input_id"]}
        else:
            values = json.loads(copies[0]["measurement"])
            turn = copies[0]["input_id"]
            inputs = {turn} if turn else set()
            if (copies[0]["measured_turn"] != ("codex:" + turn if turn else None)
                    or any(values[k] != copies[0][k] for k in FIELDS)):
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
