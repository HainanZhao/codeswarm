# Same-agent steering design

## Goal

When a user submits an untagged message while an agent is working, Wingmen
must send that message to the same agent before advancing the relay. This gives
every supported ACP agent predictable steering without concurrent prompts or
adapter-specific protocol extensions.

## Behavior

- The active relay agent owns every untagged message submitted during its turn.
- Messages are bounded by the existing `MAX_QUEUED_PROMPTS` limit and processed
  in FIFO order.
- The active agent finishes its current ACP prompt before receiving queued
  steering. Wingmen never overlaps two `session/prompt` requests for one agent.
- Each steering response replaces the relay context. After the steering queue
  for that turn is empty, the next active agent receives the latest response.
- A trailing `[WINGMEN:STOP]` does not stop the relay while steering for that
  agent remains queued. The stop marker from the superseded response remains
  hidden and is not forwarded.
- A trailing stop marker from the final steered response keeps its existing
  meaning and ends automated collaboration.
- Explicit `#agent` messages remain private direct turns and retain their
  existing priority and routing behavior.
- Paused relays retain queued input. The active-agent association is preserved
  until the relay resumes.
- Solo sessions keep their existing FIFO same-agent prompt queue.

## Relay model

`RelayConversation` will add a bounded FIFO steering queue containing a stable
roster index and prompt. `enqueue_human` will capture `last_active_index` while
a turn is running, rather than representing an anonymous prompt for the next
agent.

After `agent.send_prompt` completes, the relay checks for steering owned by
that agent before evaluating stop markers or advancing the ring. If present,
it dispatches the oldest steering prompt to the same agent as the next relay
round. Once no steering remains for that agent, normal ring advancement uses
the most recent response as context. Every steering dispatch counts as one of
the existing `--max-rounds` safety-limited relay turns.

The queue stores stable roster indices because roster removal tombstones
entries. Steering for an agent dropped before dispatch is discarded, matching
the existing direct-message behavior.

## UI behavior

The submitted user message appears immediately in the transcript. The compact
status line continues showing the same agent while its steering prompts run.
The existing transient confirmation changes from “Queued for the next agent”
to “Queued for <agent name>”. No additional transcript note or loading widget
is added.

## Failure and lifecycle handling

- Queue overflow keeps the existing visible error and rejects the newest
  message.
- Agent failure follows existing roster degradation. Steering for a dropped
  agent is discarded instead of being silently reassigned.
- If the roster collapses to one agent, queued steering for the survivor is
  included when relay work is drained into the solo queue.
- Pause, cancellation, and shutdown do not create concurrent requests.

## Verification

Relay tests will cover:

1. A mid-turn message returns to the current agent, not the next agent.
2. Multiple steering messages remain FIFO and delay ring advancement.
3. The next agent receives the final steered response as context.
4. Pending steering overrides an earlier stop marker.
5. Dropping an agent discards its steering.
6. Queue limits count steering and direct messages together.
7. Existing two-agent and N-agent relay behavior is unchanged when no steering
   is submitted.

Conversation tests will verify that busy-relay input uses the active agent’s
name in its compact queue confirmation.
