"""Manual, pinned-agent collaboration for CodeSwarm."""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from codeswarm import jsonrpc
from codeswarm.acp.collaboration import CollaborationContext
from codeswarm.acp.relay import (
    AgentLike,
    MAX_QUEUED_PROMPTS,
    RelayConversation,
    RelayResult,
)


class PinnedConversation(RelayConversation):
    """Send each user turn to one explicitly selected agent.

    This subclasses the existing relay to reuse its stable roster, queue, and
    public-context machinery. It drains queued turns sequentially but keeps
    every dispatch pinned until the caller selects another active roster entry.
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

    def enqueue_human(self, prompt: str, *, agent_index: int | None = None) -> bool:
        """Queue a human message for the pin at submission time."""
        # The pin is the only recipient in this mode, so a caller-supplied
        # target is deliberately ignored.
        del agent_index
        if not prompt.strip() or self.queued_prompt_count >= MAX_QUEUED_PROMPTS:
            return False
        if not self.active[self.pinned_agent_index]:
            return False
        self._steering_queue.append((self.pinned_agent_index, prompt))
        return True

    async def run(
        self,
        prompt: str,
        first_agent: int = 0,
        *,
        resume_queued: bool = False,
    ) -> RelayResult:
        """Dispatch normal and queued prompts sequentially to their targets."""
        del first_agent
        pending_prompt = None if resume_queued else prompt
        rounds = 0

        while (
            self.dispatchable_queued_prompt_count
            or pending_prompt is not None
        ):
            if self.paused:
                return RelayResult(rounds, True, "paused")
            if not self.active[self.pinned_agent_index]:
                return RelayResult(rounds, True, "pinned_agent_unavailable")

            current = self.pinned_agent_index
            direct_turn = False
            queued_turn = False
            steering_turn = False
            created_shared_task = False
            context_event_index: int | None = None
            queued_direct = self._pop_active_queued(self._direct_queue)
            if queued_direct is not None:
                current, relay = queued_direct
                direct_turn = True
                queued_turn = True
            else:
                queued_steering = self._pop_active_queued(self._steering_queue)
                if queued_steering is not None:
                    current, relay = queued_steering
                    queued_turn = True
                    steering_turn = True
                else:
                    assert pending_prompt is not None
                    submitted_prompt = pending_prompt
                    pending_prompt = None
                    if self.context.shared_task is None:
                        self.context.shared_task = submitted_prompt
                        created_shared_task = True
                        relay = submitted_prompt
                    else:
                        relay = f"Human follow-up:\n{submitted_prompt}"
                        context_event_index = self._record_event(
                            "Human", submitted_prompt
                        )

            if not self.active[current]:
                return RelayResult(rounds, True, "pinned_agent_unavailable")
            agent = self.agents[current]
            rounds += 1
            self.last_active_index = current
            if queued_turn and self.on_queued_turn_start is not None:
                result = self.on_queued_turn_start(
                    rounds, agent, relay, direct_turn
                )
                if result is not None:
                    await result
            if steering_turn:
                if self.context.shared_task is None:
                    self.context.shared_task = relay
                    created_shared_task = True
                else:
                    context_event_index = self._record_event("Human", relay)
            if self.on_turn_start is not None:
                result = self.on_turn_start(rounds, agent)
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
            except Exception as error:
                if pending_prompt is not None:
                    self._steering_queue.append(
                        (self.pinned_agent_index, pending_prompt)
                    )
                    pending_prompt = None
                if created_shared_task and not isinstance(
                    error, jsonrpc.TransportClosed
                ):
                    self.context.shared_task = None
                raise

            raw_response = getattr(agent, "last_response", "") or ""
            response_without_stop, _requested_stop = self._strip_trailing_stop_token(
                raw_response
            )
            response = self._compact_response(response_without_stop).strip()
            if direct_turn:
                self.context.seen_event_count[current] = len(
                    self.context.public_events
                )
            else:
                self._record_event(self._name(agent), response) if response else None
                self.context.seen_event_count[current] = len(
                    self.context.public_events
                )

            if self.on_turn is not None:
                result = self.on_turn(rounds, agent, response)
                if result is not None:
                    await result
            if stop_reason not in (None, "end_turn"):
                if pending_prompt is not None:
                    self._steering_queue.append(
                        (self.pinned_agent_index, pending_prompt)
                    )
                    pending_prompt = None
                return RelayResult(rounds, True, stop_reason)

        return RelayResult(rounds, True, "turn_complete")
