# Live Roster Owner Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Execute this plan inline with the repository's TDD workflow.

**Goal:** Let `/config` replace the live session owner on Save and ensure every newly added or promoted agent receives the public conversation context it missed.

**Architecture:** Keep roster slot 0 as the persisted session owner, but make a Save-time transfer transactional: start the requested owner first, replay the shared public journal, then stop the old owner and update slot 0, relay arrays, and persistence metadata. New peers and promoted agents reset their context watermark to zero so their next turn receives all retained public updates.

**Tech Stack:** Python 3.14, Textual, asyncio, unittest.

**Spec:** User-approved design in the conversation: transfer ownership only when the user presses Save; start the replacement first and replay full public context.

## Global Constraints

- Never mutate a live roster while an agent turn is active.
- Preserve session DB identity and relay context; do not discard queued work on a successful transfer.
- On replacement startup failure, leave the old owner and roster unchanged.
- Run `make verify` before completion.

### Task 1: Context handoff regression

**Files:** `tests/test_session.py`, `src/codeswarm/session.py`

- [x] Add a failing test proving a newly added peer gets `seen_event_count == 0` and receives existing public events on its next turn.
- [x] Add a failing test proving owner transfer starts the replacement before stopping the old owner and rewires slot 0/relay state.
- [x] Implement the smallest context-reset and transfer API needed by those tests.
- [x] Run the focused session tests.

### Task 2: Live config behavior

**Files:** `tests/test_config.py`, `src/codeswarm/screens/config.py`

- [x] Add a failing Textual integration test allowing the owner checkbox to be cleared and another agent selected.
- [x] Add a failing save test asserting the transfer API is called only by Save and the screen reports startup failures without dismissing.
- [x] Remove the live-owner checkbox disable, preserving the at-least-one-agent validation.
- [x] Route the selected roster through the session transfer/reconcile implementation.
- [x] Run focused config and conversation tests.

### Task 3: Persistence and release verification

**Files:** `tests/test_conversation_acp.py`, `CHANGELOG.md`

- [x] Add a regression for context visibility after a live roster replacement.
- [x] Run `make verify` and inspect `git diff --check`.
- [x] Add a changelog entry if the implementation changes user-visible behavior.
