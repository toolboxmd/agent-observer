# Changelog

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
