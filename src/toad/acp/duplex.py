"""Turn-taking orchestration for two ACP agents."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Awaitable, Callable, Protocol, Sequence


class AgentLike(Protocol):
    """The small portion of an ACP agent required by the relay."""

    last_response: str

    async def send_prompt(self, prompt: str) -> str | None: ...

    def get_info(self) -> object: ...


STOP_TOKEN = "[TAIJI:STOP]"
MAX_RELAY_RESPONSE_CHARS = 12_000


@dataclass(frozen=True)
class RelayResult:
    """The result of one automated two-agent conversation."""

    rounds: int
    stopped: bool
    reason: str


class DuplexConversation:
    """Alternate prompts between two already-started ACP agents.

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
        on_turn: Callable[[int, AgentLike, str], Awaitable[None] | None] | None = None,
    ) -> None:
        if len(agents) != 2:
            raise ValueError("DuplexConversation requires exactly two agents")
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.agents = tuple(agents)
        self.max_rounds = max_rounds
        self.stop_token = stop_token
        self.on_turn_start = on_turn_start
        self.on_turn = on_turn
        self._human_queue: deque[str] = deque()
        self._direct_queue: deque[tuple[int, str]] = deque()
        self.paused = False
        self.last_active_index = 0
        self.next_agent_index: int | None = None

    def pause(self) -> None:
        """Prevent the relay from dispatching another turn."""
        self.paused = True

    def resume(self) -> None:
        """Allow the relay to dispatch turns again."""
        self.paused = False

    def enqueue_human(self, prompt: str) -> None:
        """Queue an untagged human follow-up for the next agent."""
        if prompt.strip():
            self._human_queue.append(prompt)

    def enqueue_direct(self, agent_index: int, prompt: str) -> None:
        """Queue a tagged prompt for one specific agent."""
        if agent_index not in (0, 1):
            raise ValueError("agent_index must be 0 or 1")
        if prompt.strip():
            self._direct_queue.append((agent_index, prompt))

    async def run(self, prompt: str, first_agent: int = 0) -> RelayResult:
        """Run the initial prompt and relay each response to the other agent."""
        if first_agent not in (0, 1):
            raise ValueError("first_agent must be 0 or 1")

        current = (
            first_agent
            if self.next_agent_index is None
            else self.next_agent_index
        )
        relay = prompt
        for round_number in range(1, self.max_rounds + 1):
            if self.paused:
                return RelayResult(round_number - 1, True, "paused")
            direct_turn = False
            if self._direct_queue:
                current, relay = self._direct_queue.popleft()
                direct_turn = True
            agent = self.agents[current]
            self.last_active_index = current
            if self.on_turn_start is not None:
                result = self.on_turn_start(round_number, agent)
                if result is not None:
                    await result
            if round_number == 1 and not direct_turn:
                relay = (
                    "This is a two-agent automated collaboration. The safe word "
                    f"is {self.stop_token}. If the task is complete, reply with "
                    f"{self.stop_token} and do not add further work. A response "
                    "containing the safe word ends the collaboration and will "
                    "not be forwarded.\n\n"
                    f"Human task:\n{relay}"
                )
            stop_reason = await agent.send_prompt(relay)
            raw_response = getattr(agent, "last_response", "") or ""
            response = self._compact_response(raw_response)

            if self.on_turn is not None:
                result = self.on_turn(round_number, agent, response)
                if result is not None:
                    await result

            if self.stop_token in raw_response:
                return RelayResult(round_number, True, "stop_token")
            if stop_reason not in (None, "end_turn"):
                return RelayResult(round_number, True, stop_reason)

            self.next_agent_index = 1 - current

            if direct_turn:
                # A tagged response is intentionally private to its target;
                # never use it as relay context for the other agent.
                current = 1 - current
                relay = "Continue the original task after a direct agent instruction. Inspect the shared workspace and proceed."
                continue

            if self._human_queue:
                # Untagged human follow-ups are ordinary turns for the next
                # agent, just like an automated relay response.
                current = self.next_agent_index
                relay = self._human_queue.popleft()
                continue

            current = 1 - current
            relay = (
                "You are one participant in an automated collaboration.\n\n"
                f"The previous participant ({self._name(agent)}) responded:\n"
                f"{response}\n\n"
                "Continue the original task. Inspect or improve the work as "
                f"needed. If the task is complete, include {self.stop_token}."
            )

        return RelayResult(self.max_rounds, True, "max_rounds")

    @staticmethod
    def _name(agent: AgentLike) -> str:
        info = agent.get_info()
        return str(info)

    @staticmethod
    def _compact_response(response: str) -> str:
        """Keep relay context bounded without forwarding tool/UI history."""
        if len(response) <= MAX_RELAY_RESPONSE_CHARS:
            return response
        head_size = MAX_RELAY_RESPONSE_CHARS // 2
        tail_size = MAX_RELAY_RESPONSE_CHARS - head_size
        return (
            response[:head_size]
            + "\n\n[Taiji omitted the middle of this response to protect context.]\n\n"
            + response[-tail_size:]
        )
