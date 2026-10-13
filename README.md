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
agent-observer thread-misuse --since 2026-10-01  # deep Chromeria spawns, cross-thread messages
agent-observer compare --by agentsmd         # behavior per AgentsMD version
agent-observer health                        # live sessions Observer cannot see
agent-observer claims --session claude:<id>  # final-report claims checked against tool calls
agent-observer delegation-modes --since 2026-10-01  # planner delegation versus a dispatcher thread
agent-observer event show <id> [<id> ...]    # full tool input, result and assistant text from the native log
agent-observer event search wal checkpoint  # events and user messages containing every word, read-only
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
the exact interventions:

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

## Claim audit

`claims --session claude:<id>` lists each factual claim in a finished Claude
Code session's final report (the assistant text after the last prompt or
tool result) and marks it backed by numbered evidence or `unsupported`.
Evidence is every tool call with its result, plus every message the agent
received, since some hosts deliver a delegated reviewer's verdict as a
message instead of a tool result. One Claude Opus 5.5 call through the
`claude` CLI (no tools, hooks, MCP servers or saved session) names the
claims and cites evidence; the command then drops any citation that names
no evidence, so an invented citation leaves the claim `unsupported`. The
output ends with the unsupported rate.

```text
- [backed by #12 Bash] All 387 tests passed
- [backed by #20 message] The reviewer approved with no blocking findings
- [unsupported] The summary for existing records is byte-identical
unsupported: 1/3 (33%)
```

`unsupported` means the transcript shows no evidence, not that the claim is
false. Long inputs and results are clipped (6,000 characters, or the first
6,000 and last 3,000 of a result, shrinking for very long sessions), so a
claim resting on a clipped part can read as unsupported. Transcript text
is treated as untrusted data in the prompt, but an injected instruction can
still sway the model's judgment. The audit reads
the transcript at audit time and sends it to Anthropic through Claude
Code; it writes nothing to the ledger. Other harnesses, and claims in
Issues or PRs, are not audited yet.

## Chromeria thread misuse

`thread-misuse [--since <date>] [--until <date>]` lists two signals from
Chromeria threads that `sync --harness t3` imported:

- every thread more than two spawn levels below the thread a person
  started, with its chain from the root;
- every `message_thread` message into a thread that is not the sender's
  own child, with sender, target, how they relate in the spawn tree
  (`child to parent`, `to ancestor`, `to grandchild or deeper`,
  `within tree`, `across trees`) and the time.

```text
spawn chains deeper than 2 levels: 0
  none
messages into a thread other than the sender's own child: 1
- 2026-09-30T10:00:00Z child to parent: sub.<root>.worker-bbbb (worker) -> <root> (unknown)
```

Roles come from Prism's `<role>-<hex>` child id suffix and print
`unknown` otherwise, which includes every Chromeria V2 thread: V2 ids
name no role. The ledger keeps only thread and message ids and
times, never titles or message text. Both lists are evidence to inspect,
not verdicts; replies a child sends its parent through `message_thread`
appear as `child to parent`.

## Delegation modes

`delegation-modes` compares the two ways a Chromeria planner delegates a job:
starting workers and reviewers itself (planner mode), or starting one
`dispatcher-` thread that starts them (dispatcher mode). Per mode it prints
jobs, succeeded, success rate, median time to the first worker thread,
median time to an approving review, and tokens and list-price cost per job,
each with its sample size. A figure with no data prints `unknown (n=0)`.

```sh
agent-observer delegation-modes --planner <thread> --job owner/repo#N --since 2026-10-01
```

- A job is one planner thread and the PR its children link, or the Issue
  when they link no PR. `--job` accepts either. The job's threads are the
  planner's direct children whose subtree links it.
- The mode comes from the child thread names: a `dispatcher-` child makes a
  dispatcher job, other role names (`worker-`, `reviewer-`, `retry-`,
  `escalation-`) a planner job. Older children without a role name leave
  the mode `unknown`.
- Times start at the planner turn that created the job's first child. The
  approving review is the first `review/independent` success status or
  approving review on the PR, read through `gh`. A job succeeds when that
  approval exists or the PR merged; when GitHub is unreachable, success is
  `unknown`.
- Tokens and cost cover every thread in the job's subtrees plus the planner
  turns that coordinated it: a turn that a child's report started, or in
  which the planner created or messaged a child. A thread or planner turn
  serving several jobs is split evenly between them. If any of those threads
  has no session in the ledger (a Grok child, or a thread not yet synced),
  the job's tokens and cost print `unknown` and leave the mode's sample.

Thread trees, turns and links come from the V1 tables in T3's state
database, read-only at report time, so `delegation-modes` covers only
threads from before Chromeria's V2 move on 2026-10-09. Usage comes from
the ledger, so run `sync` first.

## Event content

The ledger keeps no file contents, tool outputs or full assistant text
(see Privacy). When an analysis needs them, for example to check how a PR
body begins, `event show` reads them again from the host's original log
for the events you name, prints them, and stores nothing.

```sh
agent-observer trace --session claude:<id> --family tool_call --json   # event ids are the "id" field
agent-observer event show 31995 31997 --json
sqlite3 observer.db "SELECT id FROM events WHERE ..." | agent-observer event show --ids-from - --json
```

Each event returns `status` and, when it is `ok`:

- `input` and `result`: the full tool input (command, heredoc body, patch)
  and the full tool result, plus `tool`, the native tool name. Read, file
  change, Skill and permission events resolve to the tool call behind them.
- `assistant_text`: the assistant text written between the previous tool
  result or prompt and the call, when there is any.
- `text`: the full text of an `assistant_message` event.
- `record`: the native record, for lifecycle, compaction and other events
  with no tool call.

The other statuses carry no content: `source_missing` (the log or OpenCode
database is gone), `source_changed` (the log shrank or was replaced since
the last sync), `not_found` (the log no longer holds that native id) and
`no_such_event`. The exit status is 3 when any event is not `ok`.

It works for Claude Code, Codex, Grok Build and OpenCode, including
subagent transcripts, because every event keeps its source and native id.
Grok tool content comes from the session's `updates.jsonl`. OpenCode
change detection is limited to the database still existing and holding
the part. Known secret shapes are redacted as in the ledger; heredoc
bodies, patches and outputs are shown in full, so treat the output as
private. Each source is parsed once per run: 1,000 live events across all
four hosts took 4.4 seconds on rocky.

## Event search

`event search` finds events and user messages that contain every given
word, case-insensitively, newest first. It searches what the ledger keeps:
event names and targets (commands and paths), assistant excerpts and user
message excerpts, all redacted at import. It reads no native log and never
writes, so another machine can call it over SSH, for example to check
whether a problem came back on every machine:

```sh
ssh cavallo agent-observer event search database locked --since 2026-10-01 --json
```

Filters: `--since`, `--until`, `--harness`, `--project` (the project
directory contains it) and `--limit` (default 50). The JSON carries
`machine`, `ledger_synced_at` (the newest import, so a caller knows how
current the answer is), `total`, `truncated` and `matches`. Each match has
`type` (`event` or `user_message`), `id` (the event id for `event show`, or
the user message's native id), `at`, `session`, `project`, `family` and
`anchor`, about 160 characters around the first matching word. No matches
exit 0 with an empty list; a missing ledger or no words exits 2.

## Judge interventions

Some behaviors, such as a person correcting an agent, need judgment that
pattern matching cannot supply. Agent Observer prepares the evidence for
an outside LLM judge and stores the judge's answers; it never runs a
model itself. [docs/judge-corrections.md](docs/judge-corrections.md) is
a reference judge prompt for corrections.

An intervention is one point in a main session where someone acted on the
agent: a `prompt`, an `interrupt`, or a `rejection` of a tool call. Its
`created_by` is the source label: who acted, from evidence only. A message
another agent sent (a parent's brief, a reviewer's findings, a child's
report) is a `prompt` intervention created by `agent` when Chromeria records
it as agent-sent. Corrections from a person and from another agent both
count, so selection and reports default to `user` and `agent`, with the
two reported apart and combined; `unknown` interventions are counted but left
out.

- `user`: Chromeria recorded a person starting the turn or sending the
  prompt, the session ran in a terminal or desktop app (`cli`,
  `Codex Desktop`, `codex-tui`), or an interrupt or rejection stopped a
  top-level Chromeria thread that a person opened, or a session outside
  Chromeria (Claude's `user-rejected` included).
- `agent`: Chromeria recorded an agent or the server sending it, or the
  session ran headless (`sdk-cli`, `codex_exec`). Other synthetic input,
  such as reminders, is no intervention.
- `unknown`: no evidence either way, for example a prompt in a delegated
  child thread from before Chromeria V2, a Grok or OpenCode prompt (their
  Chromeria text does not match the native prompt), or any interrupt or
  rejection in a child thread or in a top-level thread an agent launched
  (`thread` `child` or `launched`), which an agent can cause. A
  top-level thread counts as person-opened from Chromeria V2's
  `createdBy`, or, for V1 threads and V2's legacy imports of them, from
  who started its first turn; a ledger not synced since that evidence
  existed reads every top-level thread as `launched`. Codex
  prompt origins come from Chromeria V2 only, so older Codex child
  prompts stay `unknown`.

Hook and permission-rule denials and auto-mode blocks are policy, not a
person. They are never interventions and are counted as `policy_denials`.
Claude writes a rejected tool call twice, as the denial and as an
interrupt in the same second; that is one intervention.

```sh
agent-observer interventions --since 2026-09-14 --unjudged correction --json   # default --created-by user --created-by agent
agent-observer intervention show sub:claude:<uuid> ev:codex:<id>/lifecycle/turn_aborted:<turn> --json
agent-observer verdict add --json < verdicts.jsonl
agent-observer verdict list --behavior correction --verdict yes --json
agent-observer verdict groups --behavior correction --json
agent-observer recurrence --behavior correction --since 2026-09-14 [--attr arm=holdout]
agent-observer session-attr add --json < arms.jsonl     # {"session": "claude:<id>", "name": "arm", "value": "holdout"}
```

`interventions` lists the selected interventions and counts every intervention in scope by
`created_by` and kind, so filtered interventions stay visible as numbers. An intervention
marked `opening` comes before anything the agent did in its session (a
first prompt, a child's brief), so it cannot correct that agent. `intervention show`
reads each intervention from the native log: its full text (including text
inside Chromeria wrapper tags) and whole turns around it. A turn is a
prompt or interrupt (with its `created_by`) plus the agent's messages and
tool calls up to the next one. `before` lists turns oldest first, each
opened by its prompt; `after` lists the intervention's own turn first, each
closed by the next prompt. The window is a parameter, echoed back as
`window`, so a judge can measure its accuracy against context size on a
labeled sample: `--turns-before 0 --turns-after 0` is the intervention alone,
the default `1`/`1` adds the turn it answers and the agent's reply, and
larger values add more turns. `--items` caps agent messages and tool
calls per turn (each turn reports how many it `omitted`), `--chars` cuts each of them, and `--text-chars` (default
20,000) bounds prompt text separately, so a context limit never cuts the
person's words. Its statuses match `event show`,
plus `no_such_intervention`; the exit status is 3 when any intervention is not `ok`.

A verdict is `yes`, `no` or `unclear` for one intervention, behavior and judge;
`yes` carries a one-line `problem` and an optional `group`. Observer
validates a batch whole and stores it only when every line is valid.
Adding the same intervention, behavior and judge again replaces the verdict,
so grouping can run as a second pass. `recurrence` reports the yes
verdicts, the problems (groups), the problems that recur in two or more
sessions, and repeats per group, with first and last seen, sessions and
hosts, plus judge coverage by `created_by` and the policy denials. Its
headline numbers combine `user` and `agent` interventions (each group shows
how many of each), and `by_created_by` repeats the counts for each source
alone; `--created-by` narrows or widens the set. When more than one judge labeled the scope, it requires
`--judge`. `interventions --unjudged B --judge J` lists the work left for one
judge; `verdict list` returns stored verdicts with their problem lines,
which the grouping pass reads. A batch that names the same intervention,
behavior and judge twice is rejected. Yes verdicts on interventions outside
the reported `created_by` values appear only in `other_created_by`, never in the
headline numbers.

Intervention text, problem lines and session attributes stay on this machine:
`publish` never includes them.

## Ownership and tasks

Put `observer_task_id` in related Router jobs' prepared task JSON to connect
them to one Observer task. `sync` binds their dedicated worker and dispatcher
sessions using Router records, preserving captured task metadata and outcomes.
Without that field, each job retains its legacy `router:<request-id>` task. For work
outside Model Router, a coordinator records ownership through its existing
briefs and handoffs:

```sh
agent-observer capture create-task --task T-42 --project myapp --title "Fix login" --issue https://github.com/o/r/issues/42
agent-observer capture assign --user-message <native prompt id> --task T-42 --evidence "brief 2026-09-23"
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
separately. Missing ownership, conflicting ownership, active task turns,
crashes, missing native sessions, unbound child sessions and incomplete coverage stay visible; arithmetic reconciliation
alone never claims completeness. Acceptance is explicit only: a zero exit
or successful task turn never implies an accepted outcome.

Task reports carry a stable snapshot identity (`snapshot_id`, a SHA-256
over the selected task, sessions, response counters, assignments, outcome,
task turns, delegations, source cutoff and price schedule) and a measured set (task, sessions
and attributed/shared/unassigned response ids). Repeated rendering without
ledger changes keeps the same identity; new evidence changes it. Time and
diagnostics are task-turn scoped when turn timing exists, otherwise labeled
unavailable with the whole-session span kept explicitly as session context.

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
price table is needed. Every command except `sync`, `capture`, `verdict add`
and `session-attr add` opens an existing ledger read-only, using one consistent
database snapshot, so reports and lookups never wait for a running `sync`. Run
`sync` first to create or upgrade the ledger; reporting never migrates it. A strict read-only sandbox may
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
or `capture task-turn --phase implementation` for the entire observed turn.
Reports partition attributed usage by phase and show reasoning versus other
generated output, plus recorded tool/MCP activity. A long turn spanning phases
stays `mixed`, and missing phase ownership stays `unclassified`. Tool counts
are observations, not individual token bills; activity without turn ownership
remains labeled session context. This supports investigating inefficiency
without claiming that reasoning, tool use and implementation are disjoint work.

For a provably dedicated session outside Router, `capture assign-session
--task T-42 --session <key> --exclusive --evidence <delegation-ref>` binds the
whole session. It rejects existing ownership conflicts. Do not use it for a
conversation that has served, or will serve, other tasks. The installed
Observer Skill owns the complete capture and delivery procedure.

## Several machines

Each machine runs its own Observer over its own ledger. Transcripts stay on
their machine; only aggregates travel.

```sh
agent-observer export                      # this machine's export, JSON on stdout
agent-observer fleet pull bestia cavallo   # fetch their exports over SSH
agent-observer fleet report --expect rocky,cavallo,bestia
```

`export` writes session usage by model and counter semantics, plus each task's
outcome, GitHub identifiers and session keys. It never includes prompts,
assistant text, excerpts, titles, local paths or branch names. Task ids travel
only when they are identifiers such as `owner/repo#12` or `T-42`; a free-text
id stays home and is counted in `withheld_tasks`. Judge evidence (see Judge
interventions) travels as counts and identifiers, per session: interventions by `created_by`
and kind and policy denials, and per behavior and judge the verdict counts
and judged interventions by `created_by`; plus each group's id, the judge's one-line problem and its
judged-yes interventions (id, session key, `created_by`, time). Intervention and transcript
text never leave the machine; the problem line is the judge's own words,
redacted for known secret shapes. `fleet pull`
runs `agent-observer export` on each host over SSH (`--remote-command` when
the CLI is not on the remote PATH). It stores the result as
`fleet/<machine>.json` beside the ledger, mode 600. It stores nothing when SSH
fails or any field of the output fails validation, and warns when a host
reports a machine name other than its own. Machine names come from
`$AGENT_OBSERVER_MACHINE` or the short hostname.

The machines are your own, reached over SSH keys you control. Validation
guards against corrupt or buggy exports: every field is type- and
range-checked, strings that could break report lines are refused, and `pull`
stores only an export it can render. It does not make a hostile host safe to
pull from.

`fleet report` combines this machine's live export with the stored ones. Each
session counts once. Sessions found on several machines, and machines
expected with `--expect` but absent, are listed and make the exit status 3.
Each machine shows its export and ledger sync times, so stale evidence is
visible. A task's combined usage is the whole sessions touching it, each once,
with the number it shares with other tasks. Exact per-task attribution stays
with `task show` on each machine. Recurrence combines across machines:
groups with the same id under the same judge (a shared judge run grouped
them) merge, and a problem that
appears in one session on each of two machines recurs. A behavior judged
by different judges on different machines is reported per judge and
listed under `unmerged_behaviors`. One rule covers every combined count:
a session reported by several machines contributes its usage, interventions,
denials, verdicts and judged interventions once, from the first of those machines
by name. Headline numbers count user and agent
interventions, as `recurrence` does. Nothing pulls on a schedule.

Exports use `agent-observer.fleet-export/2`. During the switch to ledger
schema 3, `pull` and `fleet report` also read `/1` exports from machines
still on Observer 0.15 and translate them (`moments` to `interventions`,
`origin` to `created_by`, `human` to `user`). Upgrade every machine, then
the `/1` reader goes in the next release.

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

## Ledger schema 3

Observer 1.0 uses ledger schema 3, which renames stored terms to match the
rest of the system: `submissions` became `user_messages`, `dispatches`
became `delegations` (`owning_user_message`, `child_thread`, `child_turn`),
`attempts` became `task_turns` (`color_requested`; role `worker` is now
`child`), and `verdicts.moment_id` became `intervention_id`.
`assignments` names its columns `user_message_id` and `task_turn`.
The `t3` identifiers, `sub:` and `ev:` ids and `tasks.origin` keep their names.

The first command that opens a schema-2 ledger read-write (such as `sync`)
copies it to `observer.db.schema-2.bak` next to it, then migrates it in
one transaction; a failure rolls back and leaves schema 2. Read-only
commands ask for a `sync` first. Capture, task and verdict rows survive:
they cannot be rebuilt from native logs.

For one release the old names keep working: read-only views
`submissions`, `dispatches` and `attempts` with their schema-2 columns
(and `worker` roles), the commands `moments`, `moment show`,
`dispatch-modes`, `capture dispatch` and `capture attempt`, the flags
`--submission`, `--worker`, `--worker-turn`, `--attempt` and `--origin`
(with `human`), and the verdict key `moment`.

Every Observer copy on a machine (Claude Code, Codex and Grok plugins,
other tools' `agent-observer` paths) must move to 1.0 together. Observer
0.15 refuses a schema-3 ledger and suggests moving it aside: do not.
That would leave capture and verdict rows outside the active ledger.
Upgrade the old copy instead.

## Privacy

The ledger is private and local: its directory is created or hardened to
mode 0700, and the database plus SQLite sidecars to mode 0600. It stores
counters, identities, paths, hashes and short excerpts needed for the
detectors; it never stores file contents, tool outputs or AgentsMD
preference contents. `event show` reads those from the native logs on
request and prints them without storing them. Two tables hold free text
that tools other than the harness write: `verdicts.problem`, a one-line
problem an outside judge wrote after reading private session content
(redacted for known secret shapes), and `session_attributes.value`.
Both stay on this machine and are never published. Only sanitized fixtures are committed to this
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
serialized. A tool call keeps the full path or command it names, up to
65,536 characters, so an audit can list what a session read before an
edit. Known secret shapes (API keys, tokens, private keys,
secret-named assignments and flags, URL passwords) become `[redacted]`
first, patches keep only their file header lines, and file bodies a
command writes through a heredoc, here-string, `echo` or `printf` (by
redirect or `tee`) are omitted. Known limits: writes through `exec >f`,
a redirected group or subshell (`{ echo x; } >f`), or a program such as
`python3 -c` keep their payloads. When these rules change, the next sync fully re-imports
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
