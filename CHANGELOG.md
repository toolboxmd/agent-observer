# Changelog

## [Unreleased]

### Added

- Import Cue decisions and settled ContextMD reads read-only; report procedure reads by entry, harness, machine and AgentsMD version against session-wide holdout, with source and target gaps.

### Fixed

- Parse sectioned AgentsMD direction identity, Codex native durations and Read calls, and Grok MCP timing and outcomes. Retain read success state and count only successful procedure reads; replay older sources to repair stored evidence (#128).

## [1.3.0] - 2026-10-19

### Added

- Discover Chromeria bare Codex and Grok homes by default; report trace truncation, unresolved Codex identity conflicts and unavailable health scans; recognize Claude request-replay metadata; faster availability and diagnose queries

### Changed

- Judge moments JSON `opening` is `null` when unknown, and `false` for interrupts and rejections (was wrongly `true`)
- `health` exits 4 when its process scan is unavailable (previously it crashed)

### Fixed

- `health` scans only processes its checks use, so T3 or a non-English locale cannot make it unavailable
- A file reached through two paths in one sync is imported under one source row

## [1.2.6] - 2026-10-15

### Changed

- Count Claude queued inputs once without changing message identities

## [1.2.5] - 2026-10-15

### Changed

- Keep schema-3 queries readable before occurrence metadata migration

## [1.2.4] - 2026-10-15

### Changed

- Hydrate valid empty OpenCode assistant text without treating it as missing

## [1.2.3] - 2026-10-15

### Changed

- Retain native Codex Extension web.search evidence and repair existing indexes

## [1.2.2] - 2026-10-14

### Changed

- Index native conversation events and attribute actors by identity, keeping sidechain restrictions, Codex user text, and cleared OpenCode text aligned with those records. Codex orders every row by its complete-line position. Native ids keep the raw metadata ordinal, and a Codex privacy reimport of the same source updates that stored position from the first record without replacing the event or its verdict. A fork moves copied history by that file position on an append, a full replay, and a repair. A ledger imported before that chronology still moves on its stored metadata ordinals, and the replay then stores file positions. A repeat in the same source keeps that source's first position, and another file records another occurrence rather than moving the original. Competing response witnesses stay unplaced. A timeline orders the assembled rows from a spanning source before it applies a limit. Copied fork history keeps each native id, including two explicit ids that share a raw metadata ordinal, stores that record's file position, and retains the reads and fallback usage that attribute its responses. Only a ledger imported before privacy 9 rewrites those locators. A copied row matches the id its writer stored, including a file-position fallback, a historical ordinal fallback, and an id-less abort, and lookup still reads that historical id. The source is complete only when every retained locator is inside the file. A requested set stays unknown when the sources disagree about what preceded a shared identity, and each source sequence is kept. A timeline leaves sessions with no Codex order in timestamp order, applies a supported Codex order before the limit, and marks a cross-session order unknown. Opposite observations of the same native evidence count once, and that direction stays unknown. A failed replay rolls back so the next source cannot commit a partial replacement.

## [1.2.1] - 2026-10-14

### Changed

- publish reads plain JSON from gh when color is forced, and names the command and stderr when output is empty or not JSON (#88)

## [1.2.0] - 2026-10-14

### Added

- Report whether each indexed event still has a native source.

## [1.1.5] - 2026-10-14

### Changed

- Map AgentsMD versions from the clone root behind the global instruction link (#81)

## [1.1.4] - 2026-10-14

### Changed

- Resolve each shared checkout once per sync

## [1.1.3] - 2026-10-14

### Changed

- Writers take one ledger lock and a second writer waits up to 10 minutes before exiting 2, and text sync output names each failed source (#78)

## [1.1.2] - 2026-10-14

### Changed

- Default sync imports Codex rollouts under ~/.codex/archived_sessions as well as ~/.codex/sessions, and the same rollout basename in both is imported once (#77)

## [1.1.1] - 2026-10-13

### Changed

- Each forked Codex child rollout is its own session, keyed by its first session_meta id and linked to its fork parent; the copied parent history stays with the parent, and sync --full --harness codex moves children out of the parent sessions they were merged into (#69)

## [1.1.0] - 2026-10-13

### Added

- event search finds events and user messages by words in the local ledger, read-only and callable over SSH, with machine, ledger sync time, total and truncation so a remote judge can check recurrence on every machine (#70)

## [1.0.2] - 2026-10-13

### Changed

- Lookups (trace, interventions, intervention show, sessions, verdict list and groups, recurrence, diagnose, compare) open the ledger read-only, so they never take the write lock, never fail with 'database is locked' while sync writes, and never create or migrate a ledger (#71, #73)

## [1.0.1] - 2026-10-13

### Changed

- Glossary keeps Observer's own terms under the schema-3 names and points to Chromeria's system glossary; AGENTS.md lists connected projects (#66)

## [1.0.0] - 2026-10-12

### Changed

- Ledger schema 3 with the unified terms: moment becomes intervention (`interventions`, `intervention show`), submission becomes user message, dispatch becomes delegation (`capture delegation --child-thread`), attempt becomes task turn (`capture task-turn`, role `child`), origin becomes `created_by` (`human` becomes `user`), and checkpoint becomes usage snapshot. A schema-2 ledger migrates in place after a backup, keeping every row. Old commands, flags, values, the verdict key `moment` and read-only views `submissions`, `dispatches` and `attempts` work for one release. JSON report keys change (`task_turns`, `delegations`, `by_created_by`, `user_messages_inserted`), and fleet exports move to `/2` while still reading `/1` (#65)

## [0.15.1] - 2026-10-12

### Changed

- Read-only commands (`export`, `fleet`, `task`, `event`) open an idle ledger on SQLite builds that cannot open a WAL database read-only once its `-shm` file is gone, such as Apple's 3.43 behind macOS `/usr/bin/python3`; they fall back to a read-write open with `query_only` that never creates a ledger (#63)

## [0.15.0] - 2026-10-12

### Added

- Combine evidence across machines: export, fleet pull and fleet report, including judge recurrence merged across machines by judge and group; transcripts and moment text stay on their machine, only aggregates and the judge's one-line problems travel (#56)

## [0.14.0] - 2026-10-12

### Added

- Judge moments: `moments`, `moment show`, `verdict add|list|groups`, `recurrence` and `session-attr add` select every moment where a person or another agent acted on an agent, give an outside LLM judge each one with a configurable window of whole turns from the native logs, store its verdicts and report recurring problems for people and agents combined and apart; hook and rule denials are counted separately and Chromeria prompt origins now cover Codex as well as Claude (#60)

## [0.13.1] - 2026-10-12

### Changed

- Objective covers rocky, cavallo and bestia, each with its own Observer, with Chromeria V2 as a first-class source; transcripts stay on their machine and only aggregates travel; wording cut to fit the 1,500-character cap (#55)

## [0.13.0] - 2026-10-12

### Added

- Import Chromeria V2 threads, lineage, PR links, messages and prompt origins from chromeria-v2.sqlite (#53)

## [0.12.1] - 2026-10-10

### Fixed

- `sync` no longer exits 1 on a host without Model Router: the router reports 0 sources instead of a failed source. A missing ledger named with an explicit root or source still fails (#50)

## [0.12.0] - 2026-10-09

### Added

- `event show <id>...` (and `--ids-from`, `--json`) reads the full tool input, tool result and assistant text behind ledger events from the original Claude Code, Codex, Grok Build or OpenCode log, including subagent transcripts, with secrets redacted and nothing stored; a missing or changed source is reported per event (#48)

## [0.11.0] - 2026-10-01

### Added

- dispatch-modes compares planner dispatch with a dispatcher thread on Chromeria jobs: per mode, jobs, success rate, median time to the first worker and to an approving review, and tokens and cost per job, each with its sample size; missing data, including a job thread with no ledger session, prints unknown (#44)

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
