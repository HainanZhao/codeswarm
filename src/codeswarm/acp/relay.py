"""Turn-taking orchestration for an unlimited-size roster of ACP agents."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Awaitable, Callable, Protocol, Sequence


class AgentLike(Protocol):
    """The small portion of an ACP agent required by the relay."""

    last_response: str

    async def send_prompt(self, prompt: str) -> str | None: ...

    def get_info(self) -> object: ...


STOP_TOKEN = "[CODESWARM:STOP]"
DEFAULT_STOP_ACKNOWLEDGMENT = "👍"
MAX_RELAY_RESPONSE_CHARS = 12_000
MAX_RELAY_HISTORY_CHARS = 24_000
MAX_RELAY_JOURNAL_CHARS = 48_000
MAX_RELAY_EVENTS = 200
MAX_QUEUED_PROMPTS = 100


@dataclass(frozen=True)
class RelayResult:
    """The result of one automated relay conversation."""

    rounds: int
    stopped: bool
    reason: str


@dataclass(frozen=True)
class RelayEvent:
    """One public conversation update that other agents may not have seen."""

    speaker: str
    text: str


class RelayConversation:
    """Alternate prompts around a roster of already-started ACP agents.

    The orchestrator deliberately sends turns sequentially. ACP supports
    concurrent processes, but a relay has a causal dependency on the previous
    response, so concurrent prompts would make the conversation nondeterministic.
    """

    def __init__(
        self,
        agents: Sequence[AgentLike],
        *,
        max_rounds: int = 100,
        stop_token: str = STOP_TOKEN,
        on_turn_start: Callable[[int, AgentLike], Awaitable[None] | None] | None = None,
        on_queued_turn_start: Callable[[int, AgentLike, str, bool], Awaitable[None] | None]
        | None = None,
        on_queued_turn_discarded: Callable[[str, bool], None] | None = None,
        on_turn: Callable[[int, AgentLike, str], Awaitable[None] | None] | None = None,
    ) -> None:
        if len(agents) < 2:
            raise ValueError("RelayConversation requires at least two agents")
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.agents: list[AgentLike] = list(agents)
        self.active: list[bool] = [True] * len(self.agents)
        self.max_rounds = max_rounds
        self.stop_token = stop_token
        self.on_turn_start = on_turn_start
        self.on_queued_turn_start = on_queued_turn_start
        self.on_queued_turn_discarded = on_queued_turn_discarded
        self.on_turn = on_turn
        self._steering_queue: deque[tuple[int, str]] = deque()
        self._direct_queue: deque[tuple[int, str]] = deque()
        self.paused = False
        self.last_active_index = 0
        self.next_agent_index: int | None = None
        self._shared_task: str | None = None
        self._public_events: list[RelayEvent] = []
        self._seen_event_count: list[int] = [0] * len(self.agents)
        self._history_truncated: list[bool] = [False] * len(self.agents)
        self.turn_instructions = ""

    def set_turn_instructions(self, instructions: str) -> None:
        """Set CodeSwarm-owned guidance applied to every future relay turn."""
        self.turn_instructions = instructions.strip()

    @property
    def active_indices(self) -> list[int]:
        """Indices of agents still participating in the relay."""
        return [index for index, active in enumerate(self.active) if active]

    @property
    def active_agents(self) -> list[AgentLike]:
        """Agents still participating in the relay, in roster order."""
        return [self.agents[index] for index in self.active_indices]

    def add_agent(self, agent: AgentLike) -> int:
        """Append a new agent to the roster and return its index.

        Safe to call mid-run: the rotation is computed from ``len(self.agents)``
        at advance time, so a newly appended agent joins on the next full lap.
        """
        self.agents.append(agent)
        self.active.append(True)
        self._seen_event_count.append(0)
        self._history_truncated.append(False)
        return len(self.agents) - 1

    async def send_direct_prompt(self, agent_index: int, prompt: str) -> str | None:
        """Send a private prompt while preserving the target's public context."""
        if not 0 <= agent_index < len(self.agents) or not self.active[agent_index]:
            raise ValueError("agent_index must name an active agent")
        agent = self.agents[agent_index]
        task = self._shared_task or prompt
        turn_prompt = self._build_turn_prompt(
            task,
            prompt,
            previous_agent=None,
            unseen_updates=self._unseen_updates(agent_index, excluding=None),
            can_stop=False,
        )
        result = await agent.send_prompt(turn_prompt)
        self._seen_event_count[agent_index] = len(self._public_events)
        return result

    def drop_agent(self, index: int) -> None:
        """Remove an agent from the rotation without renumbering the roster.

        This is a tombstone, not a splice: surviving agents keep their index,
        so any externally held index stays valid. Direct prompts already
        queued for the dropped agent are discarded.
        """
        if not 0 <= index < len(self.agents):
            raise ValueError("index out of range")
        self.active[index] = False
        # Queued work must not be dispatched to an agent the caller stopped.
        if self.on_queued_turn_discarded is not None:
            for target, prompt in self._direct_queue:
                if target == index:
                    self.on_queued_turn_discarded(prompt, True)
            for target, prompt in self._steering_queue:
                if target == index:
                    self.on_queued_turn_discarded(prompt, False)
        self._direct_queue = deque(
            (target, prompt)
            for target, prompt in self._direct_queue
            if target != index
        )
        self._steering_queue = deque(
            (target, prompt)
            for target, prompt in self._steering_queue
            if target != index
        )

    def pause(self) -> None:
        """Prevent the relay from dispatching another turn."""
        self.paused = True

    def resume(self) -> None:
        """Allow the relay to dispatch turns again."""
        self.paused = False

    def enqueue_human(self, prompt: str) -> bool:
        """Queue a follow-up for the agent that owns the active turn."""
        if not prompt.strip() or self.queued_prompt_count >= MAX_QUEUED_PROMPTS:
            return False
        self._steering_queue.append((self.last_active_index, prompt))
        return True

    @property
    def queued_prompt_count(self) -> int:
        """Number of user messages waiting to be delivered."""
        return len(self._steering_queue) + len(self._direct_queue)

    def enqueue_direct(self, agent_index: int, prompt: str) -> bool:
        """Queue a tagged prompt, returning whether it was accepted."""
        if not 0 <= agent_index < len(self.agents) or not self.active[agent_index]:
            raise ValueError("agent_index must name an active agent")
        if not prompt.strip() or self.queued_prompt_count >= MAX_QUEUED_PROMPTS:
            return False
        self._direct_queue.append((agent_index, prompt))
        return True

    def drain_for_solo_agent(self) -> list[str]:
        """Return queued work that can survive a relay collapsing to one agent.

        Direct instructions retain their existing priority. Instructions for a
        removed agent have already been discarded by ``drop_agent``; direct
        instructions for the sole survivor and ordinary human follow-ups can
        continue as sequential solo prompts.
        """
        active_indices = self.active_indices
        if len(active_indices) != 1:
            return []
        sole_agent = active_indices[0]
        direct_prompts = [
            prompt for target, prompt in self._direct_queue if target == sole_agent
        ]
        steering_prompts = [
            prompt
            for target, prompt in self._steering_queue
            if target == sole_agent
        ]
        self._direct_queue.clear()
        self._steering_queue.clear()
        return direct_prompts + steering_prompts

    def _advance(self, index: int) -> int:
        """Index of the next active agent after ``index`` (wraps around)."""
        total = len(self.agents)
        for offset in range(1, total + 1):
            candidate = (index + offset) % total
            if self.active[candidate]:
                return candidate
        return index

    async def run(self, prompt: str, first_agent: int = 0) -> RelayResult:
        """Run the initial prompt and relay each response around the roster."""
        if not 0 <= first_agent < len(self.agents):
            raise ValueError("first_agent out of range")

        current = (
            first_agent
            if self.next_agent_index is None
            else self.next_agent_index
        )
        if not self.active[current]:
            current = self._advance(current)
        new_shared_task = self._shared_task is None
        if new_shared_task:
            self._shared_task = prompt
            relay = prompt
            context_event_index: int | None = None
        else:
            relay = f"Human follow-up:\n{prompt}"
            context_event_index = self._record_event("Human", prompt)
        task = self._shared_task
        assert task is not None
        context_agent: AgentLike | None = None
        for round_number in range(1, self.max_rounds + 1):
            if self.paused:
                return RelayResult(round_number - 1, True, "paused")
            if len(self.active_indices) < 2:
                # Agents were dropped mid-run. Without this the rotation
                # collapses onto the survivor and it relays its own response
                # back to itself until max_rounds.
                return RelayResult(round_number - 1, True, "roster_collapsed")
            direct_turn = False
            queued_turn = False
            steering_turn = False
            if self._direct_queue:
                current, relay = self._direct_queue.popleft()
                direct_turn = True
                queued_turn = True
                context_event_index = None
            elif self._steering_queue:
                current, relay = self._steering_queue.popleft()
                queued_turn = True
                steering_turn = True
            agent = self.agents[current]
            can_stop = (
                not direct_turn
                and context_agent is not None
                and context_agent is not agent
            )
            self.last_active_index = current
            if queued_turn and self.on_queued_turn_start is not None:
                result = self.on_queued_turn_start(
                    round_number, agent, relay, direct_turn
                )
                if result is not None:
                    await result
            if steering_turn:
                context_event_index = self._record_event("Human", relay)
            if self.on_turn_start is not None:
                result = self.on_turn_start(round_number, agent)
                if result is not None:
                    await result
            turn_prompt = self._build_turn_prompt(
                task,
                relay,
                previous_agent=None if direct_turn else context_agent,
                unseen_updates=self._unseen_updates(
                    current,
                    excluding=context_event_index,
                ),
                can_stop=can_stop,
            )
            try:
                stop_reason = await agent.send_prompt(turn_prompt)
            except Exception:
                if new_shared_task and round_number == 1:
                    self._shared_task = None
                raise
            raw_response = getattr(agent, "last_response", "") or ""
            response_without_stop, requested_stop = self._strip_trailing_stop_token(
                raw_response
            )
            response = self._compact_response(response_without_stop).strip()
            accepted_stop = requested_stop and can_stop
            if accepted_stop and not response:
                response = DEFAULT_STOP_ACKNOWLEDGMENT

            if direct_turn:
                # The target has now seen every public update included above,
                # but its private instruction and answer never enter the
                # journal for other agents.
                self._seen_event_count[current] = len(self._public_events)
                response_event_index = None
            else:
                response_event_index = (
                    self._record_event(self._name(agent), response)
                    if response
                    else None
                )
                # An agent already knows the answer it just produced. Mark the
                # journal through that answer so it receives only later diffs
                # on its next turn.
                self._seen_event_count[current] = len(self._public_events)

            if self.on_turn is not None:
                result = self.on_turn(round_number, agent, response)
                if result is not None:
                    await result

            # A trailing token is a control marker on an otherwise useful
            # answer. It is hidden from the UI and never enters relay context.
            self.next_agent_index = self._advance(current)
            if accepted_stop:
                # The marker suppresses another *automated* hand-off. A human
                # message submitted while this agent was working is newer,
                # explicit work and must never be stranded behind that marker.
                if self._direct_queue:
                    current = self.next_agent_index
                    continue
                if self._steering_queue:
                    context_agent = agent
                    continue
                return RelayResult(round_number, True, "stop_token")
            if stop_reason not in (None, "end_turn"):
                return RelayResult(round_number, True, stop_reason)

            context_agent = agent

            if direct_turn:
                # A tagged response is intentionally private to its target;
                # never use it as relay context for the other agents.
                current = self.next_agent_index
                relay = (
                    "A direct instruction was handled privately. Inspect the "
                    "shared workspace and continue the task."
                )
                context_event_index = None
                continue

            if self._steering_queue:
                # A human correction belongs to the agent that was active
                # when it was submitted. The queue entry selects that stable
                # roster index at the start of the next loop.
                continue

            current = self.next_agent_index
            relay = response
            context_event_index = response_event_index

        return RelayResult(self.max_rounds, True, "max_rounds")

    @staticmethod
    def _name(agent: AgentLike) -> str:
        info = agent.get_info()
        return str(info)

    def _build_turn_prompt(
        self,
        task: str,
        context: str,
        *,
        previous_agent: AgentLike | None,
        unseen_updates: str = "",
        can_stop: bool = False,
    ) -> str:
        """Give every agent the task before its turn-specific context."""
        previous = (
            "This is the first turn."
            if previous_agent is None
            else f"Previous participant: {self._name(previous_agent)}."
        )
        work_instruction = (
            self.turn_instructions
            or "Inspect the shared workspace and make useful progress."
        )
        updates = (
            "Conversation updates since your previous turn:\n"
            f"{unseen_updates}\n\n"
            if unseen_updates
            else ""
        )
        if can_stop:
            stop_instruction = (
                "You are reviewing another participant's answer. If it needs no "
                "meaningful correction or addition, reply with an optional "
                "acknowledgment emoji followed by "
                f"{self.stop_token} as the final line. If you provide no emoji, "
                f"CodeSwarm displays {DEFAULT_STOP_ACKNOWLEDGMENT}. If you add "
                "substantive content, do not use the token; let the next "
                "participant review your contribution."
            )
        else:
            stop_instruction = (
                f"Do not use {self.stop_token} on this turn. Your response must "
                "be offered to another participant for review, even when you are "
                "highly confident it is correct."
            )
        return (
            f"You are one participant in a {len(self.active_agents)}-agent "
            "automated collaboration.\n\n"
            f"Shared task:\n{task}\n\n"
            f"{updates}"
            f"Turn context:\n{context}\n\n"
            f"{previous}\n"
            f"{work_instruction}\n\n"
            f"{stop_instruction}\n"
            "The token is internal: CodeSwarm hides it from the conversation."
        )

    def _record_event(self, speaker: str, text: str) -> int:
        """Append one bounded public event and return its journal index."""
        self._prune_public_events()
        self._public_events.append(
            RelayEvent(speaker, self._compact_response(text).strip())
        )
        return len(self._public_events) - 1

    def _prune_public_events(self) -> None:
        """Discard consumed history and hard-bound permanently unseen events."""
        active = self.active_indices
        consumed = min((self._seen_event_count[index] for index in active), default=0)
        if consumed:
            del self._public_events[:consumed]
            self._seen_event_count = [max(0, count - consumed) for count in self._seen_event_count]

        retained_chars = sum(len(event.speaker) + len(event.text) for event in self._public_events)
        while self._public_events and (
            len(self._public_events) >= MAX_RELAY_EVENTS
            or retained_chars >= MAX_RELAY_JOURNAL_CHARS
        ):
            removed = self._public_events.pop(0)
            retained_chars -= len(removed.speaker) + len(removed.text)
            for index, count in enumerate(self._seen_event_count):
                if count:
                    self._seen_event_count[index] = count - 1
                elif self.active[index]:
                    self._history_truncated[index] = True

    def _unseen_updates(self, agent_index: int, *, excluding: int | None) -> str:
        """Render public events not included in this agent's prior prompts."""
        start = self._seen_event_count[agent_index]
        parts = [
            f"{event.speaker}:\n{event.text}"
            for index, event in enumerate(self._public_events[start:], start)
            if index != excluding and event.text
        ]
        rendered = "\n\n".join(parts)
        if self._history_truncated[agent_index]:
            marker = "[CodeSwarm omitted older unseen updates to protect context.]"
            rendered = f"{marker}\n\n{rendered}" if rendered else marker
            self._history_truncated[agent_index] = False
        if len(rendered) <= MAX_RELAY_HISTORY_CHARS:
            return rendered

        marker = "[CodeSwarm omitted older unseen updates to protect context.]"
        remaining = MAX_RELAY_HISTORY_CHARS - len(marker) - 2
        newest: list[str] = []
        used = 0
        for part in reversed(parts):
            added = len(part) + (2 if newest else 0)
            if used + added > remaining:
                break
            newest.append(part)
            used += added
        newest.reverse()
        return f"{marker}\n\n" + "\n\n".join(newest)

    def _strip_trailing_stop_token(self, response: str) -> tuple[str, bool]:
        """Remove a final relay stop marker and report whether it was present."""
        stripped = response.rstrip()
        if not stripped.endswith(self.stop_token):
            return response, False
        return stripped[: -len(self.stop_token)].rstrip(), True

    @staticmethod
    def _compact_response(response: str) -> str:
        """Keep relay context bounded without forwarding tool/UI history."""
        if len(response) <= MAX_RELAY_RESPONSE_CHARS:
            return response
        head_size = MAX_RELAY_RESPONSE_CHARS // 2
        tail_size = MAX_RELAY_RESPONSE_CHARS - head_size
        return (
            response[:head_size]
            + "\n\n[CodeSwarm omitted the middle of this response to protect context.]\n\n"
            + response[-tail_size:]
        )
