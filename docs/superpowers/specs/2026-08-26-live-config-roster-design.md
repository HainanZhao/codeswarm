# Live Config Roster Design

## Goal

Saving `/config` updates the active conversation membership as well as the
saved roster. Newly selected agents join the current conversation and
unchecked peers leave it without restarting Wingmen.

## User-visible behavior

- The current session owner is always selected and cannot be unchecked. The
  owner remains roster index 0 and can only be ended with `/close`.
- Saving while the conversation is idle starts newly selected agents and
  removes unchecked non-owner agents.
- Existing live agents keep their stable roster indices and relative order.
  Newly added agents append to the live relay in the order shown in `/config`.
- Checkbox reordering is saved for the next workspace but does not reorder the
  current relay.
- If a newly selected agent cannot start, Wingmen keeps every healthy existing
  agent active, reports the failure in the conversation notification ribbon,
  and persists the roster that actually became active.
- Saving during active agent work does not mutate the live roster. Wingmen
  keeps the configuration screen open and asks the user to retry when idle.
- Opening `/config` inside a conversation reflects its active membership,
  including peers added or dropped since launch. Outside a conversation, the
  screen continues to edit only the next-workspace roster.

## Architecture

`ConfigScreen` accepts an optional `Conversation`. When present, it uses the
conversation's active roster as the checkbox membership source, locks the
owner control, and delegates live reconciliation after validating ordinary
settings. Directly constructed configuration screens remain supported and
retain next-workspace-only behavior.

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

- `WingmenApp.run_test` verifies that `/config` reflects the active roster,
  locks the owner, and adds/removes peers immediately on Save.
- Integration coverage verifies that existing live order remains stable while
  the saved next-workspace order follows the checkboxes.
- Failure coverage verifies that a replacement external agent startup failure
  preserves healthy peers and persists only the roster that is actually live.
- Session-level tests continue to cover stable indices, owner protection,
  relay creation, queue discard, and adapter shutdown.

