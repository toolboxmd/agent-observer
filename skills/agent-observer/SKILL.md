---
name: agent-observer
description: Measure what agents actually did on this machine. Use when someone asks about agent token usage, time, models, repeated reads or commands, tests edited after failures, interrupts or corrections, invisible sessions, a task's or PR's consumption, or how behavior changed between AgentsMD versions, models or harnesses.
---

# Agent Observer

Agent Observer reads native Codex, Claude Code, OpenCode and Grok Build
records and Model Router's ledger into one private local ledger. It never
changes those records, never calls a model, and posts nothing unless asked.

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
| Usage of a task with explicit ownership | `task show --task <id>` |
| Record ownership for work outside Model Router | `capture create-task`, `capture assign`, `capture outcome` |
| Summary comment on a PR or commit | `publish --task <id> --repo owner/name --pr N` (explicit request only) |

Add `--json` to any command for structured output.

## Reading the results

- Token totals are each harness's own total. Cache and reasoning buckets
  mean different things per harness and are never added across harnesses.
  Totals are usage, not billing.
- `diagnose` returns candidates with event references, not verdicts. A
  reread after a compaction, a different range or an edit is not reported;
  a remaining repeat may still be legitimate. Heuristic detectors say so.
- `compare` is observational: groups differ in period, project and task
  mix. Report sample sizes with every figure and treat differences as leads.
- Unknown stays unknown: a missing version, model or usage is not zero.

## Boundaries

- Keep transcripts, prompts, tool arguments and file contents out of any
  reply meant for others; quote aggregates and event references instead.
- `publish` writes to GitHub. Run it only when a person asks for it, and
  only against the repository and PR or commit they named.
