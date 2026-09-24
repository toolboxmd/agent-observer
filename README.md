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
| Model Router | `~/.local/state/model-router/jobs.db` (read-only) | ownership: jobs become tasks, invocations become attempts |

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

Model Router jobs need no extra work: `sync` binds each job's worker and
dispatcher sessions to a task through the router's own records. For work
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
is never divided.

## GitHub summaries

`publish --task T-42 --repo o/r --pr 7` renders one aggregate comment
(models, effort, tokens, span, diagnostic counts) and creates or updates the
single Observer-owned comment on that PR or commit (`--commit <sha>`).
`--dry-run` prints it without posting. `publish --dry-run --json` instead
prints parseable JSON with the same summary data, the rendered body, and an
explicit target naming the task or sessions plus any repo, PR or commit.
Only aggregates leave the machine.

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
