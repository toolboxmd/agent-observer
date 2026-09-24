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
6. Ledger privacy, fail closed, implemented once in
   `agent_observer/privacy.py` (adapters keep no private copies):
   `submissions.text_excerpt` is empty unless the submission is a genuine
   human submission of the main session; then the human text up to the
   first tag-like marker (`<` followed by a letter, `/` or `!`, or the
   first `<<<`), whitespace-collapsed, at most 300 characters. Assistant
   excerpts keep the last 400 characters of each assistant text block
   only when that span holds no marker: the Claude Code adapter calls
   the helper once per nonempty assistant text block and emits one
   assistant_message event per text block, so a message with several
   blocks keeps one excerpt per block; the Codex adapter concatenates
   the text items of an assistant message and keeps one excerpt for
   the combined message. No other adapter emits assistant_message
   excerpts. Every source records the privacy rules version; a version
   change fully re-imports the source, updating rows in place and
   replacing that source's `import_errors`. `import_errors.error` is
   exactly one closed category (`malformed_json`, `unknown_record`,
   `schema_error`, `missing_id`, `malformed_usage`, `usage_conflict`,
   `source_unreadable`, `unsupported_schema`, fallback `import_error`);
   `line_excerpt` holds only sorted top-level JSON key names (200 chars
   max). `events.detail_json` keeps only per-family allowlisted keys with
   correctly typed values (numbers, booleans, fixed-length hashes, native
   identifiers, paths, commands, closed status/kind enums); targets follow
   the same type rules, and target filtering is family-aware: `skill_read`
   and `skill_invoke` targets hold only a validated native skill
   identifier under the same complete identifier rule as names, so a
   free-text skill title or an installed directory path never persists
   as a target. The installed skill file path lives only in detail
   `skill_path` (`skill_read` and `skill_invoke`), marker- and
   type-checked; `skill_invoke` keeps it only when the native invocation
   supplies a directory. Never titles, messages, error text, outputs,
   content, arguments or other free text. A privacy-version change also
   replaces source-owned session identity (`identity_json`,
   `agentsmd_version`, `instructions_sha256`, `preferences_sha256`,
   `direction_status`): omitted or invalid values clear to NULL instead
   of COALESCE-keeping old content.
7. Tables not named above store no free text from native records beyond
   identifiers, model and provider names, paths and commands. Native
   free-text titles are discarded.

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
| `skill_read` | Read under an installed Skill directory | validated skill identifier in `target` (never a title or directory path); installed file path only in detail `skill_path`; skill name and AgentsMD version from the path |
| `skill_invoke` | Explicit Skill invocation (for example Claude's `Skill` tool) | validated skill identifier in `target`; installed `SKILL.md` path only in detail `skill_path` when the native record supplies a directory |
| `file_change` | Edit or write by the agent | path, change kind, content size and hash |
| `compaction` | Context compaction boundary | trigger, before and after sizes when recorded |
| `lifecycle` | Turn start, completion, abort, subagent activity, stop reasons | duration, reason |
| `assistant_message` | Final assistant text of a turn | last 400 characters per Claude assistant text block; one combined-message excerpt on Codex |
| `permission` | Permission request or denial | tool, outcome |

A tool call joins its result only on an equal native call id. Nothing is
guessed into a join.

## Instruction identity

`identity.SessionIdentity` records, where evidence exists:
`instructions_sha256`, `preferences_sha256` and `direction_status` from the
`AGENTSMD_PROJECT_DIRECTION_V1` hook block, and `agentsmd_version` from the
most-read versioned plugin path (`.../agentsmd/<x.y.z>/...`). The block is
sanitized centrally in `identity.py`, fail closed: `direction_status` must
belong to exactly `ready`, `missing`, `stale`, `potentially_stale`,
`invalid`, `uninitialized`, `not_in_repository`, SHA-256 fields must be exactly
64 lowercase hex characters, the git head must be the fixed-length
lowercase hex Git digest, paths must be absolute and marker-free, direction
files are limited to the approved `VISION.md`, `MISSION.md`, `OBJECTIVE.md`
names, and `identity_json` serializes only that approved allowlist of
fields, never the raw block, titles, contents or free text. `sync`
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
  missing or conflicting ownership exists. Every command accepts `--json`.
- `publish --task ID|--session KEY --repo owner/name --pr N|--commit SHA`:
  render or post the summary comment; `--dry-run` prints the comment
  without posting, and `--dry-run --json` prints a JSON payload with the
  same summary data, the rendered body, and an explicit target naming the
  task or sessions plus any repo, PR or commit.
- `trace --task ID | --session KEY | --turn ID [--family F]`: ordered events
  with tool join status. `trace --capabilities` prints coverage per harness.

Raw counter buckets are only ever summed within one counter semantics. A
scope mixing semantics, including a known semantics beside missing
semantics, omits the top-level raw buckets (`input_tokens`,
`cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, `total_tokens`) and reports complete
per-semantics totals under `by_semantics` instead; response and overlap
counts are preserved. This holds for scope totals, for every
`task_report` counter section (attributed, shared joint, unassigned in
scope, nested scope), for publish model rows (grouped by
harness, model, effort and semantics) and summaries (mixed shared scope
exposes `shared_by_semantics` with no combined total), for `sessions
list`/`show` (mixed sessions expose `by_semantics` with no combined
total; text renders `mixed semantics (see --json)`), and for `compare`
per-session and group token cells (mixed groups expose `by_semantics`
with no median, mean or total).

Reconciliation: attributed plus shared plus unassigned equals the scope
total. Responses must partition exactly; token sums are compared per
counter semantics when the scope is mixed, so an absent mixed total is
never treated as zero. A report never claims completeness from arithmetic alone; the
`complete` flag requires zero missing and zero conflicting bindings.

## Verifier separation

ccusage is a pinned development cross-check only. Runtime code never imports
or executes it. `tests/test_cli.py` asserts that no runtime module imports
or calls it.
