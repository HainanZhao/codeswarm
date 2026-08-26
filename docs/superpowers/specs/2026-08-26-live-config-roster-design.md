# Live Config Roster Design

## Goal

Saving `/config` updates the active conversation membership as well as the
saved roster. Newly selected agents join the current conversation and
unchecked peers leave it without restarting Wingmen.

## User-visible behavior

- `/config` remains available while agents are initializing or after an agent
  connection failure. Opening local configuration does not depend on ACP
  readiness.
- The current session owner is always selected and cannot be unchecked. The
  owner remains roster index 0 and can only be ended with `/close`.
- Saving while the conversation is idle starts newly selected agents and
  removes unchecked non-owner agents.
- Existing live agents keep their stable roster indices and relative order.
  Newly added agents append to the live relay in the order shown in `/config`.
- Checkbox reordering is saved for the next workspace but does not reorder the
  current relay.
- Every selected agent row has its own Up and Down controls. Clicking one moves
  that exact row without first focusing or clicking its checkbox, so reordering
  cannot accidentally change roster membership.
- The first selected row's Up control and last selected row's Down control are
  disabled. Unchecked rows do not offer active reorder controls.
- `Alt+Up` and `Alt+Down` remain available and move the row containing keyboard
  focus, whether focus is on its checkbox or either reorder control. The old
  global Move Up and Move Down buttons are removed.
- If a newly selected agent cannot start, Wingmen keeps every healthy existing
  agent active, reports the failure in the conversation notification ribbon,
  and persists the roster that actually became active.
- Saving during active agent work does not mutate the live roster. Wingmen
  keeps the configuration screen open and asks the user to retry when idle.
- Opening `/config` inside a conversation reflects its active membership,
  including peers added or dropped since launch. Outside a conversation, the
  screen continues to edit only the next-workspace roster.

## Architecture

Prompt submission no longer rejects every input before the conversation can
classify it. Wingmen-owned slash commands are dispatched locally regardless of
agent readiness. Prompts and agent-owned commands retain the readiness guard
and are not sent until their target ACP agent is connected.

`ConfigScreen` accepts an optional `Conversation`. When present, it uses the
conversation's active roster as the checkbox membership source, locks the
owner control, and delegates live reconciliation after validating ordinary
settings. Directly constructed configuration screens remain supported and
retain next-workspace-only behavior.

Each catalog agent is rendered by a focused roster-row component containing
its membership checkbox and compact Up/Down buttons. The row owns its identity,
so button events directly identify the row to move. Reordering operates among
selected rows: moving a selected row crosses the adjacent selected row while
unchecked catalog entries remain outside the numbered relay order. Refreshing
the rows updates numbering and button enabled states together.

`Conversation` owns the UI-facing reconciliation operation. It compares the
requested identities with `SessionCoordinator.roster`, starts missing peers
through the existing `SessionCoordinator.add()` lifecycle, then drops
unchecked peers through `SessionCoordinator.drop()`. Additions happen before
removals so an unavailable new adapter cannot collapse a healthy roster.

After reconciliation, `Conversation` clears removed agents from readiness,
mode, timing, and selection state; refreshes the roster display and shared
mode surface; synchronizes the desired permission policy; updates commands;
and persists the roster. Existing relay callbacks continue to discard queued
direct turns that belonged to a dropped agent.

## Failure and consistency rules

Each requested addition is attempted independently. Failed additions are
reported and excluded from the persisted active roster. Healthy additions may
still join. Removals then apply to the original active peers requested by the
user. The resulting current roster is authoritative for persistence; the
user's displayed ordering remains the saved order for the next launch, with
failed additions omitted.

The owner identity is forced into the saved roster even if external state or
a malformed widget value attempts to omit it. Removed entries remain
tombstones in the current `SessionCoordinator`, preserving the stable-index
contract. Selecting a previously dropped identity creates a new appended
entry rather than reactivating the tombstone.

## Testing

- `WingmenApp.run_test` verifies that submitting `/config` opens the screen
  while `agent_ready` is false, including submission through slash completion.
- `WingmenApp.run_test` verifies that `/config` reflects the active roster,
  locks the owner, and adds/removes peers immediately on Save.
- UI tests click a row's Up/Down control without focusing its checkbox and
  verify that membership is unchanged, selected order changes, boundary
  controls disable correctly, and unchecked rows cannot be reordered.
- Keyboard coverage verifies that `Alt+Up` and `Alt+Down` move the row that owns
  the currently focused checkbox or inline reorder button.
- Integration coverage verifies that existing live order remains stable while
  the saved next-workspace order follows the checkboxes.
- Failure coverage verifies that a replacement external agent startup failure
  preserves healthy peers and persists only the roster that is actually live.
- Session-level tests continue to cover stable indices, owner protection,
  relay creation, queue discard, and adapter shutdown.
