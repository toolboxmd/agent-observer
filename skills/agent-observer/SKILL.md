---
name: agent-observer
description: Measure what agents actually did on this machine. Use when someone asks about agent token usage, time, models, repeated reads or commands, tests edited after failures, interrupts or corrections, invisible sessions, a task's or PR's consumption, or how behavior changed between AgentsMD versions, models or harnesses.
---

# Agent Observer

Agent Observer reads native Codex, Claude Code, OpenCode and Grok Build
records, Model Router's ledger and Chromeria/T3 Code state into one
private local ledger. It never changes those records, never calls a model,
and posts only its cost comment, on a PR being delivered or when asked.

Resolve this `SKILL.md` to its real path. The plugin root is two directories
above its `agent-observer` directory; run `<plugin-root>/bin/agent-observer`
from any working directory. The ledger lives at
`~/.local/state/agent-observer/observer.db` unless `--db` or
`AGENT_OBSERVER_DB` names another.

## Commands

Run `sync` first; it is incremental and takes seconds after the first run.

| Question | Command |
| --- | --- |
| Refresh the ledger | `sync` (or `sync --harness claude`) |
| Recent sessions for a project | `sessions list --project <name> [--since 2026-09-01]` |
| One session's usage, events and prompts | `sessions show --session <key>` |
| What happened inside a session or task | `trace --session <key>` or `trace --task <id>` |
| Repeated work and behavior incidents | `diagnose [--project P] [--since D] [--detector test_edit_after_failure]` |
| Behavior by AgentsMD version, model, harness or project | `compare --by agentsmd` (or `model`, `harness`, `project`) |
| Sessions Observer cannot see | `health` |
| Which claims in a Claude Code session's final report its tool calls back | `claims --session claude:<id>` (one Opus call per run) |
| Usage of a task with explicit ownership | `task show --task <id> [--prices schedule.json]` |
| Summary comment on a PR or commit | `publish --task <id> --repo owner/name --pr N [--prices schedule.json]` |

Add `--json` to any command for structured output. `publish --dry-run
--json` returns a JSON payload with the summary data, the rendered body,
and an explicit target (task or sessions, plus repo, PR or commit).

## Attribution (automatic, no bookkeeping)

`sync` attributes Chromeria/T3 Code work on its own from T3 state: prompts
typed in T3 Code count as human submissions, one task per linked Issue or
PR (`owner/repo#N`) owns its thread tree's sessions, and a merged PR
marks the task complete. When one thread serves several PRs, each Claude
or Codex response counts for the PR whose checkout it worked in; the rest
is reported as unplaced, never added to a PR's cost. Never run `capture` commands: per-prompt
assignment, phase labels and outcome recording stay out of agent workflows.
If reporting fails, record the measurement gap in the existing handoff and
continue other authorized work. `task show` keeps the stable task id across
handoffs; exit 3 means missing, conflicting or wholly unbound ownership
remains. Repeats without ledger changes keep the same snapshot; new
evidence changes it. Dispatchers consume `task show --json`, or the
coordinator's saved JSON when a read-only sandbox cannot open the ledger;
`price_schedule` in the export preserves the selected rates for repeat
valuations with `--prices`.

## Reading the results

- Token totals are each harness's own total. Cache and reasoning buckets
  mean different things per harness and are never added across harnesses.
  A scope mixing counter semantics carries no top-level raw totals; read
  the per-semantics groups instead. Totals are usage, not billing.
- Skill events name skills by validated identifier only: a free-text
  skill title never persists as a target, name or detail.
- `diagnose` returns candidates with event references, not verdicts. A
  reread after a compaction, a different range or an edit is not reported;
  a remaining repeat may still be legitimate. Heuristic detectors say so.
- `compare` is observational: groups differ in period, project and task
  mix. Report sample sizes with every figure and treat differences as leads.
- Unknown stays unknown: a missing version, model or usage is not zero.
  Unpriced models stay unknown with priced coverage visible; partial cost
  subtotals never stand in for a complete total. Native reported cost,
  list-price estimates and subscription spending are separate.
- A task report carries its snapshot, cutoff, measured sessions and
  responses, explicit acceptance, active work and task-scoped time and
  diagnostics with session context labeled. Repeats keep the snapshot;
  new evidence changes it.

## Boundaries

- Keep transcripts, prompts, tool arguments and file contents out of any
  reply meant for others; quote aggregates and event references instead.
- `publish` writes to GitHub. Run it on the PR of the task you are
  delivering, or when a person asks, and only against the repository and
  PR or commit of that task or request.
