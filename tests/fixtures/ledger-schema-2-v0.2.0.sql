BEGIN TRANSACTION;
CREATE TABLE agentsmd_versions (
  sha256 TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  commit_sha TEXT,
  released_at REAL,
  size_bytes INTEGER
);
CREATE TABLE assignments (
  submission_native_id TEXT NOT NULL REFERENCES submissions(native_id),
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt TEXT,
  phase TEXT,
  evidence TEXT,
  shared INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  PRIMARY KEY (submission_native_id, task_id)
);
INSERT INTO "assignments" VALUES('claude:u1','o/r#1','a1','implementation','e',0,1.0);
CREATE TABLE attempts (
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
INSERT INTO "attempts" VALUES(1,'o/r#1','turn-w','worker','codex',NULL,NULL,'route-b',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'failed',NULL,NULL,NULL);
INSERT INTO "attempts" VALUES(2,'o/r#1','turn-p','parent','claude',NULL,NULL,'route-a',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'complete',NULL,NULL,NULL);
CREATE TABLE dispatches (
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
INSERT INTO "dispatches" VALUES(1,'claude:u1','th-p','tp','th-w','tw','gpt',NULL,NULL,NULL,NULL,1.0);
CREATE TABLE events (
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
CREATE TABLE import_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT,
  source_path TEXT NOT NULL,
  ordinal_num INTEGER,
  error TEXT NOT NULL,
  line_excerpt TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE outcomes (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  candidate TEXT,
  proof_ref TEXT,
  acceptance_state TEXT NOT NULL DEFAULT 'unknown',
  repairs TEXT,
  corrections TEXT,
  updated_at REAL NOT NULL
);
INSERT INTO "outcomes" VALUES('o/r#1',NULL,NULL,'complete',NULL,NULL,1.0);
CREATE TABLE responses (
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
CREATE TABLE router_invocations (
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
CREATE TABLE router_jobs (
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
CREATE TABLE router_readings (
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
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO "schema_meta" VALUES('schema_version','2');
INSERT INTO "schema_meta" VALUES('event_contract_version','2');
INSERT INTO "schema_meta" VALUES('capture_contract_version','1');
CREATE TABLE session_assignments (
  session_key TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  evidence TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (session_key, task_id)
);
CREATE TABLE sessions (
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
INSERT INTO "sessions" VALUES('claude:s1','claude','s1',1,NULL,NULL,'/work/a',NULL,NULL,'cli',NULL,100.0,NULL,NULL,NULL,NULL,NULL,NULL,1.0);
CREATE TABLE sources (
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
INSERT INTO "sources" VALUES(1,'claude','/fixture/s.jsonl','x',0,0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,-1,0,1.0,NULL,NULL);
CREATE TABLE submissions (
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
INSERT INTO "submissions" VALUES('claude:u1',NULL,1,'claude:s1','claude:u1',0,100.0,'genuine','h1','please add the cache',1);
INSERT INTO "submissions" VALUES('claude:u2',NULL,1,'claude:s1','claude:u1',3,105.0,'genuine','h2','no, revert that',1);
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  project TEXT,
  family TEXT,
  title TEXT,
  issue_url TEXT,
  origin TEXT NOT NULL DEFAULT 'capture',
  created_at REAL NOT NULL
);
INSERT INTO "tasks" VALUES('o/r#1','o/r',NULL,'t',NULL,'capture',1.0);
CREATE TABLE turns (
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
CREATE INDEX idx_responses_session ON responses(session_key);
CREATE INDEX idx_responses_reconcile ON responses(session_key, semantics, is_overlap, thread_total_tokens, total_tokens);
CREATE INDEX idx_events_session ON events(session_key, ts);
CREATE INDEX idx_events_family ON events(family);
CREATE INDEX idx_submissions_session ON submissions(session_key);
CREATE INDEX idx_sessions_project ON sessions(project_dir);
CREATE INDEX idx_router_inv_request ON router_invocations(request_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('sources',1);
INSERT INTO "sqlite_sequence" VALUES('dispatches',1);
INSERT INTO "sqlite_sequence" VALUES('attempts',2);
COMMIT;
