# Agent Observer

Local evidence about how coding agents use resources and produce outcomes.

Agent Observer reads the records Codex, Claude Code, OpenCode and Grok Build
already keep on this Mac, plus Model Router's job ledger, into one private
SQLite ledger. It reports usage, timelines, repeated work and behavior
incidents by session, task, model, project and AgentsMD version. It never
changes the harnesses' records, never calls a model, and posts nothing to
GitHub unless you run `publish`.

## Install

Agent Observer ships through the [toolboxmd marketplace](https://github.com/toolboxmd/marketplace)
like AgentsMD and Model Router:

```sh
codex plugin add agent-observer@toolboxmd          # Codex
claude plugin install agent-observer@toolboxmd     # Claude Code
grok plugin install agent-observer --trust          # Grok Build
```

The plugin carries the CLI (`bin/agent-observer`, Python 3.11+, standard
library only) and a Skill that tells agents when and how to use it. From a
checkout, run `bin/agent-observer` directly.

## Quick start

```sh
agent-observer sync                          # import everything; later runs are incremental
agent-observer sessions list --project myapp --since 2026-09-01
agent-observer diagnose --since 2026-09-01   # repeated work and behavior incidents
agent-observer compare --by agentsmd         # behavior per AgentsMD version
agent-observer health                        # live sessions Observer cannot see
```

Every command accepts `--json`. The ledger lives at
`~/.local/state/agent-observer/observer.db`; `--db` or `AGENT_OBSERVER_DB`
names another.

## What it reads

| Source | Location | Usage granularity |
| --- | --- | --- |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | per response (`token_usage_record`); older rollouts from rising `token_count` totals |
| Claude Code | `~/.claude/projects/*/*.jsonl` and subagent transcripts | per message, counted once across streamed blocks |
| OpenCode | `~/.local/share/opencode/opencode.db` (read-only) | per completed assistant message |
| Grok Build | `~/.grok/sessions/*/*/` | per prompt completion |
| Model Router | `DURABLE_RUNNER_STATE_DIR`, `~/.local/share/durable-runner`, and legacy `~/.local/state/model-router` (read-only, deduplicated) | explicit task ownership, jobs and invocations |

Counter meanings differ by harness (cached input is part of input on Codex
and Grok, separate on Claude Code and OpenCode). Each response keeps its
native counters and its harness's own total; reports never add cache or
reasoning buckets across harnesses. A scope mixing counter semantics
exposes no top-level raw bucket totals: usage appears only under
per-semantics groups (`by_semantics`), and task reconciliation compares
response partitions and per-semantics sums. Unknown values stay unknown.

Each session also records which AgentsMD it ran with: the AGENTS.md SHA-256
from AgentsMD's Project Direction hook, the instruction text a host
embedded, or the versioned plugin path of a Skill it loaded, resolved to a
release through the AgentsMD repository's tags.

## Evaluating AgentsMD releases

`compare --by agentsmd` groups sessions from every project by the AgentsMD
version they ran under and reports, per version with sample sizes: tokens
and elapsed time per session, genuine prompts per session, interrupts,
AgentsMD reading cost, and incidents per detector. `diagnose` lists the
incidents with session, project, time and event references, so you can open
the exact moments:

| Detector | Finds |
| --- | --- |
| `test_edit_after_failure` | test files edited between a failing and a passing test run (`only_tests_changed` when no code changed) |
| `repeated_read`, `repeated_skill_load` | the same file range or Skill loaded again with no edit or compaction in between |
| `repeated_command`, `repeated_failure` | a command run three or more times, or failing twice in a row |
| `permission_seeking` (heuristic) | a turn ending with a question asking permission |
| `human_correction` (heuristic for wording) | interrupts, permission denials, and prompts that open with a correction |
| `large_tool_output` | tool results of 50 KB or more, or truncated |

Comparisons are observational: groups differ in period, projects and task
mix, so differences are leads to inspect, not causal effects. Detector
output is a candidate list with evidence, not a verdict.

## Ownership and tasks

Put `observer_task_id` in related Router jobs' prepared task JSON to connect
them to one Observer task. `sync` binds their dedicated worker and dispatcher
sessions using Router records, preserving captured task metadata and outcomes.
Without that field, each job retains its legacy `router:<request-id>` task. For work
outside Model Router, a coordinator records ownership through its existing
briefs and handoffs:

```sh
agent-observer capture create-task --task T-42 --project myapp --title "Fix login" --issue https://github.com/o/r/issues/42
agent-observer capture assign --submission <native prompt id> --task T-42 --evidence "brief 2026-09-23"
agent-observer capture outcome --task T-42 --state complete --candidate <sha> --proof <ref>
agent-observer task show --task T-42
```

Every genuine prompt in a task's sessions needs a binding; a missing or
conflicting one is reported (exit 3), never inherited. A prompt serving
several tasks can be bound with `--shared`; its usage then stays shared and
is never divided. `task show` reports the same attributed, shared-joint and
unassigned partitions that publication uses, with per-model rows carrying
native input, cache-read, cache-write, output and reasoning buckets plus
exact counter semantics. Buckets are never added across semantics; a mixed
scope exposes per-semantics groups with no combined total. The attributed
headline excludes shared and outside-task usage; shared model rows render
separately. Missing ownership, conflicting ownership, active attempts,
crashes, missing native sessions, unbound workers and incomplete coverage stay visible; arithmetic reconciliation
alone never claims completeness. Acceptance is explicit only: a zero exit
or successful attempt never implies an accepted outcome.

Task reports carry a stable snapshot identity (`snapshot_id`, a SHA-256
over the selected task, sessions, response counters, assignments, outcome,
attempts with timing evidence, submission timestamps, dispatches, source cutoff
and price schedule) and a measured set (task, sessions
and attributed/shared/unassigned response ids). Repeated rendering without
ledger changes keeps the same identity; new evidence changes it. Time and
diagnostics are task-turn scoped when turn timing exists, otherwise labeled
unavailable with the whole-session span kept explicitly as session context.
`task show` also reports submission-to-accepted-completion time only with
explicit endpoints (active tasks show elapsed-so-far at the named source
cutoff), observed wall time per attempt/session/role with waiting intervals
for the same known session or Router request only, failures by class
from explicit terminal and stage with production denominators
including quota provider exhaustion and cancellation intent unknown
unless explicit, separate job outcomes,
per-failure recovery inside compatible identities, and attributable
Router usage source coverage. Parallel attempt
durations never become task elapsed time; Router and native rows for one
execution merge only on explicit turn/session/time evidence and count once.
Union span is merged covered duration with gaps excluded.

The bundled dated price schedule supplies standard API list-price equivalents
for supported exact model identities. `task show --prices schedule.json` and
`publish --task T-42 --prices schedule.json` override it with a sourced offline
schedule. Each response is priced under its native semantics. Unknown models,
unsupported semantics, missing counters or rates, cache-write TTL ambiguity and
unverified long-context tiers remain unpriced. The known subtotal and priced
response count remain separate from a complete estimate. These estimates do
not represent subscription spending or an invoice; native reported cost is
shown separately. Provider source URLs travel with each model's estimate.

Coordinators and dispatchers reuse `task show --task T-42 --json`; no Router
price table is needed. Task inspection and publication open an existing ledger
read-only, using one consistent database snapshot. Run `sync` first to create or
upgrade the ledger; reporting never migrates it. A strict read-only sandbox may
also prevent SQLite from opening a WAL ledger whose sidecars are absent. In
that case the coordinator exports the report and hands its saved JSON to the
dispatcher; do not relax permissions or bypass SQLite locking. Save that JSON
as valuation evidence. It includes
the exact selected `price_schedule` and its `price_schedule_id` (SHA-256 of
JSON with sorted keys and compact separators), alongside the costs, coverage
and task snapshot. `publish --dry-run --json` carries the same evidence;
the GitHub body shows the schedule identity and rates for reported models.
The local JSON retains the supplied schedule, including any custom metadata;
publication does not render arbitrary metadata or unused model entries.
Extracting `price_schedule` to a file and passing it to `--prices` reproduces
the valuation while the underlying task evidence is unchanged. Keep the original
export when usage or ownership changes; this is not a historical ledger replay.
Changing the schedule creates a new valuation identity, never an invoice.

The bundled schedule's `as_of` records when its prices were verified. Neither
that date nor a caller's `effective_date` selects rates by response time:
reports explicitly value usage using the selected schedule. Verify official
sources when adding a model/route, after announced changes or promotion expiry,
or when freshness affects a decision. Verify a promotional price before claiming
it is currently free. Reports retain the schedule date without implying a live
check; rendering is offline and no periodic updater runs.

Claude cache-write prices use the native 5-minute/1-hour breakdown, including
backfill for older imports. Muse Contributor Free uses its verified promotional
route price of $0; a paid-model proxy is not substituted as actual cost.

Work phase is separate from token type. Record `capture assign --phase review`
or `capture attempt --phase implementation` for the entire observed turn.
Reports partition attributed usage by phase and show reasoning versus other
generated output, plus recorded tool/MCP activity. A long turn spanning phases
stays `mixed`, and missing phase ownership stays `unclassified`. Tool counts
are observations, not individual token bills; activity without turn ownership
remains labeled session context. This supports investigating inefficiency
without claiming that reasoning, tool use and implementation are disjoint work.

For a provably dedicated session outside Router, `capture assign-session
--task T-42 --session <key> --exclusive --evidence <dispatch-ref>` binds the
whole session. It rejects existing ownership conflicts. Do not use it for a
conversation that has served, or will serve, other tasks. The installed
Observer Skill owns the complete capture and delivery procedure.

## GitHub summaries

`publish --task T-42 --repo o/r --pr 7` renders one aggregate comment
(models, effort, token buckets with semantics, sourced cost, snapshot,
outcome, coverage and task-scoped diagnostics) and creates or updates the
single Observer-owned comment on that PR or commit (`--commit <sha>`).
`--dry-run` prints it without posting. `publish --dry-run --json` instead
prints parseable JSON with the same summary data, the rendered body, and an
explicit target naming the task or sessions plus any repo, PR or commit.
Only aggregates leave the machine: no transcripts, prompts, excerpts,
paths or free-form repair/proof text. Candidate and proof render only as
public GitHub URLs, owner/name identifiers, PR numbers, hex SHAs or fixed
hashes; anything else renders as withheld. Repo, PR and commit targets are
validated before posting; session-only summaries name their limited scope
and never imply a task outcome. Publication is explicit only and offline
until posted; repeats update the owned comment in place.

## Launch requirements

A session is visible only if its harness writes its records:

| Host | Requirement |
| --- | --- |
| Claude Code | A session started from inside another Claude Code session inherits `CLAUDE_CODE_CHILD_SESSION` and saves no transcript. Unset the parent's `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID` and `CLAUDE_EFFORT` variables, or set `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`. |
| Codex | Keep `CODEX_HOME/sessions` writable and reachable; kits that isolate `CODEX_HOME` must keep their sessions directory (Model Router does). |
| OpenCode | Keep OpenCode's data directory (`~/.local/share/opencode`) reachable; do not override `XDG_DATA_HOME` for sessions you want observed. |
| Grok Build | Keep `GROK_HOME/sessions` reachable. |

`agent-observer health` reports live sessions that break these rules, with
the process, directory and fix.

## Privacy

The ledger is private and local: its directory is created or hardened to
mode 0700, and the database plus SQLite sidecars to mode 0600. It stores
counters, identities, paths, hashes and short excerpts needed for the
detectors; it never stores file contents, tool outputs or AgentsMD
preference contents. Only sanitized fixtures are committed to this
repository.

Prompt excerpts are kept only for genuine human prompts in the main
session: the human's own text up to the first tag-like marker (`<`
followed by a letter, `/` or `!`, or the first `<<<`),
whitespace-collapsed, at most 300 characters. Pasted blocks, injected
instructions and preference contents after such a marker are therefore
never kept. An assistant excerpt is the last 400 characters of each
assistant text block, kept only when that span contains no such
marker: Claude Code emits one assistant_message event per nonempty
assistant text block, so a message streamed as several blocks keeps
one excerpt per block. Codex instead concatenates the text items of
an assistant message and keeps one excerpt for the combined message.
No other adapter emits assistant_message excerpts. Error records keep only a fixed category and the sorted
top-level key names of the record, never its values. Event details keep
only allowlisted, correctly typed metadata (identifiers, paths,
commands, numbers, closed status and kind values), never titles,
messages, error text, outputs, content, arguments or other free text;
native free-text session titles are not stored. Skill event targets are
family-specific: `skill_read` and `skill_invoke` targets hold only a
validated native skill identifier (the same identifier rule as names),
so a free-text skill title or an installed directory path never
persists as a target; the installed skill file path lives only in
detail `skill_path`. Instruction identity is sanitized centrally:
direction statuses are exactly `ready`, `missing`, `stale`,
`potentially_stale`, `invalid`, `uninitialized`, `not_in_repository`,
hashes must be fixed-length hex digests, paths must be absolute and
marker-free, and only an approved allowlist of fields is ever
serialized. When these rules change, the next sync fully re-imports
affected sources, corrects older rows in place (including replacing
source-owned session identity and clearing omitted or invalid fields),
and replaces that source's prior import errors. Reports never add
counters across different semantics: mixed scopes expose `by_semantics`
with no combined total.

## Development

```sh
python3 -m unittest discover -s tests
```

[docs/contracts.md](docs/contracts.md) defines the ledger, the adapter
contract and counter semantics. ccusage 20.0.21 is a development
cross-check for token totals only; runtime code never imports or runs it.

## Project Direction

[Vision](VISION.md) · [Mission](MISSION.md) · [Objective](OBJECTIVE.md).
The approved scope is recorded in
[model-router#3](https://github.com/toolboxmd/model-router/issues/3). Model
Router owns model selection; AgentsMD and target projects retain workflow,
authority, and proof.
