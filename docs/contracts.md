# Contracts: ledger, adapters, capture, and query surface

Versions: schema 3, event contract 2, capture contract 1, stored in
`schema_meta`. A schema-2 ledger migrates in place on its first read-write
open (backup `<ledger>.schema-2.bak`, one transaction, rollback leaves
schema 2): `submissions` to `user_messages`; `dispatches` to `delegations`
(`owning_submission`, `worker_thread`, `worker_turn` to
`owning_user_message`, `child_thread`, `child_turn`); `attempts` to
`task_turns` (`route_requested` to `color_requested`, role `worker` to
`child`); `assignments.submission_native_id` and `assignments.attempt` to
`user_message_id` and `task_turn`; `verdicts.moment_id` to
`intervention_id`. Read-only views under the three old table names with
their schema-2 columns stay for one release. A ledger with any other
schema version is refused; move it aside and sync again.

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
| `user_messages` | `native_id` | User-role inputs with `kind` genuine, synthetic, scaffolding or interrupt. |
| `events` | session_key, family, native_id | Operational events (families below). |
| `tasks`, `assignments`, `session_assignments`, `delegations`, `task_turns`, `outcomes` | see capture | Workload ownership and outcomes. |
| `t3_turn_origins`, `t3_threads`, `t3_links` | T3 message, thread, link ids | Chromeria turn origins, thread to native-session map with who opened each thread (`opened_by`: client, server or unknown), thread-tree links with PR snapshot state, mirrored read-only from T3 state. |
| `t3_spawns`, `t3_messages` | T3 thread id, T3 message id | Child thread parent, depth and creation time; sender, target and time of each attributed `message_thread` delivery. Ids and times only. |
| `verdicts` | intervention id, behavior, judge | An outside judge's `yes`, `no` or `unclear` on one intervention, with a redacted one-line problem and an optional group id for `yes`. |
| `session_attributes` | session_key, name | Attributes other tools supply (contextmd's `arm`), used only as report filters. |
| `t3_outcome_provenance` (`router_outcome_provenance`) | task id | Last adapter-written outcome snapshot; a row that differs is user-owned and survives re-import. |
| `router_jobs`, `router_invocations`, `router_readings` | router ids | Model Router ledger rows, copied read-only. |
| `agentsmd_versions` | AGENTS.md SHA-256 | Release map from the local AgentsMD tags. |
| `cue_import` | one snapshot | ContextMD import status, time and source watermark; unavailable evidence is a gap. |
| `cue_moments`, `cue_candidates` | source moment id, candidate id | Cue decision metadata, scores, all candidate outcomes and delivery stages; version-matched targets, never prompt, window or served text. |
| `cue_reads` | harness, session, epoch, path, kind, moment, time | Settled successful ContextMD reads with optional exact native event linkage; no duplicate native events are inserted. |
| `import_errors` | none | Quarantined records; prior valid data is kept. |

Every natural key carries its harness prefix (`t3:`, `codex:`, `claude:`,
`opencode:`, `grok:`, `router:`) except event native ids, which are unique
within their session and family. Rows are keyed by session, not by source
file, so re-reading a file, a grown log, or a copied file never adds usage
twice.

## Adapter contract

An adapter is `agent_observer/adapters/<harness>.py` with `HARNESS`,
`CAPABILITIES` (family, supported, detail) and
`sync(con, root=None, full=False, source=None) -> dict` returning at least
`harness`, `sources`, `unchanged`, `responses_inserted`, `events_inserted`,
`user_messages_inserted`, `malformed` and `failed` (list of
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
   `user_messages.text_excerpt` is empty unless the user message is a
   genuine one typed by a person in the main session; then that text up to the
   first tag-like marker (`<` followed by a letter, `/` or `!`, or the
   first `<<<`), whitespace-collapsed, at most 300 characters. Assistant
   excerpts keep the last 400 characters of each assistant text block
   only when that span holds no marker: the Claude Code adapter calls
   the helper once per nonempty assistant text block and emits one
   assistant_message event per text block, so a message with several
   blocks keeps one excerpt per block; the Codex adapter concatenates
   the text items of an assistant message and keeps one excerpt for
   the combined message. Grok emits one per native assistant chunk;
   OpenCode emits one per assistant text part, updated as the part streams.
   Every source records the privacy rules version; a version
   change fully re-imports the source, updating rows in place and
   replacing that source's `import_errors`. `import_errors.error` is
   exactly one closed category (`malformed_json`, `unknown_record`,
   `schema_error`, `missing_id`, `malformed_usage`, `usage_conflict`,
   `source_unreadable`, `unsupported_schema`, `identity_conflict`,
   fallback `import_error`);
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
   free-text titles are discarded. Two tables hold free text written by
   tools other than the harness: `verdicts.problem` (one line of at most
   200 characters from an outside judge that read private session
   content; known secret shapes redacted) and `session_attributes.value`
   (one line of at most 80 characters). Neither is published.
8. Tool arguments: `events.target` keeps the full path or command a tool
   call names, up to 65,536 characters (`privacy.ARGUMENT_CHARS`). The
   longest real values measured on 2026-09-29 were 42,102 characters
   (Claude Code Bash) and 54,843 (Codex exec). For Codex, a code-mode
   `exec` program or other custom tool input is the command; function
   arguments give their path key or `cmd`/`command`. Before storage,
   known secret shapes (private key blocks, `sk-`, `ghp_`, `github_pat_`,
   `glpat-`, `xox?-`, AWS, Google and JWT tokens, secret-named
   assignments and flags, bearer tokens, URL passwords) become
   `[redacted]`. Every `apply_patch` hunk keeps only its
   `*** Add/Update/Delete File:` and `*** Move to:` lines. A heredoc
   whose introducing line redirects to a file or pipes into `tee` keeps
   its delimiters and `[content omitted]` instead of its body, and so
   does the payload of `echo` or `printf` whose pipeline writes stdout
   to a file (`>`, `>>`, `1>`, `&>`, `>&file`) or pipes into `tee` in any
   later stage, and a here-string (`<<<`) feeding `tee` or a file
   redirect. A heredoc's introducing line ends at the first real or
   escaped (`\n`, as in a Codex `exec` string) newline, and its body
   closes at a terminator line of the same kind, so commands after the
   terminator stay. A heredoc feeding an interpreter (`python3 - <<EOF`)
   is the program and stays. Known limits: redirects applied to a whole group
   or shell (`exec >f`, `{ echo x; } >f`, `( … ) >f`) and programmatic
   writes (`python3 -c` writing a literal) keep their payloads. Other arguments (messages, prompts, edit strings, Codex
   function arguments that are not a JSON object) are free text and stay
   out.

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
| `tool_call` | Model requested a tool | name, argument fingerprint, `target` (full file path or command when present, rule 8) |
| `tool_result` | Tool returned | status (`ok`, `error`, `denied`), size, truncation, duration, exit code in detail |
| `read` | Observed file read | `target` path; content identity when recorded |
| `skill_read` | Read under an installed Skill directory | validated skill identifier in `target` (never a title or directory path); installed file path only in detail `skill_path`; skill name and AgentsMD version from the path |
| `skill_invoke` | Explicit Skill invocation (for example Claude's `Skill` tool) | validated skill identifier in `target`; installed `SKILL.md` path only in detail `skill_path` when the native record supplies a directory |
| `file_change` | Edit or write by the agent | path, change kind, content size and hash |
| `compaction` | Context compaction boundary | trigger, before and after sizes when recorded |
| `lifecycle` | Turn start, completion, abort, subagent activity, stop reasons | duration, reason |
| `assistant_message` | Native assistant text | last 400 characters per Claude block, Codex message, Grok chunk or OpenCode text part |
| `permission` | Permission request or denial | tool, outcome; Claude's denial kind (`user-rejected`, `permission-rule`, `automode-blocked`, `interrupted`) in detail `denial` |

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

The Skill is query-only: no agent runs capture commands. The CLI keeps
them for Router ownership and explicit human use.

- `create-task`: stable task identity with project, family, title, Issue link.
- `assign`: binds one genuine user message to one task with task turn, phase,
  and evidence. Every genuine user message needs a binding, including
  followups. A user message with no binding is reported missing and never
  inherits the prior task. Binding the same user message to several tasks
  with `--shared`
  keeps the response joint; tokens are shown under each task as shared and
  never divided. Bindings without `--shared` to several tasks are reported
  as conflicting. Bare native ids resolve when unique.
- `assign-session`: explicit whole-session binding with `--exclusive` and
  delegation evidence, only for a session dedicated to that task. Unknown
  sessions and conflicts with prior user-message/session ownership are refused.
- `delegation` (old name `dispatch`): links an owning user message to a child
  thread with requested model and effort, policy version, and reason. The
  request stays separate from the observed model and effort on the task turn.
- `task-turn` (old name `attempt`): observed turn with role parent or child
  (`worker` is accepted as the old name for child), model, effort, timing,
  terminal state, and whether output was usable. Re-capture updates state and
  supplied model/effort/usable evidence without erasing omitted observations.
- `outcome`: explicit acceptance state per task. A zero process exit never
  implies acceptance.
- Router reads `observer_task_id` from task JSON, falling back to
  `router:<request-id>` when absent or invalid. Several jobs may map to one
  task. Their task turns and dedicated sessions retain native invocation evidence;
  a shared planner session remains unbound. Reimport preserves explicit capture
  metadata/outcomes and replaces the same invocation's legacy adapter binding.
  Default discovery covers the configured `DURABLE_RUNNER_STATE_DIR`, current
  `~/.local/share/durable-runner` and legacy `~/.local/state/model-router`,
  deduplicated by canonical source path. Explicit `--root`/`--source` stays scoped.

Outcome states: `complete`, `active`, `cancelled`, `failed`,
`quota_blocked`, `crashed`, `unknown`. Crashed task turns with no usable output
are counted separately and excluded from useful-work comparisons.

## Query surface

- `sync`: import every available harness, or `--harness` / `--source`.
- `sessions list|show`: sessions with project, AgentsMD version and usage.
  `list` adds `source_status` (`available`, `partial` or `ledger_only`).
  `show` embeds `source_availability` for that session, including each
  source path, status and reason.
- `sessions availability [--session K] [--harness H]`: how many indexed
  events are `live`, `relocated`, `ledger_only` or `changed`, and why.
  The machine summary names no paths. Exit 3 when any listed session is
  `ledger_only` or `partial`. `available` means every indexed event is
  still readable, including a unique archived copy whose tail matches.
  An OpenCode source is the session row. A Grok session whose
  `updates.jsonl` is gone or replaced is not available. A session with
  no indexed events is still unavailable when a source backing the
  session, its responses, or its submissions is gone or replaced.
- `task list|show --task ID [--prices schedule.json]`: attributed, shared joint, and unassigned token
  buckets over the sessions the task touches; per-model rows with native
  buckets and exact semantics; missing and conflicting
  assignments; crash count; explicit acceptance state; outcome with
  candidate, proof, repairs and corrections; task turns; delegations; active
  work; snapshot identity with source cutoff and measured set; task-scoped
  diagnostics and time with session context labeled; native cost and
  sourced estimate with coverage. `total_cost` states one estimated
  total for the task's work: its own usage plus the whole shared pool,
  never a divided share, with `shared_with_tasks` naming the other tasks
  that count the same pool (sums across tasks overlap by it). Text
  output leads with it. Exit 3 when
  missing, conflicting or wholly unbound ownership exists: a task whose
  sessions hold measured responses but nothing attributed or shared
  reports those sessions as `unbound_usage` instead of a silent
  `missing assignments: none`. Every command accepts `--json`.
- `interventions`, `intervention show`, `verdict add|list|groups`, `recurrence`,
  `session-attr add`: the judge interface (README "Judge interventions").
  Intervention ids are `sub:<user message native id>` (genuine or interrupt
  user messages, and synthetic ones that Chromeria records as agent-sent,
  created by `agent`) and `ev:<session_key>/<family>/<native id>` (Codex
  `turn_aborted` or Grok `turn_ended` interrupted/cancelled; a denied
  permission that is `user-rejected`
  on Claude or kindless elsewhere). An interrupt user message within one
  second after a `user-rejected` denial in the same session is the same
  act and no intervention. `permission-rule` and `automode-blocked` denials are
  `policy_denials`, never interventions. `created_by` precedence: T3 turn origin
  (Claude uuid), then V2 origin by harness, native session and message/turn id,
  then a headless entrypoint (`agent`); for interrupts and rejections a
  T3 child thread, or a top-level thread not opened by a person
  (`launched`: V2 `createdBy` agent or system, or no evidence; V1
  threads and V2 legacy imports take the first turn start's actor), is
  `unknown`; then `user-rejected` (`user`), an
  interactive entrypoint (`user`), and for interrupts and rejections a
  top-level T3 thread (`user`); otherwise `unknown`. A batch naming one intervention, behavior and judge twice is
  rejected. Intervention text and
  context are read from native logs on request, redacted, never stored.
  `intervention show` returns whole turns (`--turns-before`, `--turns-after`,
  `--items`, `--chars`, `--text-chars`) and echoes them as `window`.
  `interventions`, `recurrence` and `verdict groups` default to
  `created_by` `user` and `agent`; `recurrence` headline numbers combine them,
  `by_created_by` gives each alone, and yes verdicts outside the chosen
  values appear only under `other_created_by`. Per-origin recurrence numbers
  are not additive with the headline when a group mixes origins: the combined
  group can span multiple sessions even when neither origin does alone.
  A verdict batch is validated whole; `recurrence` counts a group as
  recurring when its yes verdicts span two or more sessions.
- `publish --task ID|--session KEY [--prices schedule.json] --repo owner/name --pr N|--commit SHA`:
  render or post the summary comment; `--dry-run` prints the comment
  without posting, and `--dry-run --json` prints a JSON payload with the
  same summary data, the rendered body, and an explicit target naming the
  task or sessions plus any repo, PR or commit. The body leads with a
  title, one headline (the `total_cost` total, responses, sessions, wall
  time), a per-model table and one flags line from existing diagnostics
  and coverage gaps; snapshot, prices, coverage, counter semantics and
  reconciliation sit in one collapsed details block. Task summaries carry the
  same usage, models, shared rows, outcome, coverage, scope, diagnostics,
  measured set, snapshot and cost as task JSON. Session summaries name
  their limited scope and carry no task outcome.
- `claims --session claude:ID`: read-only claim audit of one Claude Code
  session's final report. Resolves the transcript through `sources.path`,
  numbers tool calls and received messages (user-role text and
  `queued_command` attachments), and asks Claude Opus 5.5 once, through
  `claude -p` with no tools, hooks, MCP servers or saved session, for
  claims with cited numbers. Citations naming no evidence are dropped; a
  claim with none left is `unsupported`. Output: `claims` (claim, status
  `backed` or `unsupported`, calls, reason), `total`, `unsupported`,
  `unsupported_rate`, `invalid_citations`, `models`, `cost_usd`. Exit 2 for
  an unknown or non-Claude session, an unreadable transcript, no final
  report, or a failed model call. The ledger is not written.
- `event search WORD... [--since] [--until] [--harness] [--project]
  [--limit N]`: read-only search of event name, target and assistant
  excerpt, and user message excerpts; every word must appear in one record,
  case-insensitively and literally. Output: `machine`, `words`, `filters`,
  `ledger_synced_at`, `total`, `truncated`, `matches` (newest first: `type`
  `event` or `user_message`, `id`, `at`, `session`, `project`, `family`,
  `anchor`). Exit 0 with or without matches, 2 for a missing ledger or no
  words. Native logs are not read and the ledger is not written.
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
`complete` flag requires zero missing and zero conflicting bindings, source
availability `available` (no ledger-only or changed in-scope events), and no
recorded active work, missing native sessions/usage or unbound child sessions.
Captured delegations without explicit child ownership are coverage gaps too;
the delegation edge alone never assigns the child's usage to the task.
Conflicts between whole-session and user-message ownership stay shared and incomplete.
Active task turns, crashes and missing evidence stay visible alongside the
arithmetic.

## Report identity, time and acceptance

- `snapshot_id` is a SHA-256 over the selected task, sorted sessions,
  response counters with semantics and model/effort/turn, assignments,
  outcome, task turns, delegations, source cutoff, price schedule and coverage
  evidence (including source availability, the ledger-only event count,
  the session keys that are not available, and the source ids that are
  not readable),
  and ownership that another task records in the same session. `source_cutoff`
  is the max native `imported_at` backing the scope. Repeats without
  ledger changes keep the identity; changed evidence changes it. The
  `measured` set names the task, sessions and attributed/shared/
  unassigned response ids with the cutoff. No prompt or transcript
  content participates.
- `acceptance_state` is explicit, defaulting to `unknown` when no outcome
  row exists. Process success, zero exit or a successful task turn never
  implies acceptance. The T3 adapter completes `origin='t3'` tasks whose
  thread tree links a PR snapshot with state `merged` (proof is the PR
  URL); every other PR state and Issue-only links stay an explicit
  `unknown`. A T3-owned row advances only while it matches the
  `t3_outcome_provenance` snapshot; any other pre-existing row is
  human-owned and survives re-import.
- `diagnostics` holds task-turn scoped detector counts plus session
  context counts kept explicitly separate. `time` holds task-turn elapsed
  with its source, or unavailable when no task turn timing exists, with
  the whole-session span labeled session context, never a task-only fact.

## Sourced pricing

Without `--prices`, task reports price from T3's LiteLLM rate table
(`$T3CODE_HOME/userdata/usage-model-rates.json`, default `~/.t3`) when it
names an observed model; the bundled `agent_observer/prices.json` stays as
the offline fallback per model. Converted entries serve the observed
counter semantics (TTL-split cache writes for Claude-only models, flat
otherwise; 200k-token long tier when T3 names it) and carry their T3 key;
the winning source stays labeled on `source_url` with the bundled URL on
`fallback_source_url`. An absent or unusable T3 table prices everything
from the bundle.

`agent_observer/prices.json` supplies dated, sourced standard API list-price
rates for exact supported model identities. It is an equivalent-cost estimate,
not a subscription invoice; discounts and service-tier premiums are excluded. A caller schedule is JSON with `source_url` (http(s) or file),
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
user-message `phase`, an exact-turn `capture task-turn --phase` label when supplied,
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

## Chromeria/T3 attribution (`sync --harness t3`)

T3 state is read from a live database copy or snapshot, never written:
`$T3CODE_HOME/userdata/chromeria-v2.sqlite` (default `~/.t3`) opens
read-only when present, else `state.sqlite`, and `sync --source` accepts
an explicit state file. Chromeria seeded the V2 file from `state.sqlite`,
so it also holds the frozen V1 tables; legacy threads keep their V1 ids,
so one read covers both without double counting. The mirror is a full
re-read every sync; workload bindings are durable ledger rows.

- Chromeria V2: thread payload `lineage` gives parent and root; V2 ids
  carry no `sub.` prefix. `pullRequests` snapshots are PR links (a V2
  snapshot replaces the V1 row for the same thread and PR). Each provider
  thread's strong `nativeThreadRef.nativeId` is a native Claude, Codex,
  Grok or OpenCode session of its thread. Each run names its user message;
  its attempts name provider turns, whose `nativeTurnRef` joins the native
  message or turn in that exact harness session. `createdBy=user` from
  `web` or `mobile` is a person; `agent` or `system` is an agent. Missing
  or conflicting identity stays unknown and is counted in actor coverage.
  Older synthetic refs that have no native counterpart stay unmatched.
  Native Claude uuid evidence takes precedence. Legacy copies of V1
  messages (`user` from `server`) are no evidence. No prompt text or hash
  is used for attribution. Prompts classified at import keep their excerpt.

- Turn origin: `thread.turn-start-requested` `actor_kind` (`client` human,
  `server` dispatch) joins the native Claude user-message uuid through
  `projection_turns`. A listed Claude `sdk` prompt is `genuine` on client
  turns and `synthetic` on server turns, at import and by backfill
  (backfilled rows gain their turn but no excerpt). Scaffolding, command
  and interrupt verdicts fail closed; unlisted records (typed prompts,
  Router `sdk-cli` workers) never move; unknown stays unknown.
- Thread trees: `sub.<parent>.<suffix>` ids (nesting allowed) group under
  their root. Each linked PR or Issue becomes one `repo#N` task
  (`origin='t3'`); every ledger session in the tree (cursor sessions plus
  Claude subagent children plus rotated sessions found by turn uuid) binds
  whole to every tree task. One-link trees attribute exclusively.
  In a multi-link tree, a Claude or Codex response belongs to one PR when
  the paths its tool calls name lie in a checkout of that PR's repository
  on its head branch (T3 snapshot `headBranch`; checkouts recorded in
  `checkouts` at sync so removed worktrees still match). A response
  matching no PR or several stays unplaced and outside every headline.
  Other harnesses, and links without a head branch, keep whole sharing.
  Ghostty bodies and unknown cursor shapes are skipped.
- Outcomes come from the PR snapshot (`merged` completes); exit codes are
  never read.
- Spawns and messages: every `sub.` thread in `projection_threads` records
  its parent, depth (count of `sub.` levels) and creation time. Every
  `user` message whose first line is Chromeria's
  `[Message from <title> (thread <id>)]` records sender id, target thread
  and time; Chromeria adds that line only when the target is not the
  sender's child. V2 children record their lineage parent and depth; a V2
  agent message with a `senderThreadId` into a thread that is not the
  sender's child records the same fields. `thread-misuse` reads these two
  tables only and follows the recorded parents for chains.

## Codex model context across imports

Each source preserves the native model/effort context at its imported byte
offset. An append resumes that context; deferred fallback usage preserves the
context at each record, including model changes. Older sources, or offsets
advanced by an older reader, replay once in place to repair model metadata
without deleting ownership or adding response counters. Unchanged sources then
retain the fast path. No model is inferred when native context is absent.
