# Contracts: ledger, adapters, capture, and query surface

Versions: schema 2, event contract 2, capture contract 1, stored in
`schema_meta`. A ledger with another schema version is refused; move it
aside and sync again.

## Ledger

SQLite, stdlib only, at `~/.local/state/agent-observer/observer.db`
(`--db` or `AGENT_OBSERVER_DB` overrides). Raw session files and the
database stay outside Git. Only sanitized fixtures under `tests/fixtures`
are committed.

| Table | Natural key | Holds |
| --- | --- | --- |
| `sources` | harness, path | One native source unit: a session file, or `opencode.db#<session id>`. Snapshot fingerprint, read offset, tail hash, import duration. |
| `sessions` | `session_key` | `<harness>:<native session id>`: project directory, branch, client version, entrypoint, parent session, start and end, instruction identity. |
| `turns` | `turn_id` | Harness turn or prompt boundary with observed model and effort, timing, state. |
| `responses` | `response_id` | One usage-bearing model response: raw counters, `semantics`, harness total. |
| `submissions` | `native_id` | User-role inputs with `kind` genuine, synthetic, scaffolding or interrupt. |
| `events` | session_key, family, native_id | Operational events (families below). |
| `tasks`, `assignments`, `session_assignments`, `dispatches`, `attempts`, `outcomes` | see capture | Workload ownership and outcomes. |
| `router_jobs`, `router_invocations`, `router_readings` | router ids | Model Router ledger rows, copied read-only. |
| `agentsmd_versions` | AGENTS.md SHA-256 | Release map from the local AgentsMD tags. |
| `import_errors` | none | Quarantined records; prior valid data is kept. |

Every natural key carries its harness prefix (`codex:`, `claude:`,
`opencode:`, `grok:`, `router:`) except event native ids, which are unique
within their session and family. Rows are keyed by session, not by source
file, so re-reading a file, a grown log, or a copied file never adds usage
twice.

## Adapter contract

An adapter is `agent_observer/adapters/<harness>.py` with `HARNESS`,
`CAPABILITIES` (family, supported, detail) and
`sync(con, root=None, full=False, source=None) -> dict` returning at least
`harness`, `sources`, `unchanged`, `responses_inserted`, `events_inserted`,
`submissions_inserted`, `malformed` and `failed` (list of
`{path, error}`). It must:

1. Read native records only, read-only. Never write a harness's files or
   database.
2. Be idempotent: insert under natural keys with `INSERT OR IGNORE`. A
   repeated immutable record with different counters is a conflict recorded
   in `import_errors`, never a second row and never a silent overwrite.
   Mutable native rows (a message still streaming) are imported only once
   final, or updated in place when the harness finalizes them.
3. Use `ingest.JsonlSource` for append-only JSONL files (it resumes at the
   last complete line when the preceding bytes are unchanged).
4. Write one `sessions` row per native session through `db.upsert_session`,
   with project directory, branch, client version, start and end where
   recorded, and instruction identity from `identity.SessionIdentity`
   (feed it message texts that may carry the AgentsMD direction block and
   every file path the session read).
5. Keep native counter meaning. `responses.semantics` names it; the raw
   columns hold native values; `total_tokens` is the harness's own total.
   Unknown counters stay NULL, never zero.
6. Quarantine unknown record shapes in `import_errors` with an excerpt no
   longer than 200 characters, and continue.
7. Store no file contents, tool outputs or preference contents. Excerpts
   are limited to 300 characters of a user submission and the last 400
   characters of an assistant message, in the private ledger only.

### Counter semantics by harness

| Harness | `semantics` | total_tokens |
| --- | --- | --- |
| Codex | `codex:input_includes_cached,output_includes_reasoning` | input + output (native `total_tokens`) |
| Claude Code | `claude:input_excludes_cache,output_includes_thinking` | input + cache_creation + cache_read + output |
| OpenCode | `opencode:input_excludes_cache,reasoning_separate` | input + output + reasoning + cache read + cache write |
| Grok Build | `grok:<per native record>` | as reported per prompt completion |

### Event families

| Family | Meaning | Key detail |
| --- | --- | --- |
| `tool_call` | Model requested a tool | name, argument fingerprint, `target` (file path or command when present) |
| `tool_result` | Tool returned | status (`ok`, `error`, `denied`), size, truncation, duration, exit code in detail |
| `read` | Observed file read | `target` path; content identity when recorded |
| `skill_read` | Read under an installed Skill directory | `target` path; skill name and AgentsMD version from the path |
| `skill_invoke` | Explicit Skill invocation (for example Claude's `Skill` tool) | skill name |
| `file_change` | Edit or write by the agent | path, change kind, content size and hash |
| `compaction` | Context compaction boundary | trigger, before and after sizes when recorded |
| `lifecycle` | Turn start, completion, abort, subagent activity, stop reasons | duration, reason |
| `assistant_message` | Final assistant text of a turn | last 400 characters |
| `permission` | Permission request or denial | tool, outcome |

A tool call joins its result only on an equal native call id. Nothing is
guessed into a join.

## Instruction identity

`identity.SessionIdentity` records, where evidence exists:
`instructions_sha256`, `preferences_sha256` and `direction_status` from the
`AGENTSMD_PROJECT_DIRECTION_V1` hook block, and `agentsmd_version` from the
most-read versioned plugin path (`.../agentsmd/<x.y.z>/...`). `sync`
rebuilds `agentsmd_versions` from the AgentsMD repository behind the global
instruction link (or `--agentsmd-repo`) and resolves versions from hashes.
Unresolved hashes stay unresolved.

## Capture contract (CLI `capture`)

- `create-task`: stable task identity with project, family, title, Issue link.
- `assign`: binds one genuine submission to one task with attempt, phase, and
  evidence. Every genuine submission needs a binding, including followups.
  A submission with no binding is reported missing and never inherits the
  prior task. Binding the same submission to several tasks with `--shared`
  keeps the response joint; tokens are shown under each task as shared and
  never divided. Bindings without `--shared` to several tasks are reported
  as conflicting. Bare native ids resolve when unique.
- `dispatch`: links an owning submission to a worker thread with requested
  route, policy version, and reason. Requested route stays separate from the
  observed model and effort on the attempt.
- `attempt`: observed turn with role parent or worker, model, effort, timing,
  terminal state, and whether output was usable.
- `outcome`: explicit acceptance state per task. A zero process exit never
  implies acceptance.
- Runner jobs need no capture: the router import binds each job's sessions
  to its task through `session_assignments` with the job record as evidence.

Outcome states: `complete`, `active`, `cancelled`, `failed`,
`quota_blocked`, `crashed`, `unknown`. Crashed attempts with no usable output
are counted separately and excluded from useful-work comparisons.

## Query surface

- `sync`: import every available harness, or `--harness` / `--source`.
- `sessions list|show`: sessions with project, AgentsMD version and usage.
- `task list|show --task ID`: attributed, shared joint, and unassigned token
  buckets over the sessions the task touches; missing and conflicting
  assignments; crash count; outcome; attempts; dispatches. Exit 3 when
  missing or conflicting ownership exists.
- `trace --task ID | --session KEY | --turn ID [--family F]`: ordered events
  with tool join status. `trace --capabilities` prints coverage per harness.

Reconciliation: attributed plus shared plus unassigned equals the scope
total. A report never claims completeness from arithmetic alone; the
`complete` flag requires zero missing and zero conflicting bindings.

## Verifier separation

ccusage is a pinned development cross-check only. Runtime code never imports
or executes it. `tests/test_cli.py` asserts that no runtime module imports
or calls it.
