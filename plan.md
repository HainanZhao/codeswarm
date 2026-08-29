# CodeSwarm Rust Terminal Rewrite — Coding Plan

**Goal:** Replace the Python/Textual frontend with a tmux-first Rust terminal
application that stays responsive while streaming and while scrolling a
5,000-word reply. Preserve CodeSwarm's product behavior: ACP support, native
non-ACP adapters, sequential multi-agent relay, session recovery, and the
`codeswarm` package/executable identity.

**Non-goal:** Pixel-for-pixel parity with Textual. The transcript is a fast,
compact terminal surface, not a permanently mounted Markdown/widget tree.

## Operating Rules

- Default to compact inline output that works well in tmux and SSH. Alternate
  screen is optional, never the only UI.
- The scroll hot path may read cached rows and draw the viewport; it may not
  parse Markdown, rewrap historical text, rebuild a transcript tree, inspect
  SQLite, or await an adapter.
- The core owns domain state. Renderers and adapters are replaceable clients of
  that core; ACP is an adapter, not the domain model.
- Every adapter reports capabilities. A missing, reordered, or replaced mode
  catalog must be handled as normal external state.
- Preserve the current documented relay contracts in `AGENTS.md` and prove
  them with translated tests before changing behavior intentionally.
- Rich UI is welcome only if it is lazy and has a measured cost. No animation
  or per-token redraw is allowed on the default path.

## Deliverable Layout

Create a Cargo workspace alongside the existing Python source during the
migration. The final distribution continues to expose `codeswarm`; do not add
a compatibility executable.

```text
Cargo.toml
crates/
  codeswarm-core/       # events, reducer, relay, persistence interfaces
  codeswarm-adapters/   # AgentAdapter trait, ACP and native implementations
  codeswarm-transcript/ # immutable blocks, wrapping cache, viewport index
  codeswarm-tui/        # Ratatui/Crossterm inline + optional alt-screen UI
  codeswarm-cli/        # `codeswarm` command, config and migration wiring
tests/
  rust/                 # behavior, adapter-contract, transcript tests
  tmux/                 # black-box pane benchmarks and regression scripts
```

The Python implementation remains the behavior oracle until the Rust client is
the default. Do not delete Python code as part of the first implementation
slice.

## Implementation Status — 2026-08-29

Completed on branch \`rewrite/rust-ratatui-architecture\`:

- [x] Created the Rust Cargo workspace and the \`codeswarm-core\`,
  \`codeswarm-adapters\`, \`codeswarm-transcript\`, \`codeswarm-tui\`, and
  \`codeswarm-cli\` crates.
- [x] Added deterministic 5,000-word and 100-turn transcript fixtures, a
  benchmark binary, a cached viewport transcript renderer, and a real tmux
  smoke test.
- [x] Added a 5,000-word cached-scroll regression budget (<100ms), bounded
  visible rows, and stream-chunk coalescing into one logical transcript block.
- [x] Added framework-independent normalized events, a replayable JSONL event
  log, a sequential relay scheduler, shared permission-policy resolution, and
  bounded per-agent public-context watermarks.
- [x] Added ACP stdio initialization/session creation/prompt lifecycle and a
  native Agy stream-JSON adapter under one \`AgentAdapter\` contract.
- [x] Added an inline Ratatui terminal preview with optional alternate screen,
  live adapter event rendering, follow-up prompt dispatch, cancellation, and
  durable user-local event logging.
- [x] Added \`AdapterHost\`, which consumes ACP/native adapter events, reduces
  core session state, persists normalized events, and exposes reducer effects
  without coupling the UI to process I/O.
- [x] Added a latest-state frame scheduler that drops stale terminal deltas
  after backpressure and requires a complete repaint before deltas resume.
- [x] Added deterministic trailing-edge resize coalescing so only final pane
  geometry is applied after a resize burst.
- [x] Added \`AdapterHost\` reload, mode forwarding, and failure tombstoning
  while preserving the stable roster slot and event log.
- [x] Normalized ACP permission requests, including stable tool IDs, titles,
  and selectable option labels.
- [x] Added lazy collapsed tool/thought/terminal details with explicit
  keyboard expansion, keeping expensive detail off the scroll path.
- [x] Verified the current branch with \`make verify\`, \`make rust-test\`, and
  \`make rust-tmux\`.

Still required before cutover:

- [ ] Full relay execution wired to live adapters and the public-context
  journal, including selected-first routing, direct/private turns, pause, and
  reviewer-only stop handling.
- [ ] Adapter parity for permission answers, terminal lifecycle, session
  loading/reload, roster-slot recovery, and capability/mode replacement.
- [ ] Production terminal UI parity: roster, queue controls, permission UI,
  lazy tool/diff/terminal detail, launcher/settings, and a complete inline
  interaction model.
- [ ] Persistence migration/import, shared trace-corpus comparison, dogfooding,
  preview release, and staged default cutover.

## Phase 0 — Baseline and Performance Harness

**Purpose:** Make “fast in tmux” falsifiable before a renderer exists.

- [ ] Add a deterministic fixture generator for:
  - one 5,000-word agent reply with prose, lists, and code fences;
  - 100 alternating human/agent turns averaging 300 words;
  - active token streaming while the user edits a prompt;
  - repeated pane resize and a stopped/crashed adapter.
- [ ] Add a Rust benchmark binary that feeds those fixtures to the transcript
  model and records render time, input-to-paint latency, RSS, allocations (in
  CI where available), and bytes emitted to the terminal backend.
- [ ] Add a real-tmux harness that starts a disposable server/socket, drives a
  pane with `send-keys`, captures output, resizes the pane, and always cleans
  it up. Keep deterministic unit benchmarks separate from this black-box test.
- [ ] Establish and enforce these initial budgets on the reference CI machine;
  record machine details with benchmark output rather than comparing machines
  blindly:

  | Scenario | Required result |
  | --- | --- |
  | 5k-word reply, continuous scroll | no event-loop/input stall over 100 ms; p99 render work under 16 ms after cache warm-up |
  | 100 turns, 300 words each | bounded visible-row memory; no history-wide rewrap on scroll |
  | 20 token chunks/second | input remains responsive; redraws are batched to at most 20 Hz |
  | resize storm | only latest geometry is rendered after a short debounce |
  | blocked/slow terminal | bounded pending output; newest complete state wins |

**Exit criterion:** The harness runs locally and in CI, fails on a budget
regression, and produces a baseline for the existing client where practical.

## Phase 1 — Core Event Model and Persistence

**Purpose:** Extract behavior from UI lifecycle and make replay deterministic.

- [ ] Define `AgentCommand`, `AgentEvent`, `AgentCapabilities`, `SessionState`,
  `RelayState`, and `TranscriptEvent` in `codeswarm-core`. Events must cover
  response text, thought text, tools, terminal lifecycle, permission requests,
  mode updates, failures, completion, and adapter replacement.
- [ ] Implement a pure reducer from `(SessionState, AgentEvent)` to next state
  plus explicit effects. UI rendering, process I/O, timers, and SQLite calls
  must not be inside the reducer.
- [ ] Make the transcript append-only and attributable by stable roster slot.
  Keep public relay context separate from local-only tool/thought/UI events,
  matching the existing collaboration journal contract.
- [ ] Implement durable event/session storage with schema versioning and an
  importer for existing CodeSwarm session metadata where feasible. Failed or
  partially imported sessions must remain readable by the Python client.
- [ ] Translate pure relay cases from `tests/test_relay.py`, including N>2
  rotation, queued prompts, direct turns, reviewer-only stop token handling,
  pause/resume, and maximum-round behavior.

**Exit criterion:** Recorded event traces replay into identical state and
translated relay tests cover every documented `AGENTS.md` relay invariant.

## Phase 2 — Adapter Host and Compatibility

**Purpose:** Retain integrations that do not speak ACP.

- [ ] Define an async `AgentAdapter` trait with `start`, `stop`, `send_prompt`,
  `cancel`, `reload`, `set_mode`, `capabilities`, and a normalized event stream.
  Explicitly model unsupported operations instead of faking ACP capability.
- [ ] Port the generic stdio ACP implementation: JSON-RPC framing,
  initialization, permission answers, terminal operations, session loading,
  mode/command catalog replacement, cancellation, and bounded stream handling.
- [ ] Port the native Antigravity/Agy stream-JSON adapter as its own
  implementation of `AgentAdapter`; do not route it through an invented ACP
  bridge.
- [ ] Inventory current agent definitions and classify each as ACP or native.
  Port only working adapters in the first release, while keeping the adapter
  host extensible for future CLI protocols.
- [ ] Translate lifecycle/recovery regressions: tombstone immediately on crash,
  reload into the same roster slot, reuse a session only when supported, rewind
  that agent's shared-context watermark, and distinguish startup failure from
  mid-turn crash.
- [ ] Add adapter-contract tests that intentionally omit, reorder, and replace
  capabilities/modes between events.

**Exit criterion:** One ACP CLI and the native Agy adapter can both execute the
same scripted turn, emit equivalent normalized traces, and participate in the
same relay state machine.

## Phase 3 — Transcript Engine

**Purpose:** Eliminate the long-message scroll failure at its architectural
source.

- [ ] Define immutable transcript blocks: header, text paragraph, list,
  fenced code, tool summary/detail, diff summary/detail, thought summary, and
  system/permission notice. Store original source for copy/export.
- [ ] Parse new/finalized response text into blocks off the input/render path.
  While streaming, retain an inexpensive plain-text tail; parse and replace
  only the finalized portion without changing prior row offsets unnecessarily.
- [ ] Build a width-keyed row/wrap cache and prefix-sum row index. On scroll,
  binary-search visible rows and render viewport plus overscan only.
- [ ] On terminal resize, invalidate width-dependent wrap entries lazily. Do
  not synchronously rewrap the entire history before accepting input.
- [ ] Make long responses, thoughts, tool output, and diffs collapsed by
  default. Expansion is an explicit state change and only materializes that
  detail's rows.
- [ ] Add transcript tests for 5,000-word single-message scrolling, mixed
  Markdown/code, copy fidelity, resize while scrolled away from tail, and
  incremental streaming without row corruption.

**Exit criterion:** The Phase 0 5k-word benchmark passes using a single long
agent reply; no optimization may rely only on having many small messages.

## Phase 4 — Minimal tmux-First Terminal Client

**Purpose:** Ship one usable end-to-end vertical slice before advanced UI.

- [ ] Build the `codeswarm` CLI and inline Ratatui renderer. It must run in
  tmux/SSH without requiring mouse support or an alternate screen.
- [ ] Implement a compact fixed status line: active agent, permission policy,
  working directory, queued count, streaming/paused state, and elapsed time.
- [ ] Implement prompt editing, history, slash-command completion, submit,
  first/second `Ctrl+C` semantics, cancellation, scroll/follow-tail toggle,
  and keyboard help.
- [ ] Render text as plain cached terminal rows by default. Syntax highlighting,
  rich Markdown decoration, and previews are optional/lazy render modes.
- [ ] Render permission requests as one focused keyboard-driven decision. Keep
  their pending state independent of transcript scrolling.
- [ ] Throttle agent stream paints; the adapter may receive token-sized chunks,
  but the renderer consumes a bounded coalesced stream.
- [ ] Verify the complete path in tmux: prompt → ACP/native adapter → stream →
  cancel/permission → persisted session → resume.

**Exit criterion:** This path passes all Phase 0 budgets and is usable without
any advanced panel or alternate-screen implementation.

## Phase 5 — Collaboration and Lazy Detail Views

**Purpose:** Restore CodeSwarm's differentiators without taxing common use.

- [ ] Implement roster launch, selected-first routing, duplicate-name
  disambiguation, queued-message visibility/cancellation, and sequential relay
  turn status.
- [ ] Implement CodeSwarm's shared policy mapping: default **Auto pilot**,
  native per-adapter mode resolution after catalogs arrive, roster-wide sync,
  no user-facing `Mixed` mode.
- [ ] Implement relay pause/resume and direct/private turns. Preserve FIFO
  steering semantics and the reviewer-only safe-word rules.
- [ ] Add lazy, keyboard-selectable tool output, terminal output, diff, and
  thought detail views. They may be rich, but must not affect idle transcript
  rendering or scroll cost when collapsed.
- [ ] Add optional alternate-screen mode for users who want modal pickers or
  larger detail panes. It uses the same event store and transcript engine as
  inline mode.
- [ ] Port store/launcher/settings behavior: restore last valid roster, open
  the store when none resolves, pre-select detected agents but never auto-start
  them, and retain CodeSwarm branding and no-telemetry policy.

**Exit criterion:** A multi-agent session preserves the documented relay and
failure/reload contracts while its collapsed transcript still passes the tmux
benchmark suite.

## Phase 6 — Cutover

- [ ] Run both clients against a shared scripted adapter trace corpus and
  compare normalized state, relay order, persistence results, and user-visible
  terminal decisions—not byte-for-byte rendering.
- [ ] Run `make verify` for the Python reference and the Rust formatter,
  clippy, unit, integration, tmux, and benchmark gates for the rewrite.
- [ ] Dogfood with ACP and native adapters on real tmux and SSH sessions;
  capture only local benchmark diagnostics, never telemetry.
- [ ] Release the Rust frontend as an opt-in preview under the existing
  `codeswarm` identity. Provide a safe rollback path to the Python frontend
  during the preview.
- [ ] Switch the default only after the compatibility and performance gates
  pass for stable releases. Remove the Python/Textual UI only in a later,
  separately reviewed change.

## Completion Checklist

- [ ] `codeswarm` has a tmux-first Rust implementation and retains its package,
  import, executable, branding, and no-telemetry commitments.
- [ ] A 5,000-word single agent reply scrolls within the defined benchmark
  budget while the agent streams and the prompt remains editable.
- [ ] ACP and at least one native non-ACP adapter use the same normalized core
  and can participate in a relay.
- [ ] All documented relay, permission-policy, recovery/reload, launch, and
  custom-adapter contracts have translated regression coverage.
- [ ] Rich details are lazy and cannot regress collapsed transcript scrolling.
- [ ] tmux/SSH, resize, slow-terminal, cancellation, persistence, and recovery
  integration tests pass before default cutover.
