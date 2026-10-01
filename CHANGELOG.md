# Changelog

## [0.10.0] - 2026-10-01

### Added

- thread-misuse lists Chromeria spawn chains deeper than two levels and message_thread messages into threads other than the sender's own child, with ids, roles where known and times; Chromeria trees re-import on the next sync (#42)

## [0.9.0] - 2026-10-01

### Added

- A session shared by several PRs is split per response: each PR's cost counts only the Claude and Codex responses that worked in its checkout; the rest is shown as unplaced (#43)

## [0.8.0] - 2026-10-01

### Added

- diagnose counts only incidents inside --since/--until; only user rejections count as human corrections; agents publish the cost on the PR they deliver. Claude sources re-import on the next sync

## [0.7.1] - 2026-09-30

### Fixed

- Stored commands keep everything after a heredoc's terminator. A
  `>` elsewhere on a Codex `exec` line or in an interpreter program no
  longer turns the rest of the command into `[content omitted]`; file
  bodies written through a heredoc stay omitted. Affected sources
  re-import on the next sync (#39)

## [0.7.0] - 2026-09-30

### Added

- `claims --session claude:<id>` lists the factual claims in a Claude Code
  session's final report and marks each backed by a cited tool call or
  received message, or `unsupported`, with the unsupported rate. One
  Claude Opus 5.5 call names the claims; citations to evidence that does
  not exist are dropped (toolboxmd/agentsmd#162)

## [0.6.0] - 2026-09-29

### Added

- Keep the full path or command of every tool call, up to 64 KiB, for
  Claude Code, Codex, OpenCode and Grok Build, so an audit can list what a
  session read before an edit; Codex tool calls now record their command
  (#36)
- Redact known secret shapes from stored paths and commands, keep only
  the file header lines of patches, and omit file bodies that a command
  writes through a heredoc, `echo` or `printf`

### Fixed

- Grok Build paths containing `sk-` inside a word (such as `risk-tiered`)
  are no longer mangled by secret redaction

## [0.5.1] - 2026-09-29

### Fixed

- Retry the post-creation release read with bounded exponential backoff
  (6 attempts, 1 s start, x2, max 8 s, 30 s budget, ported from Model
  Router), so a just-created release is never judged on one read and the
  Marketplace wake-up is not skipped

## [0.5.0] - 2026-09-28

### Added

- Lead `task show` and published summaries with one estimated total per
  task: its own usage plus the whole shared pool, which is never divided
  (`total_cost` in JSON), so a shared-only task no longer reads $0.00

### Changed

- Published PR summaries are glanceable: a headline, a per-model table
  and one flags line, with evidence in a collapsed details block

## [0.4.1] - 2026-09-28

### Changed

- A release now wakes Marketplace promotion at once instead of waiting for its hourly schedule

## [0.4.0] - 2026-09-27

### Added

- Attribute Chromeria/T3 Code work to Issues and PRs automatically from
  read-only T3 state: T3 turn origins classify `sdk` prompts as genuine
  (typed) or synthetic (dispatched), thread trees become `repo#N` tasks
  with whole-session ownership (multi-link trees stay shared, never
  divided), and merged PR snapshots mark tasks complete
- Price from T3's LiteLLM rate table when present, with the bundled
  schedule as the labeled offline fallback
- Flag tasks whose sessions are known but have no bound usage instead of
  reporting `missing assignments: none` with exit 0

### Removed

- The agent-operated task lifecycle from the Skill; query use only, no
  agent runs capture commands

## [0.3.0] - 2026-09-24

### Added

- Connect task-scoped model costs, Router ownership and outcome evidence

## [0.2.0] - 2026-09-24

### Added

- Agent Observer: one private ledger of Codex, Claude Code, OpenCode, Grok Build and Model Router records, with usage, timelines, behavior detectors, comparison by AgentsMD version, session visibility, GitHub summaries and enforced ledger privacy

## [0.1.0] - 2026-09-18

### Added

- Establish approved Agent Observer direction and initial development scaffold
