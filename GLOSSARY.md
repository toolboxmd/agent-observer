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

- Accepted completion: the first explicit outcome row with acceptance_state complete, at its first recorded updated_at. Re-recording complete to add proof or metadata never moves that timestamp. Failed, cancelled and unaccepted tasks never acquire an invented accepted completion time. A known accepted completion without submission timing is reported as known but elapsed-unavailable; negative endpoint differences are rejected and qualified.
- Submission-to-accepted-completion: elapsed time from the earliest explicit bound submission timestamp to the first accepted completion. Reported only when both endpoints have explicit evidence with a non-negative difference.
- Elapsed-so-far: for active tasks, the source cutoff minus submission time, labeled with the named cutoff. Never an accepted completion time.
- Partial execution span: owned-turn timing from diagnostics. Labeled partial, never accepted completion.
- Observed wall time: measured duration of one attempt or session. Never called active thinking time; no unobserved phase is inferred from it.
- Union span: merged covered duration of explicit attempt windows in one session; gaps excluded, never first start to last end.
- Waiting interval: gap between one attempt end and the next attempt start for the same known session or the same Router request, derived only from explicit timestamps with no overlap. Parallel overlap is not waiting and produces no waiting row.
- Reconciled execution: one execution reported from both Router and native evidence, merged only on explicit turn/session/time evidence and counted once for durations and attempt counts. Shared sessions and unknown ownership stay qualified and never auto-merge.
- Failure class: observed category of a failed production attempt from explicit terminal and stage only: timeout, stall, provider, infrastructure, implementation, verification, or unknown when classification evidence is missing. Router reason describes why an invocation was launched and never classifies a later failure.
- Production attempt: an attempt with state complete, failed, or quota_blocked provider exhaustion. quota_blocked counts as a provider failure inside failed/total and stays visible separately as quota. Cancellations of unknown intent, separately counted crashes, active and unknown states stay outside failed/total production counts. Intentional cancellation needs explicit intent evidence; bare Router cancelled stays intent unknown.
- Job outcome: a Router job status behind the task, defined separately from attempt failure counts. A provider exhaustion followed by a successful pool move is an attempt failure plus a separate job outcome, not a failed accepted task.
- Recovery: for one failed execution, the failure-to-next-attempt-start duration inside the same compatible identity (same Router request_id or same known non-shared session), the first subsequent completed progress at any stage with its stage label measured at its end when observable, and the stage-specific outcome with repeated failed counts in that stage chain only. Recovered, active and repeated counts need the failed stage and the candidate stage both known and equal; a dispatcher or other different-stage completion never recovers implementation work and unknown stage never matches. A new attempt starting alone is not successful recovery; unresolved recovery stays active or unknown. Attempts from another request or shared/unknown sessions never pair.
- Usage source coverage: for Router attempts with null usage_json, whether attributable native usage exists for the same session plus a turn or time match (turn match required for shared sessions). Null with attributable native usage is source coverage, never zero usage or complete loss. Measured but unpriced usage is distinct from missing usage.
