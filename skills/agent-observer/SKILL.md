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
| Usage of a task with explicit ownership | `task show --task <id> [--prices schedule.json]` |
| Record ownership for work outside Model Router | `capture create-task`, `capture assign`, `capture dispatch`, `capture attempt`, `capture outcome` |
| Summary comment on a PR or commit | `publish --task <id> --repo owner/name --pr N [--prices schedule.json]` (explicit request only) |

Add `--json` to any command for structured output. `publish --dry-run
--json` returns a JSON payload with the summary data, the rendered body,
and an explicit target (task or sessions, plus repo, PR or commit).

## Task lifecycle (agent-operated)

Observer owns accounting; AgentsMD owns workflow and acceptance, Model
Router owns execution evidence. Keep the stable task id across handoffs.
AgentsMD provides one trigger for this procedure; do not copy these steps into
its core, delivery or orchestration references. If capture or reporting fails,
record the measurement gap in the existing handoff and continue other authorized work.

1. Coordinator creates or resumes the task:
   `capture create-task --task T-42 --project myapp --title "Fix login" --issue https://github.com/o/r/issues/42`.
   Reuse the id for follow-ups on the same task. A different task gets a
   different id even when it shares the conversation.
2. Assign every genuine submission:
   `capture assign --submission <native prompt id> --task T-42 --phase implementation --evidence "brief 2026-09-23"`.
   Bare native ids resolve when unique. A submission with no binding is
   reported missing and never inherits the prior task. A submission
   serving several tasks needs `--shared` on every binding; its usage
   stays shared whole and is never divided. Non-shared multi-task
   bindings report as conflicting.
3. Dispatcher records the edge separately from observed execution:
   `capture dispatch --submission <owning id> --worker <thread> [--requested-model M] [--requested-effort E]`.
   Requested route stays separate from observed execution. Put the task id
   in Router's prepared JSON as `observer_task_id`; several jobs can share
   it while keeping distinct request ids. Sync imports their invocations and
   dedicated worker sessions. It never assigns the shared planner session.
   For a separately created, provably dedicated session outside Router, use
   `capture assign-session --task T-42 --session <key> --exclusive --evidence <dispatch-ref>`.
   Use this only when the entire session belongs to this task and will not
   be reused for unrelated work. Existing conflicting ownership is refused.
   For shared conversations, assign individual genuine submissions instead.
4. Worker records each observed turn, updating its state at completion:
   `capture attempt --task T-42 --turn <harness:turn> --role worker --phase implementation --state complete`.
   Record the actual work phase (planning, implementation, review, correction,
   or another supported phase), independently of model reasoning tokens.
   A phase labels the entire turn. Use `mixed` when a long turn crosses phases;
   never relabel all earlier usage as the latest phase. Router's dedicated
   sessions can use their recorded stage; missing labels remain unclassified.
   The coordinator records the candidate and proof/review references with
   `capture outcome --task T-42 --state <complete|active|failed|...> --candidate <sha> --proof <ref>`.
   Include review and repair work through their own ownership bindings.
   Acceptance stays explicit; a zero exit or successful attempt never
   implies acceptance. Session-only work cannot imply a task outcome.
5. Coordinator inspects coverage before delivery:
   `task show --task T-42` surfaces attributed, shared and unassigned
   buckets, per-model rows with semantics, missing and conflicting
   bindings, active attempts, crashes, snapshot and cutoff, measured
   sessions and responses, usage and observed tool/MCP activity by phase,
   reasoning/other-output splits, task-scoped diagnostics and time, native cost
   and any sourced estimate. Exit 3 means missing or conflicting
   ownership remains. Repeats without ledger changes keep the same
   snapshot; new evidence changes it.
   Dispatchers consume this same command with `--json`, or the coordinator's
   saved JSON when a read-only sandbox cannot open the ledger. Retain the export:
   `price_schedule` and `price_schedule_id` preserve the selected rates and
   their identity alongside the valuation. Use the saved schedule with `--prices`
   to repeat a valuation of unchanged evidence. Keep the original export when
   task evidence changes. A schedule date is not proof of a current price check
   or of the price effective when each response ran.
6. Render and publish only at authorized delivery:
   `publish --task T-42 --repo owner/name --pr N --dry-run` first, then
   the explicit post. `--prices schedule.json` supplies an offline
   list-price schedule override. The bundled dated schedule prices supported
   exact model identities by default; uncovered usage stays unknown. `publish
   --session <key> --dry-run` is session scope only and never a task
   outcome.

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
- `publish` writes to GitHub. Run it only when a person asks for it, and
  only against the repository and PR or commit they named.
