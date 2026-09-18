# Contracts: ledger, event, capture, and query surface

Versions: schema 1, event contract 1, capture contract 1. Stored in
`schema_meta`. Later adapters and reports extend these tables; they do not
rewrite core accounting.

## Ledger

SQLite, stdlib only. Tables: `sources`, `turns`, `responses`, `submissions`,
`tasks`, `assignments`, `dispatches`, `attempts`, `outcomes`, `events`,
`import_errors`, `schema_meta`. Raw session files and the database live
outside Git. Only sanitized fixtures under `tests/fixtures` are committed.

## Codex adapter (`agent_observer/codex.py`)

Input: one Codex session JSONL file. Supported record types: `session_meta`,
`event_msg`, `response_item`, `token_usage_record`, `turn_context`,
`compacted`, `world_state`, `inter_agent_communication_metadata`. Unknown
types are quarantined in `import_errors`; prior valid data is kept.

Accounting rules:

- One `token_usage_record` usage block is one atomic response keyed by
  `response_id`. Reimport verifies identical counters and never re-sums.
- `turn_token_usage` and `thread_token_usage` are checkpoints stored per row
  and never summed. Scope totals sum `usage` only.
- `input_tokens` includes cached input; `output_tokens` includes reasoning.
  Unknown counters stay null, never zero.
- The `compacted` embedded `latest_token_usage_record` repeats an existing
  response and is never inserted. The compaction event records whether its
  `response_id` resolves to a known response.
- Parent and worker files share `session_id` but have disjoint `response_id`
  sets; totals add across scopes without merging rows.

Submission rule: a genuine submission is a `response_item` message with
`role` user, `content_item_kinds` exactly `["user.text"]`, and text that does
not start with `<send_user_message_question_reply>`. Skill, plugin, and
environment scaffolding has other kinds and is excluded. Tool results and
assistant rows never create submissions.

Tool joins: calls (`function_call`, `custom_tool_call`) join results
(`function_call_output`, `custom_tool_call_output`) only on equal `call_id`.
Completed `CommandExecution` and `McpToolCall` items keep native ids and stay
unmatched rather than guessed into a wrapper or inner match. `FileChange`
items record path, change kind, and content size/hash; contents never enter
the ledger. `turn_aborted` marks the turn cancelled and keeps its account
provisional.

Read and skill evidence comes only from observed operations:
`CommandExecution.parsed_cmd` entries of type `read`. A target ending in
`SKILL.md` is a `skill_read`; prose that mentions a skill file is not
evidence. Skill invocation and quota attribution stay unknown for Codex in
this slice; see `trace --capabilities`.

## Capture contract (CLI `capture`)

- `create-task`: stable task identity with project, family, title, Issue link.
- `assign`: binds one genuine submission to one task with attempt, phase, and
  evidence. Every genuine submission needs a binding, including followups.
  A submission with no binding is reported missing and never inherits the
  prior task. Binding the same submission to several tasks with `--shared`
  keeps the response joint; tokens are shown under each task as shared and
  never divided. Bindings without `--shared` to several tasks are reported
  as conflicting.
- `dispatch`: links an owning submission to a worker thread with requested
  route, policy version, and reason. Requested route stays separate from the
  observed model and effort on the attempt.
- `attempt`: observed turn with role parent or worker, model, effort, timing,
  terminal state, and whether output was usable.
- `outcome`: explicit acceptance state per task. A zero process exit never
  implies acceptance.

Outcome states: `complete`, `active`, `cancelled`, `failed`,
`quota_blocked`, `crashed`, `unknown`. Crashed attempts with no usable output
are counted separately and excluded from useful-work comparisons. Repairs and
corrections are preserved on the outcome.

## Query surface (CLI `task`, `trace`)

- `task list`, `task show --task ID`: attributed, shared joint, and
  unassigned token buckets with response counts; missing and conflicting
  assignments; crash count; outcome; attempts; dispatches. Exit 3 when
  missing or conflicting ownership exists. JSON with `--json`.
- `trace --task ID | --turn ID [--family F]`: ordered events with tool
  join status (joined, unmatched results, unanswered calls). JSON with
  `--json`. `trace --capabilities` prints the observed coverage declaration.

Reconciliation: attributed plus shared plus unassigned equals the selected
scope total. A report never claims completeness from arithmetic alone; the
`complete` flag requires zero missing and zero conflicting bindings.

## Verifier separation

ccusage is a pinned development cross-check only. Runtime code never imports
or executes it. `tests/test_cli.py` asserts that no runtime module imports
or calls it.
