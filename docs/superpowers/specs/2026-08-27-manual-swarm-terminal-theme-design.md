# Manual Swarm Mode and Vivid Terminal Conversation Design

## Goal

Add a second multi-agent collaboration mode to CodeSwarm without changing the
existing sequential roster relay, and refresh the conversation presentation so
message bubbles feel more vivid while preserving CodeSwarm's teal input and
agent-header identity.

## Scope and non-goals

This change covers:

- a user-selectable `Swarm` collaboration mode;
- persistent manual routing to one pinned active agent at a time;
- explicit switching of the pinned agent by clicking an active roster entry;
- queue, pause, cancellation, and failure behavior for pinned turns;
- discoverable collaboration-mode controls and status indicators;
- a dark “bright avionics console” visual treatment for user and agent bubbles;
- regression coverage at the relay/session and Textual conversation boundaries.

The existing roster mode remains the default and keeps its current semantics:
automatic sequential handoff, one-shot first-recipient selection, reviewer-only
stop behavior, and existing queue ordering. This change does not alter
permission modes such as `Manual`, `Plan`, or `Auto pilot`.

The following are out of scope:

- concurrent agent execution;
- changing ACP protocol behavior or provider authentication;
- adding a light theme or a user-created theme editor;
- changing the store's roster membership workflow;
- changing the public journal's privacy boundary for private tool/UI state.

The reported `ModuleNotFoundError` is a local installation-state issue, not a
packaging-source defect: the user's uv editable tool pointed at an old checkout
(`/Users/hainan.zhao/projects/taiji`). The repair is to reinstall the editable
tool from the current checkout (`uv tool install --editable . --force`) and
verify `codeswarm --version` and `codeswarm --help`. No runtime fallback that
mutates `PYTHONPATH` belongs in the application.

## User experience

### Selecting a collaboration mode

The prompt footer exposes a collaboration selector separate from the existing
permission-mode selector. It displays `Roster` or `Swarm`, supports the
commands `/collab roster` and `/collab swarm`, and makes the current choice
visible while the conversation is active. The default is `Roster`, preserving
all current launches and saved settings.

Switching to `Swarm` while a relay is working does not interrupt the active
turn. The new mode takes effect after that turn completes; queued work remains
queued. Switching from `Swarm` to `Roster` likewise waits for the current
single-agent turn to finish and then resumes the normal ring from the selected
agent. If the user requests a mode that cannot be applied because the active
roster has become invalid, CodeSwarm keeps the current mode and shows a Flash
error.

### Pinned routing

When Swarm mode starts, the owner/first configured agent is pinned. A normal
user message is sent only to that agent. After the response, the pin remains
unchanged; there is no implicit handoff and no reviewer stop token in this
mode. Clicking another active agent in the roster changes the pin, and the
next normal user message is sent to the newly selected agent. The click also
focuses the prompt as it does today.

The visible roster distinguishes the current worker from the pinned target:

- `●` identifies the agent currently processing a turn;
- `⌖` (or the platform-safe equivalent selected marker) identifies the pinned
  target when it is not working;
- ready and unavailable states retain their existing indicators.

If the user selects an inactive or missing roster index, the selection is
rejected without changing the current pin. If the pinned agent fails, the pin
is not silently moved: queued prompts for that unavailable target are
discarded with a Flash notice, and the user must click another active agent to
resume manual routing. This honors the rule that the agent changes only by
user selection.

### Shared context and queues

Pinned turns use the same bounded public conversation journal and prompt
context rules as roster turns. An agent that the user selects receives public
human and agent updates it has not seen since its prior turn. Tool calls,
thoughts, terminal output, and other UI history remain local.

Every normal prompt submitted in Swarm mode is a public turn for the selected
agent. Agent-specific slash commands continue to use the existing private
direct-command path. A prompt submitted while the pinned agent is working is
queued with the pinned roster index captured at submission time, so later
clicks cannot retarget already-submitted work. Paused prompts remain queued;
the existing queue capacity and cancellation UI apply unchanged, with labels
that identify the pinned target.

### Mode transitions

`SessionCoordinator` owns the collaboration-mode state. It constructs the
existing `RelayConversation` for Roster mode and a separate
`PinnedConversation` for Swarm mode when two or more agents are active. A
single-agent session continues to use the existing solo path regardless of
the selected collaboration mode.

Mode changes preserve the active roster, agent processes, permission policy,
public conversation, and prompt history. The coordinator transfers the
shared journal and the selected/pinned index into the new collaboration
coordinator. A transition never starts a second request or drops a queued
prompt unless the target agent became unavailable.

## Architecture

### Collaboration coordinator interface

Introduce a small internal collaboration protocol (or equivalent typed
interface) covering the lifecycle operations already consumed by the
conversation widget:

- `run(prompt)`;
- direct prompt and queued prompt operations;
- pause/resume/cancel support;
- active-agent lookup and stable roster indexing;
- public journal/context access;
- add/drop agent support.

`RelayConversation` continues to implement this interface with its current
ring behavior. `PinnedConversation` implements the same interface with one
turn per `run()` and a persistent `pinned_agent_index`. Shared journal,
bounded-context, response-compaction, and prompt-building code should be
factored into a focused helper only where that avoids behavior duplication;
the existing relay algorithm should remain structurally recognizable and
covered by its current tests.

`PinnedConversation.run()` resolves the pinned index, validates that the
target is active, records a human follow-up when appropriate, sends exactly
one prompt, records the compacted response in the public journal, and leaves
the pin unchanged. It returns a result with a reason that lets the UI render
the same completion path without implying that another agent was reviewed.

### Session integration

Add a collaboration-mode value with `roster` as its default. Keep the
existing `relay` property/API as a compatibility surface where practical, but
make the coordinator's internal active-collaboration checks mode-neutral so
the conversation widget does not accidentally bypass Swarm queues.

The existing `select_agent()` behavior is preserved for Roster mode. Swarm
mode uses a persistent selection operation that updates the pinned coordinator
and does not clear itself after the next prompt. Stable tombstone indices are
retained for both coordinators.

The first recipient and pinned index must be validated against the current
active roster whenever agents are added, dropped, replaced, or restarted. A
replacement in the same roster slot inherits that slot's selection state. An
invalid external index is ignored or rejected; it must never route a prompt
to a different agent by accident.

### Conversation widget integration

Replace direct assumptions that “multi-agent” means “ring relay” with a
mode-neutral collaboration-active check. Keep the current roster code path
and status wording intact where the mode is `roster`; branch only the routing,
queue ownership, and completion semantics required for `swarm`.

The footer's collaboration selector is separate from the ACP permission mode
selector. The roster click handler calls the existing one-shot selector in
Roster mode and the persistent pin selector in Swarm mode. The footer shows
the selected/pinned agent clearly, and mode transitions refresh it immediately
after the transition is accepted.

## Visual design

Use a restrained, high-contrast “bright avionics console” treatment:

- retain the black background and teal input/agent-header identity;
- replace the current low-opacity gray message surface with solid, readable
  dark cards;
- give each agent-tone card a distinct saturated edge and subtly tinted
  surface (cyan, blue, violet, and warm amber remain readable against black);
- give the user bubble a vivid teal/coral-leaning surface with a clear right
  alignment and enough padding to read as an authored message;
- keep markdown body text near-white with strong code-block contrast;
- keep tool activity visually subordinate through dimmer text and compact
  previews, while retaining clear success/error colors;
- use the pinned marker and active-worker marker as semantic accents rather
  than decorative noise.

Centralize the new colors as CodeSwarm theme variables in `app.py`, then use
those variables in `screens/main.tcss`. Avoid changing the teal primary,
input focus, and agent-message header colors unless needed for contrast.
The visual change should be achievable with widget classes and CSS variables;
no provider-specific rendering or external assets are required.

## Error handling

- Unsupported or invalid collaboration-mode commands produce a Flash error
  and leave the current mode untouched.
- A mode transition that cannot construct or reconcile its coordinator leaves
  the prior coordinator active and reports the failure.
- An invalid selected/pinned index never falls back silently to another agent.
- Dropping or failing the pinned agent cancels/discards only work owned by
  that target, preserves healthy agents and their stable indices, and asks the
  user to select a replacement.
- A mode transition, agent replacement, or roster refresh must not duplicate
  public journal events or leak private direct-command responses.
- Existing cancellation, pause, queue-capacity, and ACP failure handling
  remain authoritative.

## Testing strategy

### Collaboration unit tests

Add `PinnedConversation` tests for:

- default pin and exactly-one-agent dispatch;
- repeated prompts staying with the same agent;
- a user selection changing only subsequent prompts;
- public context arriving when switching to an agent;
- queued prompts retaining their originally selected target;
- pause/resume and cancellation;
- invalid or inactive selection rejection;
- pinned-agent failure without silent rerouting;
- add/drop/replacement preserving stable indices.

Keep `tests/test_relay.py` unchanged except for shared-helper adjustments;
all existing two-agent alternation, reviewer-only stopping, queue, and N-agent
tests must continue to pass.

### Session and Textual integration tests

Add session tests that construct both collaboration modes and assert that
mode transitions preserve the roster, selected state, and queued work. Include
replacement and invalid-index cases.

Add `CodeSwarmApp.run_test` coverage that:

- shows the collaboration selector and current mode;
- routes a Swarm prompt to the pinned agent only;
- keeps the pin after completion;
- changes routing after clicking another agent;
- queues busy input for the pinned target;
- renders the pinned/working roster markers and vivid bubble classes.

Run the repository quality gate with `make verify` before declaring the change
complete.

## Files likely to change

- `src/codeswarm/acp/relay.py` or a focused shared collaboration-context
  module;
- new `src/codeswarm/acp/pinned.py` (or equivalent focused module);
- `src/codeswarm/session.py`;
- `src/codeswarm/widgets/conversation.py`;
- `src/codeswarm/widgets/agent_response.py` and, if needed,
  `src/codeswarm/widgets/user_input.py`;
- `src/codeswarm/app.py`;
- `src/codeswarm/screens/main.tcss`;
- slash-command/help definitions;
- `tests/test_relay.py` only for non-behavioral shared changes;
- new or extended session and Textual integration tests.
