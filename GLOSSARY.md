# Glossary

- Submission: one genuine user-authored request inside a harness session.
- Assignment: explicit binding of one submission to one task.
- Dispatch: recorded edge from an owning submission to a worker thread.
- Attempt: one observed turn toward a task with role, route, and outcome.
- Response: one atomic model usage record keyed by native response ID.
- Checkpoint: cumulative counter snapshot; stored but never summed.
- Joint response: one atomic response that serves several tasks; kept whole.
- Scope: the selected set of responses a total is computed over.
- Coverage: statement of which event families native evidence supports.

- Task: an explicitly owned work outcome, spanning submissions, sessions and Router jobs.
- List-price estimate: native usage valued at a dated provider API schedule, distinct from subscription spend.
- Work phase: explicitly recorded purpose of a turn or dedicated execution, independent of reasoning and tool activity; mixed or unclassified when evidence cannot separate it.

- Moment: one point in a main session where a person acted (prompt, interrupt or rejection of a tool call), selected for an outside judge.
- Origin: who acted at a moment (human, agent or unknown), from evidence only.
- Policy denial: a tool call refused by a hook, a permission rule or auto mode; never a moment or a correction.
- Verdict: an outside judge's yes, no or unclear on one moment for one behavior, with a problem line and group for yes.
- Recurring problem: a verdict group whose yes verdicts span two or more sessions.
