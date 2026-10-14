# Agent Observer Glossary

Terms only Agent Observer uses. Terms shared across the user's system, such as
task, harness, session, turn, machine, user message, created by, intervention,
correction, policy denial and recurring problem, are defined once in
Chromeria's system glossary,
[`toolboxmd/chromeria` `GLOSSARY.md`](https://github.com/toolboxmd/chromeria/blob/main/GLOSSARY.md).

**Assignment**:
An explicit binding of one user message to one task.

**Delegation**:
The recorded link from an owning user message to a child thread, with the
requested model, effort and reason.
_Avoid_: dispatch (the old command name, kept as an alias)

**Task turn**:
One observed turn toward a task, with its role (parent or child), Color and
outcome.
_Avoid_: attempt (the old command name, kept as an alias), run attempt

**Response**:
One atomic model usage record keyed by its native response id.

**Usage snapshot**:
A cumulative counter reading; stored but never summed.
_Avoid_: checkpoint

**Joint response**:
One atomic response that serves several tasks; kept whole.

**Scope**:
The selected set of responses a total is computed over.

**Coverage**:
Which event families a harness's native evidence supports.

**Ledger-only event**:
An indexed event whose native source file no longer exists, so the ledger still names it and reading its text fails with that status.
_Avoid_: missing event

**Source availability**:
For one session, whether each indexed event is backed by a live or relocated native source, or is ledger-only, and why. A session with no indexed events is still ledger-only or partial when a source backing the session, its responses, or its submissions is gone or replaced.
_Avoid_: completeness, coverage

**List-price estimate**:
Native usage valued at a model vendor's dated API price list, distinct from
subscription spend.

**Work phase**:
The explicitly recorded purpose of a turn or dedicated execution, independent
of reasoning and tool activity; mixed or unclassified when evidence cannot
separate it.

**Verdict**:
An outside judge's yes, no or unclear on one intervention for one behavior,
with a problem line and group for yes.

**Machine export**:
One machine's aggregate document: session usage by model and counter
semantics, task outcomes and session keys; never transcripts, prompts, titles
or paths.
_Avoid_: dump, backup

**Fleet report**:
Machine exports combined, each session counted once, with missing machines and
doubly reported sessions listed.

**Import source**:
A harness id for records Agent Observer imports rather than reads from a
session: `t3` is data from Chromeria (with `t3_*` tables and
`tasks.origin = 't3'`), keeping T3's name because the stored format is T3's;
`router` is jobs from the retired Model Router.
_Avoid_: chromeria (as an id)
