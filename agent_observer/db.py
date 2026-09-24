"""SQLite ledger schema and helpers. Stdlib only.

Every harness writes the same tables. Natural keys carry a harness prefix
(`claude:`, `codex:`, `opencode:`, `grok:`, `router:`) so identities from
different harnesses never collide. Rows are keyed by session, not by source
file, so a re-read, a grown log, or a copied file never adds usage twice.
"""

from __future__ import annotations

import os
import sqlite3
import time

from . import CAPTURE_CONTRACT_VERSION, EVENT_CONTRACT_VERSION, SCHEMA_VERSION

DEFAULT_DB = os.path.join(
    os.path.expanduser("~"), ".local", "state", "agent-observer", "observer.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- One row per native source unit: a session file, or one session inside a
-- harness database (locator 'opencode.db#<id>'). The fingerprint identifies
-- the imported snapshot; read_offset and tail_sha256 let append-only logs
-- resume where the previous import stopped. size_bytes, mtime_ns and ino
-- (all nullable for ledgers written before they were recorded) join the
-- tail hash in the cheap unchanged check: a same-size rewrite changes the
-- mtime, a replaced file changes the inode, and a shrink changes the size.
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  read_offset INTEGER NOT NULL DEFAULT 0,
  tail_sha256 TEXT,
  mtime_ns INTEGER,
  ino INTEGER,
  cli_version TEXT,
  session_id TEXT,
  thread_id TEXT,
  thread_source TEXT,
  ordinal_max INTEGER NOT NULL DEFAULT -1,
  raw_bytes INTEGER NOT NULL DEFAULT 0,
  imported_at REAL NOT NULL,
  import_ms INTEGER,
  privacy_version INTEGER,
  UNIQUE (harness, path)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  harness TEXT NOT NULL,
  native_id TEXT NOT NULL,
  source_id INTEGER REFERENCES sources(id),
  parent_session_key TEXT,
  role TEXT,
  project_dir TEXT,
  git_branch TEXT,
  client_version TEXT,
  entrypoint TEXT,
  title TEXT,
  started_at REAL,
  ended_at REAL,
  agentsmd_version TEXT,
  instructions_sha256 TEXT,
  preferences_sha256 TEXT,
  direction_status TEXT,
  identity_json TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id),
  session_key TEXT,
  root_turn_id TEXT,
  session_id TEXT,
  model_observed TEXT,
  effort_observed TEXT,
  started_at REAL,
  completed_at REAL,
  duration_ms INTEGER,
  state TEXT
);
-- One row per usage-bearing model response. Raw counters keep the native
-- meaning named by `semantics`; total_tokens is the harness's own total
-- under that meaning. Cached input and reasoning are not universally
-- additive, so reports never add columns across different semantics.
CREATE TABLE IF NOT EXISTS responses (
  response_id TEXT PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  harness TEXT NOT NULL DEFAULT 'codex',
  session_key TEXT,
  thread_id TEXT,
  turn_id TEXT,
  root_turn_id TEXT,
  session_id TEXT,
  ordinal_num INTEGER,
  ts REAL,
  model TEXT,
  provider TEXT,
  effort TEXT,
  input_tokens INTEGER,
  cached_input_tokens INTEGER,
  cache_write_input_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_output_tokens INTEGER,
  total_tokens INTEGER,
  turn_total_tokens INTEGER,
  thread_total_tokens INTEGER,
  semantics TEXT,
  cost_usd REAL,
  is_overlap INTEGER NOT NULL DEFAULT 0
);
-- User-role inputs. kind: genuine (typed by a person), synthetic (tool
-- results, question replies, reminders), scaffolding (skill or environment
-- injection), interrupt (a person stopped the turn).
CREATE TABLE IF NOT EXISTS submissions (
  native_id TEXT PRIMARY KEY,
  alias_id TEXT,
  source_id INTEGER REFERENCES sources(id),
  session_key TEXT,
  turn_id TEXT,
  ordinal_num INTEGER,
  ts REAL,
  kind TEXT NOT NULL DEFAULT 'genuine',
  text_hash TEXT NOT NULL,
  text_excerpt TEXT NOT NULL,
  is_genuine INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  project TEXT,
  family TEXT,
  title TEXT,
  issue_url TEXT,
  origin TEXT NOT NULL DEFAULT 'capture',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
  submission_native_id TEXT NOT NULL REFERENCES submissions(native_id),
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt TEXT,
  phase TEXT,
  evidence TEXT,
  shared INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  PRIMARY KEY (submission_native_id, task_id)
);
-- Whole-session ownership, for sessions wholly assigned to one task (a
-- runner job's worker and dispatcher sessions). Evidence names the record
-- that proves the binding.
CREATE TABLE IF NOT EXISTS session_assignments (
  session_key TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  evidence TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (session_key, task_id)
);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owning_submission TEXT REFERENCES submissions(native_id),
  parent_thread TEXT,
  parent_turn TEXT,
  worker_thread TEXT NOT NULL,
  worker_turn TEXT,
  requested_model TEXT,
  requested_effort TEXT,
  policy_version TEXT,
  reason TEXT,
  task_name TEXT,
  created_at REAL NOT NULL,
  UNIQUE (owning_submission, worker_thread)
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(task_id),
  turn_id TEXT NOT NULL,
  role TEXT NOT NULL,
  harness TEXT,
  session_key TEXT,
  stage TEXT,
  route_requested TEXT,
  policy_version TEXT,
  reason TEXT,
  model_requested TEXT,
  model_observed TEXT,
  effort_requested TEXT,
  effort_observed TEXT,
  started_at REAL,
  ended_at REAL,
  elapsed_s REAL,
  state TEXT NOT NULL DEFAULT 'active',
  terminal_class TEXT,
  usable_output INTEGER,
  usage_json TEXT,
  UNIQUE (task_id, turn_id)
);
CREATE TABLE IF NOT EXISTS outcomes (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  candidate TEXT,
  proof_ref TEXT,
  acceptance_state TEXT NOT NULL DEFAULT 'unknown',
  repairs TEXT,
  corrections TEXT,
  updated_at REAL NOT NULL
);
-- Operational events. family: tool_call, tool_result, read, skill_read,
-- skill_invoke, file_change, compaction, lifecycle, assistant_message,
-- permission, instructions. Natural key is per session so the same native
-- event read from two snapshots is stored once.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER REFERENCES sources(id),
  session_key TEXT NOT NULL DEFAULT '',
  ordinal_num INTEGER,
  ts REAL,
  family TEXT NOT NULL,
  native_id TEXT NOT NULL,
  turn_id TEXT,
  name TEXT,
  target TEXT,
  status TEXT,
  duration_ms INTEGER,
  size_bytes INTEGER,
  truncated INTEGER,
  fingerprint TEXT,
  detail_json TEXT,
  UNIQUE (session_key, family, native_id)
);
-- Model Router ledger rows, copied read-only from its jobs.db and guarded by
-- its schema_version. Observer never writes the router's database.
CREATE TABLE IF NOT EXISTS router_jobs (
  request_id TEXT PRIMARY KEY,
  status TEXT,
  lane TEXT,
  job_kind TEXT,
  replay_of TEXT,
  workspace TEXT,
  issue TEXT,
  planner_session_id TEXT,
  planner_model TEXT,
  planner_harness TEXT,
  base_commit TEXT,
  head_commit TEXT,
  block_reason TEXT,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS router_invocations (
  invocation_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  kind TEXT,
  stage TEXT,
  requested_route TEXT,
  policy_version TEXT,
  reason TEXT,
  terminal_class TEXT,
  harness_version TEXT,
  observed_model TEXT,
  observed_variant TEXT,
  elapsed_secs REAL,
  usage_json TEXT,
  native_ids_json TEXT,
  session_id TEXT,
  session_kind TEXT,
  started_at REAL,
  ended_at REAL,
  kit TEXT,
  kit_hash TEXT,
  direction_supply TEXT,
  direction_hash TEXT,
  skills_json TEXT,
  tools_json TEXT,
  schema_version INTEGER
);
CREATE TABLE IF NOT EXISTS router_readings (
  pool TEXT NOT NULL,
  model TEXT NOT NULL,
  window TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  used REAL,
  limit_value REAL,
  reset_at TEXT,
  source TEXT,
  PRIMARY KEY (pool, model, window, observed_at)
);
-- AGENTS.md SHA-256 to AgentsMD release, built from the local AgentsMD
-- repository's tags. A hash with no tag stays unresolved in reports.
CREATE TABLE IF NOT EXISTS agentsmd_versions (
  sha256 TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  commit_sha TEXT,
  released_at REAL,
  size_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS import_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT,
  source_path TEXT NOT NULL,
  ordinal_num INTEGER,
  error TEXT NOT NULL,
  line_excerpt TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_session ON responses(session_key);
CREATE INDEX IF NOT EXISTS idx_responses_reconcile ON responses(session_key, semantics, is_overlap, thread_total_tokens, total_tokens);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_key, ts);
CREATE INDEX IF NOT EXISTS idx_events_family ON events(family);
CREATE INDEX IF NOT EXISTS idx_submissions_session ON submissions(session_key);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_dir);
CREATE INDEX IF NOT EXISTS idx_router_inv_request ON router_invocations(request_id);
"""

OUTCOME_STATES = (
    "complete",
    "active",
    "cancelled",
    "failed",
    "quota_blocked",
    "crashed",
    "unknown",
)

HARNESSES = ("codex", "claude", "opencode", "grok", "router")


def default_path() -> str:
    return os.environ.get("AGENT_OBSERVER_DB") or DEFAULT_DB


def _harden_path(path: str) -> None:
    """Tighten the ledger directory to 0700 and db sidecars to 0600.

    Applied to existing paths as well as new ones. Exact modes are set
    so a permissive umask cannot leave the private ledger readable by
    other local users; SQLite keeps working because the owner retains
    read/write and directory search permission.
    """
    parent = os.path.dirname(os.path.abspath(path))
    try:
        if os.path.isdir(parent):
            os.chmod(parent, 0o700)
    except OSError:
        pass
    for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)
        except OSError:
            pass


def connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    _harden_path(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    _harden_path(path)
    return con


def init_db(con: sqlite3.Connection) -> None:
    existing = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if existing:
        row = con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row and int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger schema {row['value']} is not {SCHEMA_VERSION}; "
                "move the old ledger aside and sync again")
    con.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in con.execute("PRAGMA table_info(sources)")}
    if "privacy_version" not in columns:
        con.execute("ALTER TABLE sources ADD COLUMN privacy_version INTEGER")
    if "thread_source" not in columns:
        con.execute("ALTER TABLE sources ADD COLUMN thread_source TEXT")
    if "mtime_ns" not in columns:
        con.execute("ALTER TABLE sources ADD COLUMN mtime_ns INTEGER")
    if "ino" not in columns:
        con.execute("ALTER TABLE sources ADD COLUMN ino INTEGER")
    con.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
        "('schema_version', ?), ('event_contract_version', ?), "
        "('capture_contract_version', ?)",
        (str(SCHEMA_VERSION), str(EVENT_CONTRACT_VERSION),
         str(CAPTURE_CONTRACT_VERSION)),
    )
    con.commit()
    try:
        row = con.execute("PRAGMA database_list").fetchone()
        if row is not None:
            main_file = row["file"] if "file" in row.keys() else row[2]
            if main_file:
                _harden_path(main_file)
    except (OSError, sqlite3.DatabaseError):
        pass


def now() -> float:
    return time.time()


def _source_imported_before_privacy(row: sqlite3.Row) -> bool:
    """Whether a sources row was imported under older privacy rules.

    A stored version that differs from the current one is stale. A NULL
    version is stale only when the row already imported content (a
    non-empty fingerprint, a nonzero read offset, a recorded ordinal or
    recorded bytes): a row just created for a never-imported file carries
    no old content to replace, so it must not trigger replacement.
    """
    from . import privacy
    try:
        keys = row.keys()
    except (AttributeError, TypeError):
        return False

    def _col(name: str):
        try:
            return row[name] if name in keys else None
        except (KeyError, TypeError, IndexError):
            return None
    version = _col("privacy_version")
    if version == privacy.PRIVACY_VERSION:
        return False
    if version is not None:
        return True
    offset = _col("read_offset")
    if isinstance(offset, int) and offset > 0:
        return True
    ordinal_max = _col("ordinal_max")
    if isinstance(ordinal_max, int) and ordinal_max >= 0:
        return True
    sha = _col("sha256")
    if isinstance(sha, str) and sha:
        return True
    size_bytes = _col("size_bytes")
    if isinstance(size_bytes, int) and size_bytes > 0:
        return True
    return False


def session_sources_stale(con: sqlite3.Connection, session_key: str,
                          harness: str, native_id: str,
                          source_id: int | None = None) -> bool:
    """Whether any source backing this session predates current privacy rules.

    The check is centralized here so no adapter decides identity
    replacement: it covers every source row associated with the session,
    not only the single source_id one adapter selected. That includes
    multi-source sessions such as Grok, where updates.jsonl and
    events.jsonl are distinct source rows and either one may be stale.
    Sources are associated through the session row's stored source_id,
    through the session's existing event, submission, response and turn
    rows, through sibling rows sharing the native session id, and through
    the incoming source_id. Unknown sessions need no replacement.
    """
    try:
        known = con.execute(
            "SELECT source_id FROM sessions WHERE session_key=?",
            (session_key,)).fetchone()
    except sqlite3.DatabaseError:
        return False
    if known is None:
        return False
    candidate_ids: set[int] = set()
    if isinstance(source_id, int):
        candidate_ids.add(source_id)
    try:
        stored = known["source_id"]
    except (KeyError, TypeError, IndexError):
        stored = None
    if isinstance(stored, int):
        candidate_ids.add(stored)
    for table in ("events", "submissions", "responses", "turns"):
        try:
            rows = con.execute(
                f"SELECT DISTINCT source_id FROM {table} WHERE session_key=?",
                (session_key,)).fetchall()
        except sqlite3.DatabaseError:
            continue
        for row in rows:
            try:
                sid = row["source_id"]
            except (KeyError, TypeError, IndexError):
                continue
            if isinstance(sid, int):
                candidate_ids.add(sid)
    if isinstance(native_id, str) and native_id:
        try:
            rows = con.execute(
                "SELECT id FROM sources WHERE harness=? AND session_id=?",
                (harness, native_id)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for row in rows:
            try:
                sid = row["id"]
            except (KeyError, TypeError, IndexError):
                continue
            if isinstance(sid, int):
                candidate_ids.add(sid)
    for sid in candidate_ids:
        try:
            srow = con.execute("SELECT * FROM sources WHERE id=?",
                               (sid,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if srow is None:
            continue
        if _source_imported_before_privacy(srow):
            return True
    return False


# Source-owned session fields replaced (never COALESCE-kept) when any of
# the session's sources was imported under older privacy rules. Identity
# columns from identity.fields, the project and client fields adapters
# re-derive on every import, and the parent link and role all come from
# the current import result; omitted or invalid values clear to NULL so
# pre-sanitization content cannot survive. Timestamps keep their MIN/MAX
# merge below; titles are never stored (rule 7) and clear to NULL.
_IDENTITY_REPLACE_FIELDS = frozenset({
    "parent_session_key", "role", "project_dir", "git_branch",
    "client_version", "entrypoint", "agentsmd_version",
    "instructions_sha256", "preferences_sha256", "direction_status",
    "identity_json",
})


def upsert_session(con: sqlite3.Connection, session_key: str, harness: str,
                   native_id: str, source_id: int | None = None,
                   **fields) -> None:
    """Create a session row or fill its unknown fields.

    Known values are never overwritten by unknown ones; a later snapshot may
    extend ended_at and fill fields the earlier snapshot lacked.

    On a privacy-version stale re-import the source-owned identity fields
    are replaced, never COALESCE-kept: omitted or invalid values clear to
    NULL so pre-sanitization content cannot survive. Staleness is inferred
    centrally from the session's source rows (see session_sources_stale),
    never from an adapter flag. Normal imports keep fill-unknown behavior.
    """
    allowed = {"parent_session_key", "role", "project_dir", "git_branch",
               "client_version", "entrypoint", "title", "started_at",
               "ended_at", "agentsmd_version", "instructions_sha256",
               "preferences_sha256", "direction_status", "identity_json"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown session fields: {sorted(unknown)}")
    con.execute(
        "INSERT OR IGNORE INTO sessions(session_key, harness, native_id,"
        " source_id, updated_at) VALUES(?,?,?,?,?)",
        (session_key, harness, native_id, source_id, now()))
    stale = session_sources_stale(con, session_key, harness, native_id,
                                  source_id)
    if stale:
        for key in sorted(_IDENTITY_REPLACE_FIELDS):
            con.execute(
                f"UPDATE sessions SET {key}=? WHERE session_key=?",
                (fields.get(key), session_key))
        # Rule 7: native free-text titles are never stored; a version
        # change clears any title an older import kept.
        con.execute("UPDATE sessions SET title=NULL WHERE session_key=?",
                    (session_key,))
    for key, value in fields.items():
        if stale and (key in _IDENTITY_REPLACE_FIELDS or key == "title"):
            continue
        if value is None:
            continue
        if key == "started_at":
            con.execute(
                "UPDATE sessions SET started_at=MIN(COALESCE(started_at, ?), ?)"
                " WHERE session_key=?", (value, value, session_key))
        elif key == "ended_at":
            con.execute(
                "UPDATE sessions SET ended_at=MAX(COALESCE(ended_at, ?), ?)"
                " WHERE session_key=?", (value, value, session_key))
        else:
            con.execute(
                f"UPDATE sessions SET {key}=COALESCE({key}, ?) WHERE session_key=?",
                (value, session_key))
    con.execute("UPDATE sessions SET updated_at=? WHERE session_key=?",
                (now(), session_key))
