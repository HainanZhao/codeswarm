"""Manual, pinned-agent collaboration for CodeSwarm."""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from codeswarm.acp.collaboration import CollaborationContext
from codeswarm.acp.relay import (
    AgentLike,
    MAX_QUEUED_PROMPTS,
    RelayConversation,
    RelayResult,
)


class PinnedConversation(RelayConversation):
    """Send each user turn to one explicitly selected agent.

    This subclasses the existing relay only to reuse its stable roster, queue,
    and public-context machinery. It never calls the relay's multi-round loop:
    each ``run`` dispatches exactly one turn and leaves the selected agent
    pinned until the caller selects another active roster entry.
    """

    def __init__(
        self,
        agents: Sequence[AgentLike],
        *,
        first_agent: int = 0,
        context: CollaborationContext | None = None,
        on_turn_start: Callable[[int, AgentLike], Awaitable[None] | None] | None = None,
        on_queued_turn_start: Callable[[int, AgentLike, str, bool], Awaitable[None] | None]
        | None = None,
        on_queued_turn_discarded: Callable[[str, bool], None] | None = None,
        on_turn: Callable[[int, AgentLike, str], Awaitable[None] | None] | None = None,
    ) -> None:
        super().__init__(
            agents,
            max_rounds=1,
            on_turn_start=on_turn_start,
            on_queued_turn_start=on_queued_turn_start,
            on_queued_turn_discarded=on_queued_turn_discarded,
            on_turn=on_turn,
            context=context,
        )
        if not 0 <= first_agent < len(self.agents):
            raise ValueError("first_agent out of range")
        self.pinned_agent_index = first_agent

    def select_agent(self, index: int) -> None:
        """Persist a user-selected active roster target."""
        if not 0 <= index < len(self.agents) or not self.active[index]:
            raise ValueError("agent index must name an active agent")
        self.pinned_agent_index = index

    def enqueue_human(self, prompt: str) -> bool:
        """Queue a human message for the pin at submission time."""
        if not prompt.strip() or self.queued_prompt_count >= MAX_QUEUED_PROMPTS:
            return False
        if not self.active[self.pinned_agent_index]:
            return False
        self._steering_queue.append((self.pinned_agent_index, prompt))
        return True

    async def run(self, prompt: str, first_agent: int = 0) -> RelayResult:
        """Dispatch one prompt to the currently pinned active agent."""
        del first_agent
        if self.paused:
            return RelayResult(0, True, "paused")
        if not self.active[self.pinned_agent_index]:
            return RelayResult(0, True, "pinned_agent_unavailable")

        current = self.pinned_agent_index
        direct_turn = False
        queued_turn = False
        steering_turn = False
        context_event_index: int | None = None
        if self._direct_queue:
            current, relay = self._direct_queue.popleft()
            direct_turn = True
            queued_turn = True
        elif self._steering_queue:
            current, relay = self._steering_queue.popleft()
            queued_turn = True
            steering_turn = True
        else:
            if self.context.shared_task is None:
                self.context.shared_task = prompt
                relay = prompt
            else:
                relay = f"Human follow-up:\n{prompt}"
                context_event_index = self._record_event("Human", prompt)

        if not self.active[current]:
            return RelayResult(0, True, "pinned_agent_unavailable")
        agent = self.agents[current]
        self.last_active_index = current
        if queued_turn and self.on_queued_turn_start is not None:
            result = self.on_queued_turn_start(1, agent, relay, direct_turn)
            if result is not None:
                await result
        if steering_turn:
            context_event_index = self._record_event("Human", relay)
        if self.on_turn_start is not None:
            result = self.on_turn_start(1, agent)
            if result is not None:
                await result

        task = self.context.shared_task or relay
        turn_prompt = self._build_turn_prompt(
            task,
            relay,
            previous_agent=None,
            unseen_updates=self._unseen_updates(
                current,
                excluding=context_event_index,
            ),
            can_stop=False,
        )
        try:
            stop_reason = await agent.send_prompt(turn_prompt)
        except Exception:
            if self.context.shared_task == prompt and not queued_turn:
                self.context.shared_task = None
            raise

        raw_response = getattr(agent, "last_response", "") or ""
        response_without_stop, _requested_stop = self._strip_trailing_stop_token(
            raw_response
        )
        response = self._compact_response(response_without_stop).strip()
        if direct_turn:
            self.context.seen_event_count[current] = len(self.context.public_events)
        else:
            self._record_event(self._name(agent), response) if response else None
            self.context.seen_event_count[current] = len(self.context.public_events)

        if self.on_turn is not None:
            result = self.on_turn(1, agent, response)
            if result is not None:
                await result
        if stop_reason not in (None, "end_turn"):
            return RelayResult(1, True, stop_reason)
        return RelayResult(1, True, "turn_complete")
