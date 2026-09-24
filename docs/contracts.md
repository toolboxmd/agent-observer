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
- `assign-session`: explicit whole-session binding with `--exclusive` and
  dispatch evidence, only for a session dedicated to that task. Unknown
  sessions and conflicts with prior submission/session ownership are refused.
- `dispatch`: links an owning submission to a worker thread with requested
  route, policy version, and reason. Requested route stays separate from the
  observed model and effort on the attempt.
- `attempt`: observed turn with role parent or worker, model, effort, timing,
  terminal state, and whether output was usable. Re-capture updates state and
  supplied model/effort/usable evidence without erasing omitted observations.
  A Router-dispatched worker row for its native turn merges with the
  Router invocation row only on explicit turn/session/time evidence;
  shared or unknown ownership never auto-merges.
- `outcome`: explicit acceptance state per task. A zero process exit never
  implies acceptance. Re-recording complete to add proof, candidate or
  metadata never moves the first accepted-completion timestamp.
- Router reads `observer_task_id` from task JSON, falling back to
  `router:<request-id>` when absent or invalid. Several jobs may map to one
  task. Their attempts and dedicated sessions retain native invocation evidence;
  a shared planner session remains unbound. Reimport preserves explicit capture
  metadata/outcomes and replaces the same invocation's legacy adapter binding.
  Default discovery covers the configured `DURABLE_RUNNER_STATE_DIR`, current
  `~/.local/share/durable-runner` and legacy `~/.local/state/model-router`,
  deduplicated by canonical source path. Explicit `--root`/`--source` stays scoped.

Outcome states: `complete`, `active`, `cancelled`, `failed`,
`quota_blocked`, `crashed`, `unknown`. Crashed attempts with no usable output
are counted separately and excluded from useful-work comparisons.

## Query surface

- `sync`: import every available harness, or `--harness` / `--source`.
- `sessions list|show`: sessions with project, AgentsMD version and usage.
- `task list|show --task ID [--prices schedule.json]`: attributed, shared joint, and unassigned token
  buckets over the sessions the task touches; per-model rows with native
  buckets and exact semantics; missing and conflicting
  assignments; crash count; explicit acceptance state; outcome with
  candidate, proof, repairs and corrections; attempts; dispatches; active
  work; snapshot identity with source cutoff and measured set; task-scoped
  diagnostics and time with session context labeled; native cost and
  sourced estimate with coverage; completion timing
  (submission-to-accepted-completion or elapsed-so-far at the named
  cutoff), per attempt/session/role observed wall time with compatible
  waiting intervals only, failures by class from terminal/stage with
  production denominators including quota exhaustion and unknown
  cancellation intent, separate
  job outcomes, per-failure recovery inside compatible identities, and attributable Router usage source coverage.
  Human text and `--json` expose the same measurements with attempt and
  session identity links to existing evidence. Exit 3 when
  missing or conflicting ownership exists. Every command accepts `--json`.
- `publish --task ID|--session KEY [--prices schedule.json] --repo owner/name --pr N|--commit SHA`:
  render or post the summary comment; `--dry-run` prints the comment
  without posting, and `--dry-run --json` prints a JSON payload with the
  same summary data, the rendered body, and an explicit target naming the
  task or sessions plus any repo, PR or commit. Task summaries carry the
  same usage, models, shared rows, outcome, coverage, scope, diagnostics,
  measured set, snapshot and cost as task JSON. Session summaries name
  their limited scope and carry no task outcome.
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
`complete` flag requires zero missing and zero conflicting bindings and no
recorded active work, missing native sessions/usage or unbound worker sessions.
Captured dispatches without explicit worker ownership are coverage gaps too;
the dispatch edge alone never assigns the worker's usage to the task.
Conflicts between whole-session and submission ownership stay shared and incomplete.
Active attempts, crashes and missing evidence stay visible alongside the
arithmetic.

## Report identity, time and acceptance

- `snapshot_id` is a SHA-256 over the selected task, sorted sessions,
  response counters with semantics and model/effort/turn, assignments,
  outcome, attempts with full report inputs (timing evidence with
  started, ended, elapsed and terminal class plus role, harness,
  session_key, stage, reason, observed model/effort, route and usage
  presence), Router job status and updated_at, reconciliation groups,
  recovery inputs, usage attribution, submission timestamps,
  dispatches, source cutoff, price schedule and coverage
  evidence, including ownership that another task records in the same session. `source_cutoff`
  is the max native `imported_at` backing the scope. Repeats without
  ledger changes keep the identity; changed evidence changes it. The
  `measured` set names the task, sessions and attributed/shared/
  unassigned response ids with the cutoff. No prompt or transcript
  content participates.
- `acceptance_state` is explicit, defaulting to `unknown` when no outcome
  row exists. Process success, zero exit or a successful attempt never
  implies acceptance.
- `diagnostics` holds task-turn scoped detector counts plus session
  context counts kept explicitly separate. `time` holds task-turn elapsed
  with its source, or unavailable when no task turn timing exists, with
  the whole-session span labeled session context, never a task-only fact.
- `timing` holds submission-to-accepted-completion elapsed time only
  when the earliest bound submission timestamp and the first explicit
  complete outcome timestamp are both present with a non-negative
  difference. Re-recording complete to add proof or metadata never
  moves the first timestamp. A known accepted completion without
  submission timing reports accepted completion known but elapsed
  unavailable. Negative endpoint differences are rejected and
  qualified. Active tasks hold elapsed-so-far at the named
  source cutoff. Failed, cancelled and unaccepted tasks hold no invented
  accepted completion time. The owned-turn span stays labeled as a
  partial execution span, never as accepted completion.
- `attempt_timing` holds observed wall time per attempt, session, role
  and model with completed and active attempts distinguished and shared
  or unknown ownership qualified. Session union span is the merged
  covered duration of explicit attempt windows with gaps excluded.
  Waiting intervals derive only from explicit timestamps under the
  same known session or the same Router request with no overlap;
  parallel overlap produces no waiting row. Parallel attempt durations never become
  task elapsed time. Router and native attempt rows for one execution
  merge only on explicit turn/session/time evidence into one execution
  with its sources and members named; durations and
  attempt counts are never doubled and shared or unknown ownership
  never auto-merges.
- `failures` holds failed/production attempt counts by observed class
  from explicit terminal and stage only (timeout, stall, provider,
  infrastructure, implementation,
  verification, unknown); Router reason never classifies. Production attempts are
  complete plus failed plus quota_blocked provider exhaustion;
  quota_blocked counts as class provider inside failed/total and stays
  visible separately. Cancellations report total with intentional
  (explicit evidence only) and unknown intent split; bare Router
  cancelled stays intent unknown outside the
  denominator. Crashes stay separately counted; active and unknown
  stay outside. `job_outcomes` holds the separate Router job statuses with
  status and updated_at. A
  provider exhaustion followed by a successful pool move is an attempt
  failure plus a separate outcome, not a failed accepted task.
- `recovery` holds, per failed execution, the failure-to-next-attempt-start
  duration inside the same compatible identity (same Router request_id
  resolved through router_invocations, or same known non-shared
  session), the first subsequent completed progress at any stage with
  its `first_progress_stage` label measured at its end when observable,
  the same-stage progress turn and timing when a known equal stage
  exists, with repeated failed counts in that stage chain only,
  and the recovery outcome. Recovered, active and repeated counts need
  the failed stage and the candidate stage both known and equal; a
  dispatcher or other different-stage completion never recovers
  implementation work and unknown stage never matches. A new attempt
  starting alone is not successful recovery; unresolved recovery stays
  active or unknown; attempts from another request or shared/unknown
  sessions never pair; next start must be at or after failed end.
- `usage_coverage` holds Router usage source coverage with attributable
  native responses only (same session plus a turn or time match; turn
  match required for shared sessions): null usage_json
  beside attributable native usage is source coverage, never zero usage or
  complete loss. Measured but unpriced usage is distinct from missing
  usage.

## Sourced pricing

`agent_observer/prices.json` supplies dated, sourced standard API list-price
rates for exact supported model identities. It is an equivalent-cost estimate,
not a subscription invoice; discounts and service-tier premiums are excluded. A caller schedule is JSON with `source_url` (http(s)),
`as_of`/`effective_date`, `currency` USD, `unit` per million tokens and
`models` mapping model ids to `semantics` (exact supported strings) and
`rates` (USD per million per bucket). Rates must be finite numbers at or
above zero; booleans, negatives, infinities and NaNs invalidate the file.
A dict rate marks TTL-keyed pricing. Claude's native `cache_creation`
5-minute and 1-hour counters are retained separately and must reconcile to
total cache writes. Older sources replay once to enrich accepted rows without
changing finalized usage; contradictory TTL evidence is quarantined. Missing
TTL remains unknown. `long_context_threshold` with optional
`long_context_rates` selects the long tier by input size; unknown input
or a missing long rate for an over-threshold response stays unknown. Long-tier
rates never inherit standard rates for an omitted nonzero component.

Pricing is per response under its native semantics. Codex input includes
cached and output includes reasoning, so priced parts are input minus
cached and output minus reasoning; Claude input excludes cache with
output including thinking; OpenCode prices separate buckets directly. Other
semantics, including unverified Grok billing semantics, stay unpriced.
Every needed bucket must be known; missing rates for nonzero parts,
unknown counters and inconsistent subset rows stay unknown, never zero.
An unknown reasoning split does not block pricing inclusive output when both
parts have the same verified rate; the measured reasoning count stays unknown.
Muse Contributor Free uses its sourced promotional route price of zero, not
the price of a different paid model or a subscription cost allocation.
The harness total is never priced from and counters are never averaged.
`price_scope` returns priced/unpriced response counts, a partial subtotal
of priced responses kept separate from the complete total (present only
when every response prices), per-model priced coverage and unpriced
reasons. Native `cost_usd` has a separate known subtotal, with a complete total only
when every selected response reports cost; subscription quota readings
are account evidence and are never a cost estimate.

## Work phase and activity

`phases` partitions attributed responses without duplication. It uses explicit
submission `phase`, an exact-turn `capture attempt --phase` label when supplied,
or the recorded Router stage of a wholly owned dedicated session. Several
stages in one scope produce `mixed`; absent/unknown labels stay `unclassified`.
A turn-level phase labels the entire turn, not an invented transition within it.
An explicit `mixed` label is appropriate when a long turn spans phases.
Shared and unassigned responses remain outside phase-attributed costs.

Each phase exposes native buckets, per-model estimates and recorded tool calls,
MCP results, reads, file changes and failed tool results. These activity
observations can overlap and depend on harness coverage. They are not a split
of the token bill: a later model input can include many earlier tool results.
Reasoning and other generated output form a separate dimension inside every
phase. Inclusive output is decomposed only with valid native split counters;
OpenCode's separate counters are added for this denominator. Reasoning share is
reasoning / generated tokens, never reasoning / input-plus-output. Missing
splits stay visible and suppress a complete percentage. Other output includes
code, tool calls and replies, not implementation alone.

## Publication

Rendering is offline and deterministic; posting needs the explicit
`publish` command with a validated target (`owner/name`, positive PR,
6-40 hex commit). Only aggregates leave the machine. Dynamic model, task
and target fields escape table pipes, backticks, HTML and control
characters with newlines collapsed. Candidate, proof, repairs and
corrections render only as public GitHub URLs, owner/name identifiers,
numeric PRs, hex SHAs or fixed hashes; free-form text renders as
withheld. Session summaries carry `scope_kind` session with an explicit
limited-scope note and no acceptance. Repeats update only the
authenticated Observer-owned marker comment; foreign markers are left
alone and duplicates are reported.

## Verifier separation

ccusage is a pinned development cross-check only. Runtime code never imports
or executes it. `tests/test_cli.py` asserts that no runtime module imports
or calls it.

## Codex model context across imports

Each source preserves the native model/effort context at its imported byte
offset. An append resumes that context; deferred fallback usage preserves the
context at each record, including model changes. Older sources, or offsets
advanced by an older reader, replay once in place to repair model metadata
without deleting ownership or adding response counters. Unchanged sources then
retain the fast path. No model is inferred when native context is absent.
