# Agent Observer

Local evidence about how coding agents use resources and produce outcomes.

The first milestone covers Codex, Claude Code, OpenCode, and Grok Build on one
Mac. Native records provide token usage and operational events. A local CLI will
connect those records to tasks and expose reports, timelines, and diagnostics.
ccusage is a development cross-check, not a runtime dependency.

Implementation has not started. The approved scope is recorded in the
[architecture specification](https://github.com/toolboxmd/model-router/issues/3).

Runtime databases and raw session records belong outside the repository.
Only sanitized fixtures may be committed. Model Router owns model selection;
AgentsMD and target projects retain workflow, authority, and proof.

GitHub summaries require an explicit publish command. Importing and analyzing
records never publishes them.
