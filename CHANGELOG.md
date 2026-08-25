# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.27] - 2026-08-25

### Changed

- Refined the conversation styling with clearer user messages, aligned agent
  headers, roomier bubble spacing, slimmer scrollbars, familiar tool-status
  icons, and a more distinct muted-gold secondary agent tone.
- Agent startup and prompt failures now use a clear error presentation while
  preserving literal adapter details safely.
- Corrected project and documentation links to the Wingmen repository.

### Fixed

- ACP prompt errors now remain retryable request failures instead of being
  mistaken for agent startup failures.
- A failed first relay turn no longer leaves stale task context behind, so a
  retry starts with the new request and receives the full operating context.
- Legacy saved thin-scrollbar settings migrate cleanly to the supported normal
  scrollbar.

### Removed

- Removed the low-value local `/clear` and `/agent` commands. Agent-provided
  commands with those names continue to work, and roster changes remain
  available when configuring the next workspace.
- Removed broken Discussions links from agent failure guidance.

## [0.6.26] - 2026-08-25

### Changed

- Updated the README to document the current roster, relay, Fully Auto,
  attachment, cancellation, privacy, and ACP adapter behavior.
- Added PyPI project links, license metadata, search keywords, and supported
  platform classifiers.
- Package verification now reports only the version from the freshly built
  release artifact, avoiding stale development-environment output.

## [0.6.25] - 2026-08-25

### Changed

- Agent message bubbles now use the space reclaimed from the removed selection
  rail, aligning directly with the conversation window's left edge.
- Agent bubble interiors now use one shared visual style; roster-specific teal,
  coral, violet, and aqua are limited to the bubble border rails.
- Replaced the remaining amber, olive, and orange accents with a cohesive
  avionics palette: teal, coral, violet, and aqua identify agents, while
  emerald and red remain distinct success and error signals.

- `@` is reserved for file attachments. Click a roster agent to select the
  recipient of the next normal message; prompt-level direct-message syntax was
  removed.
- The terminal UI now uses a single teal fighter-HUD theme, full-width Flash
  ribbons, compact message panels, and a wingmen-formation landing mark.
- Messages submitted behind active work remain in a bounded holding area above
  the prompt and enter the transcript only when their agent turn starts.
- Adjacent messages from the same agent render as one compact visual stack
  without repeating the header or divider between them.

### Added

- Each agent's first prompt now identifies its Wingmen roster collaborators.
- New ACP sessions receive concise operating instructions: avoid speculation,
  answer questions without starting work, and stay within explicitly requested
  scope.
- JSON-RPC requests are scoped to their owning ACP agent so one adapter cannot
  resolve another adapter's in-flight request when response IDs collide.

### Fixed

- Live tool changes no longer briefly resize the active agent message and jump
  the conversation window; tool history stays hidden behind its fixed one-row
  summary until deliberately focused.
- Relay indicators now distinguish the next message recipient (`→`), active
  work (`●`), and idle agents (`○`); completed turns no longer leave timers.
- Clicking an agent header no longer crashes the conversation view.
- Agent header backgrounds now use a stronger per-agent accent fill.
- Closing a workspace now terminates the complete ACP adapter process group.
- Unsupported saved Textual themes are migrated before styles load, preventing
  custom HUD variables from blocking startup.
- Slash-command completion keeps focus on Tab, previews the selected command in
  the prompt, and runs argument-free commands with a single Enter press.
- Once the prompt is visible, notifications consistently use the teal Flash
  ribbon for information, warnings, and errors.
- Focusing an agent thought no longer replaces its HUD rails with a full border
  or shifts the surrounding message layout.
- JSON-RPC rejects missing, extra, or malformed parameters instead of invoking
  handlers with partial arguments.
- Fuzzy path matching, result retention, and cache growth are bounded for
  repetitive queries and large repositories.

### Removed

- The blinking left-side message selection rail and its private animation
  timer; block navigation and selection continue without visual flashing.
- The unsupported `wingwomen` executable alias and the redundant `/about`
  command.

## [0.6.24] - 2026-08-24

### Added

- The two-agent relay now supports an unlimited-size roster. `-a/--agent` is
  repeatable (`wingmen run -a claude -a codex -a gemini`); `/agent
  list|add <agent>|drop <n>` changes the roster inside a running session.
- A detected-agent section in the launcher, populated from local agent
  detection. `space` now adds/removes an agent from a roster being built;
  `enter` launches that roster (or the highlighted agent solo if nothing is
  selected). Added a filter input.
- `f4` closes the current workspace (previously only
  `/wingmen:session-close`).
- Direct `c`/`p` keys to copy the highlighted block to the clipboard or into
  the prompt.
- Per-agent response headers, timestamps, work timers, and a compact rolling
  tool-activity line with focusable history.
### Changed

- Bare `wingmen` (no `-a`) now restores the last-used agent roster. If none is
  saved, it opens the agent store instead of silently auto-starting a relay
  from whatever agents happen to be detected — a fresh install now takes one
  extra step (pick a roster once) in exchange for never starting agents you
  didn't choose.
- Rebranded the project from Taiji to Wingmen: PyPI distribution `taiji-cli` →
  `wingmen`, primary command `taiji` → `wingmen`, relay safe word
  `[TAIJI:STOP]` → `[WINGMEN:STOP]`, duplicate-agent tag/display suffixes
  `-yin`/`-yang` → `-1`/`-2`, and the app icon `☯` → `✈`. Settings and session
  data now live under a `wingmen` config/state path instead of `taiji`;
  existing local settings and session history will not be picked up
  automatically. The old compatibility identity is no longer supported.
- Style tweak for compact prompt
- Fix for overly wide question text

### Removed

- The sidebar and persistent plan panel; the conversation now uses the full
  terminal width with one primary navigation surface.
- The project file tree and tree-mode file picker; file attachment is now a
  single fuzzy search flow.
- The custom block context-menu framework and nonessential block actions such
  as SVG export and maximize. Copying content remains available directly from
  the conversation with `c` and `p`.
- Telemetry: no more usage-event collection, and no more calls to the
  upstream author's PostHog project. The `statistics.allow_collect` setting
  is gone along with it.
- The "Your agent here — sponsor this project" tile in the store and the former
  testimonial/about commands (which were unrelated to agent conversations).
  Attribution to Will McGugan and the AGPL notice remain in the license.
- The "Recommended — Best of the bunch" store section, along with the
  `recommended` agent-schema field it read. It held a single agent, which was
  also listed again under Coding agents.
- Dead code: `gist.py`, the unused `Welcome` widget, an empty `post_welcome`
  no-op still scheduled on every session start, and two unreachable
  `Schema`/`Settings` UI helpers.
- The decorative quote/throbber loading option and store artwork; waiting
  states now use the conversation's compact loading blocks.
- The fixed-width column toggle and automatic copy-on-selection behavior;
  conversations use the full terminal width and explicit copy actions.
- Unreachable ACP diff-posting helpers and the ACP handler block from the
  Conversation widget; ACP dispatch now lives in a dedicated handler module.
- The standalone Settings screen and its F2/`ctrl+,` menu entry; runtime
  defaults remain centralized in the internal settings schema.
- Settings form metadata and validation scaffolding that only served the
  removed screen; the runtime schema now stores defaults and leaf keys only.
- The background version-check request and exit-time upgrade banner; the
  wrapper no longer performs unrelated network work.

### Fixed

- Tool-history summaries and focused tool-call boxes now align with their
  agent response content.
- The CLI no longer called the removed exit hook, and screen startup no longer
  queried the conversation before it was mounted.
- ACP shutdown now terminates and awaits subprocess tasks; relay peers stop in
  parallel instead of leaking background protocol tasks.
- Fuzzy file search now creates CPU workers lazily, cancels stale searches,
  releases workers on unmount, and performs index scoring off the UI loop.
- Updated the Codex integration to the current official
  `@agentclientprotocol/codex-acp` adapter and aligned its install action.
- The relay's turn-taking core (`RelayConversation`, née `DuplexConversation`)
  is generalized from exactly two agents to N; the two-agent behavior is
  unchanged (10 pre-existing tests pass with only an import/rename edit).
- `wingmen acp COMMAND PATH -d OTHER_PATH` silently discarded `-d` — the
  positional and the option were bound to the same parameter name.
- The block context menu's copy/maximize/etc. actions had no direct key
  binding, requiring three steps (engage cursor, open menu, pick a letter) to
  copy an agent's response.
- A plan update from the agent rendered twice — once inline in the
  conversation and once in the sidebar — because only one of the two message
  handlers stopped propagation.
- The footer could show "⏎ Send" and "⏎ Select" at the same time, implying
  two live behaviors for one key when only Send would actually fire.
- Relay hand-offs now include every public human and agent-message update the
  receiving agent has not seen since its previous turn, preventing a later
  agent response from arriving without the question that prompted it.
- Clicking blank space inside an attributed response no longer treats the
  response container as one of its own Markdown children.

## [0.6.23] - 2026-08-17

### Changed

- Increased the default automated relay safety limit to 100 turns.

## [0.6.22] - 2026-08-17

### Added

- Added non-blocking local detection for Claude, Codex, and Gemini ACP agents.
- Added first-run setup guidance when no preferred agent is detected.
- Added the configurable `--max-rounds` relay safety limit.

## [0.6.21] - 2026-08-17

### Added

- Added two-agent ACP relay conversations with human queuing, direct agent
  tags, pause/resume controls, bounded relay context, and `[TAIJI:STOP]`
  termination.
- Published the Taiji CLI distribution as `taiji-cli`.

## [0.6.20] - 2016-05-22

### Added

- Added Grok to store

## [0.6.19] - 2026-05-22

### Added

- Added context usage and context to status line


## [0.6.18] - 2026-05-17

### Fixed

- Fixed issue that affected OpenCode

## [0.6.17] - 2026-05-14

### Fixed

- Fixed issue with broken code display

## [0.6.16] - 2026-04-19

### Changed

- Added textual-diff-view, which started life here

### Added

- Added diff settings for annotations and wrapping

## [0.6.15] - 2026-04-05

### Changed

- Reduced resize lag

## [0.6.14] - 2026-03-30

### Fixed

- Fixed Claude Code ACP command

## [0.6.13] - 2026-03-30

### Fixed

- Fixed issues with auto-completing root paths

### Changed

- Updated claude acp adapter installer

## [0.6.12] - 2026-03-13

### Fixed

- Fix broken output with Typeguard dependency

## [0.6.11] - 2026-03-13

### Added

- Added prompt message to settings

## [0.6.10] - 2026-03-13

### Fixed

- Fixed for agent that send blank text update (Mistral)

## [0.6.9] - 2026-03-11

### Fixed

- Fixed tool calls not refreshing
- Fixed tool call ordering
- Fixed anchoring behavor

### Changed

- Re-enabled experimental GC management via Textual
- The `end` key will now scroll the conversation to the end, if the cursor is already at the end of the prompt 

### Added

- Added Cursor to store

## [0.6.8] - 2026-03-03

### Fixed

- Removed gc management, due to expected memory issues

## [0.6.7] - 2026-03-02

### Fixed

- Fix for throbber crash

## [0.6.6] - 2026-03-02

### Changed

- Style tweaks for tools
- Some GC optimizations to smooth startup and scrolling

## [0.6.5] - 2026-02-27

### Added

- Added experimental OpenClaw support

## [0.6.4] - 2026-02-26

### Fixed

- Fixed large plans not appearing

### Changed

- Additional style tweaks, restore success color for terminal tools, tweaked margins for blocks

## [0.6.3]- 2026-02-25

### Changed

- Updated and improved style for Plans and Terminal tools

## [0.6.2]- 2026-02-24

### Changed

- Reverted a dubious style change on the store page

## [0.6.1] - 2026-02-24

### Changed

- New index for fuzzy searching makes searches faster for large repos

## [0.6.0] - 2026-02-16

### Added

- Added project directory switcher
- Added sessions, sessions tabs, sessions screen

### Fixed

- Fixed handling of agents that post null responses (OpenCode)

### Changed

- Added semantic styled edge to diff view

## [0.5.38] - 2026-02-01

### Fixed

- Fixed issue with agents empty thoughts breaking the block cursor

### Changed

- PathSearch and SlashCommand inputs are now overlays to avoid moving conversation content

## [0.5.37] - 2026-02-01

### Fixed

- Fixed session resume

## [0.5.36] - 2026-01-30

### Added

- Added toad.db sqlite database for non-config data
- Added Resume dialog (currently experimental, as agents don't yet support ACP)
- Added setting to disable title blink

### Fixed

- Fixed issue with empty terminal tools

## [0.5.35] - 2026-01-21

### Added

- Added GitHub CoPilot

### Changed

- The launcher hotkeys will now launch the agent immediately, and not just highlight the agent

## [0.5.34] - 2026-01-16

### Added

- Added display of slash command hints
- Added /toad:clear slash command

## [0.5.33] - 2026-01-16

### Fixed

- Fixed character level diff highlights

## [0.5.32] - 2026-01-15

### Fixed

- Fixed broken text form the input in commands

## [0.5.31] - 2026-01-14

### Changed

- Fix for diff highlights
- Minor cosmetic things

## [0.5.30] - 2026-01-14

### Fixed

- Fixed Terminals not focusing on click
- Fixed tool calls not rendered
- Fixed Kimi run command
- Fixed permissions screen not dispaying if "kind" is not set

### Added

- Added reporting of errors from acp initialize call
- Added Interrupt menu option to terminals

## [0.5.29] - 2026-01-11

### Added

- Set process title
- Additional help content

## [0.5.28] - 2026-01-11

### Fixed

- Fixed crash when running commands that clash with Content markup

## [0.5.27] - 2026-01-10

### Changed

- Updated Hugging Face Inference providers

## [0.5.26] - 2026-01-10

### Fixed

- Fixed issue with missing refreshes

### Added

- Added Target lines, and Additional lines, to settings

## [0.5.25] - 2026-01-09

### Added

- Added F1 key to toggle help panel
- Added context help to main widgets

### Changed

- Changed sidebar binding to ctrl+b

## [0.5.24] - 2026-01-08

### Added

- Added sound for permission request
- Added terminal title
- Added blinking of terminal title when asking permission
- Added an error message if the agent reports an internal error during its turn

## [0.5.23] - 2026-01-06

### Fixed

- A few style issue: tree background, status line padding

## [0.5.22] - 2026-01-06

### Fixed

- Fixes for settings combinations not taking effect

### Changed

- Restored prompt history
- The `/about` slash command has been renamed to `/toad:about`, to crate a namespace for future Toad commands

## [0.5.21] - 2026-01-05

### Changed

- Settings screen will now expand to full width when the screen is < 100 characters
- Sidebar will float if focused and "hide sidebar when not in use" setting is True
- Replace mac and linux shell settings with a single setting (you may have to update this you have changed the default)

### Fixed

- A more more defensive approach to watching directories, which may fixed stalling problem

## [0.5.20] - 2026-01-04

### Changed

- Smarter filesystem monitoring to avoid refreshes where nothing has changed

## [0.5.19] - 2026-01-04

### Added

- Added surfacing of "stop reason" from agents.
- Added `TOAD_LOG` env var (takes a path) to direct logs to a path.

## [0.5.18] - 2026-01-03

### Fixed

- Fixed footer setting

## [0.5.17] - 2026-01-03

### Fixed

- Fixed prompt settings not taking effect
- Fixed tool calls expanding but not updating the cursor

### Added

- Added atom-one-dark and atom-one-light themes

### Changed

- Allowed shell commands to be submitted prior to agent ready

## [0.5.15] - 2026-01-01

### Added

- Added pruning of very long conversations. This may be exposed in settings in the future.

### Fixed

- Fixed broken prompt with in question mode and the app blurs
- Fixed performance issue caused by timer

## [0.5.14] - 2025-12-31

### Added

- Added optional os notifications
- Added dialog to edit install commands

### Changed

- Copy to clipboard will now use system APIs if available, in addition to OSC52
- Implemented alternate approach to running the shell

## [0.5.13] - 2025-12-29

### Changed

- Simplified diff visuals
- Fixed keys in permissions screen

### Fixed

- Fixed broken shell after running terminals

## [0.5.12] - 2025-12-28

### Fixed

- Fixed eroneous suggestion on buffered input 

## [0.5.11] - 2025-12-28

### Fixed

- Fixed tree picker when project path isn't cwd

## [0.5.10] - 2025-12-28

### Added

- Added a tree view to file picker

## [0.5.9] - 2025-12-27

### Changed

- Optimized directory scanning and filtering. Seems fast enough on sane sized repos. More work require for very large repos.
- Fixed empty tool calls with terminals

## [0.5.8] - 2025-12-26

### Fixed

- Fixed broken tool calls

## [0.5.7] - 2025-12-26

### Changes

- Cursor keys can navigate between sections in the store screen
- Optimized path search
- Disabled path search in shell mode
- Typing in the conversation view will auto-focus the prompt

### Added

- Added single character switches https://github.com/batrachianai/toad/pull/135

## [0.5.6] - 2025-12-24

### Fixed

- Fixed agent selector not focusing on run.
- Added project directory as second argument to `toad acp` rather than a switch.

## [0.5.5] - 2025-12-22

### Fixed

- Fixed column setting not taking effect

## [0.5.0] - 2025-12-18

### Added

- First release. This document will be updated for subsequent releases.


[0.6.20]: https://github.com/batrachianai/toad/compare/v0.6.18...v0.6.20
[0.6.18]: https://github.com/batrachianai/toad/compare/v0.6.17...v0.6.18
[0.6.17]: https://github.com/batrachianai/toad/compare/v0.6.16...v0.6.17
[0.6.16]: https://github.com/batrachianai/toad/compare/v0.6.15...v0.6.16
[0.6.15]: https://github.com/batrachianai/toad/compare/v0.6.14...v0.6.15
[0.6.14]: https://github.com/batrachianai/toad/compare/v0.6.13...v0.6.14
[0.6.13]: https://github.com/batrachianai/toad/compare/v0.6.12...v0.6.13
[0.6.12]: https://github.com/batrachianai/toad/compare/v0.6.11...v0.6.12
[0.6.11]: https://github.com/batrachianai/toad/compare/v0.6.10...v0.6.11
[0.6.10]: https://github.com/batrachianai/toad/compare/v0.6.9...v0.6.10
[0.6.9]: https://github.com/batrachianai/toad/compare/v0.6.8...v0.6.9
[0.6.8]: https://github.com/batrachianai/toad/compare/v0.6.7...v0.6.8
[0.6.7]: https://github.com/batrachianai/toad/compare/v0.6.6...v0.6.7
[0.6.6]: https://github.com/batrachianai/toad/compare/v0.6.5...v0.6.6
[0.6.5]: https://github.com/batrachianai/toad/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/batrachianai/toad/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/batrachianai/toad/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/batrachianai/toad/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/batrachianai/toad/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/batrachianai/toad/compare/v0.5.38...v0.6.0
[0.5.38]: https://github.com/batrachianai/toad/compare/v0.5.37...v0.5.38
[0.5.37]: https://github.com/batrachianai/toad/compare/v0.5.36...v0.5.37
[0.5.36]: https://github.com/batrachianai/toad/compare/v0.5.35...v0.5.36
[0.5.35]: https://github.com/batrachianai/toad/compare/v0.5.34...v0.5.35
[0.5.34]: https://github.com/batrachianai/toad/compare/v0.5.33...v0.5.34
[0.5.33]: https://github.com/batrachianai/toad/compare/v0.5.32...v0.5.33
[0.5.32]: https://github.com/batrachianai/toad/compare/v0.5.31...v0.5.32
[0.5.31]: https://github.com/batrachianai/toad/compare/v0.5.30...v0.5.31
[0.5.30]: https://github.com/batrachianai/toad/compare/v0.5.29...v0.5.30
[0.5.29]: https://github.com/batrachianai/toad/compare/v0.5.28...v0.5.29
[0.5.28]: https://github.com/batrachianai/toad/compare/v0.5.27...v0.5.28
[0.5.27]: https://github.com/batrachianai/toad/compare/v0.5.26...v0.5.27
[0.5.26]: https://github.com/batrachianai/toad/compare/v0.5.25...v0.5.26
[0.5.24]: https://github.com/batrachianai/toad/compare/v0.5.23...v0.5.24
[0.5.23]: https://github.com/batrachianai/toad/compare/v0.5.22...v0.5.23
[0.5.22]: https://github.com/batrachianai/toad/compare/v0.5.21...v0.5.22
[0.5.21]: https://github.com/batrachianai/toad/compare/v0.5.20...v0.5.21
[0.5.20]: https://github.com/batrachianai/toad/compare/v0.5.19...v0.5.20
[0.5.19]: https://github.com/batrachianai/toad/compare/v0.5.18...v0.5.19
[0.5.18]: https://github.com/batrachianai/toad/compare/v0.5.17...v0.5.18
[0.5.17]: https://github.com/batrachianai/toad/compare/v0.5.16...v0.5.17
[0.5.16]: https://github.com/batrachianai/toad/compare/v0.5.15...v0.5.16
[0.5.15]: https://github.com/batrachianai/toad/compare/v0.5.14...v0.5.15
[0.5.14]: https://github.com/batrachianai/toad/compare/v0.5.13...v0.5.14
[0.5.13]: https://github.com/batrachianai/toad/compare/v0.5.12...v0.5.13
[0.5.12]: https://github.com/batrachianai/toad/compare/v0.5.11...v0.5.12
[0.5.11]: https://github.com/batrachianai/toad/compare/v0.5.10...v0.5.11
[0.5.10]: https://github.com/batrachianai/toad/compare/v0.5.9...v0.5.10
[0.5.9]: https://github.com/batrachianai/toad/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/batrachianai/toad/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/batrachianai/toad/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/batrachianai/toad/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/batrachianai/toad/compare/v0.5.0...v0.5.5
[0.5.0]: https://github.com/batrachianai/toad/releases/tag/v0.5.0
