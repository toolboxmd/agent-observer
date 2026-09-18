"""SQLite ledger schema and helpers. Stdlib only."""

from __future__ import annotations

import sqlite3
import time

from . import CAPTURE_CONTRACT_VERSION, EVENT_CONTRACT_VERSION, SCHEMA_VERSION

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  cli_version TEXT,
  session_id TEXT,
  thread_id TEXT,
  ordinal_max INTEGER NOT NULL DEFAULT -1,
  raw_bytes INTEGER NOT NULL DEFAULT 0,
  imported_at REAL NOT NULL,
  UNIQUE (harness, sha256)
);
CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id),
  root_turn_id TEXT,
  session_id TEXT,
  model_observed TEXT,
  effort_observed TEXT,
  started_at REAL,
  completed_at REAL,
  duration_ms INTEGER,
  state TEXT
);
CREATE TABLE IF NOT EXISTS responses (
  response_id TEXT PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  thread_id TEXT,
  turn_id TEXT REFERENCES turns(turn_id),
  root_turn_id TEXT,
  session_id TEXT,
  ordinal_num INTEGER,
  ts REAL,
  input_tokens INTEGER,
  cached_input_tokens INTEGER,
  cache_write_input_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_output_tokens INTEGER,
  total_tokens INTEGER,
  turn_total_tokens INTEGER,
  thread_total_tokens INTEGER,
  is_overlap INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS submissions (
  native_id TEXT PRIMARY KEY,
  alias_id TEXT,
  source_id INTEGER REFERENCES sources(id),
  turn_id TEXT REFERENCES turns(turn_id),
  ordinal_num INTEGER,
  ts REAL,
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
  model_requested TEXT,
  model_observed TEXT,
  effort_requested TEXT,
  effort_observed TEXT,
  started_at REAL,
  ended_at REAL,
  state TEXT NOT NULL DEFAULT 'active',
  usable_output INTEGER,
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
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER REFERENCES sources(id),
  ordinal_num INTEGER,
  ts REAL,
  family TEXT NOT NULL,
  native_id TEXT,
  turn_id TEXT,
  name TEXT,
  status TEXT,
  duration_ms INTEGER,
  size_bytes INTEGER,
  truncated INTEGER,
  fingerprint TEXT,
  detail_json TEXT,
  UNIQUE (source_id, family, native_id)
);
CREATE TABLE IF NOT EXISTS import_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL,
  ordinal_num INTEGER,
  error TEXT NOT NULL,
  line_excerpt TEXT,
  created_at REAL NOT NULL
);
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


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
        "('schema_version', ?), ('event_contract_version', ?), "
        "('capture_contract_version', ?)",
        (str(SCHEMA_VERSION), str(EVENT_CONTRACT_VERSION),
         str(CAPTURE_CONTRACT_VERSION)),
    )
    con.commit()


def now() -> float:
    return time.time()
