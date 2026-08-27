"""Shared public context for CodeSwarm collaboration coordinators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class AgentContextLike(Protocol):
    def get_info(self) -> object: ...


STOP_TOKEN = "[CODESWARM:STOP]"
DEFAULT_STOP_ACKNOWLEDGMENT = "👍"
MAX_RELAY_RESPONSE_CHARS = 12_000
MAX_RELAY_HISTORY_CHARS = 24_000
MAX_RELAY_JOURNAL_CHARS = 48_000
MAX_RELAY_EVENTS = 200


@dataclass(frozen=True)
class RelayEvent:
    """One public conversation update that other agents may not have seen."""

    speaker: str
    text: str


@dataclass
class CollaborationContext:
    """Bounded public journal and prompt state shared across modes."""

    agent_count: int
    shared_task: str | None = None
    public_events: list[RelayEvent] | None = None
    seen_event_count: list[int] | None = None
    history_truncated: list[bool] | None = None
    turn_instructions: str = ""

    def __post_init__(self) -> None:
        self.public_events = list(self.public_events or [])
        self.seen_event_count = list(self.seen_event_count or [0] * self.agent_count)
        self.history_truncated = list(
            self.history_truncated or [False] * self.agent_count
        )
        self.ensure_agent_count(self.agent_count)

    def ensure_agent_count(self, count: int) -> None:
        while len(self.seen_event_count) < count:
            self.seen_event_count.append(0)
            self.history_truncated.append(False)
        self.agent_count = max(self.agent_count, count)

    def set_turn_instructions(self, instructions: str) -> None:
        self.turn_instructions = instructions.strip()

    def add_agent(self) -> None:
        self.ensure_agent_count(self.agent_count + 1)

    @staticmethod
    def compact_response(response: str) -> str:
        if len(response) <= MAX_RELAY_RESPONSE_CHARS:
            return response
        head_size = MAX_RELAY_RESPONSE_CHARS // 2
        tail_size = MAX_RELAY_RESPONSE_CHARS - head_size
        return (
            response[:head_size]
            + "\n\n[CodeSwarm omitted the middle of this response to protect context.]\n\n"
            + response[-tail_size:]
        )

    def record_event(self, speaker: str, text: str, active: Sequence[bool]) -> int:
        """Append one bounded public event and return its journal index."""
        self._prune_public_events(active)
        self.public_events.append(
            RelayEvent(speaker, self.compact_response(text).strip())
        )
        return len(self.public_events) - 1

    def _prune_public_events(self, active: Sequence[bool]) -> None:
        active_indices = [index for index, value in enumerate(active) if value]
        consumed = min(
            (self.seen_event_count[index] for index in active_indices),
            default=0,
        )
        if consumed:
            del self.public_events[:consumed]
            self.seen_event_count = [
                max(0, count - consumed) for count in self.seen_event_count
            ]

        retained_chars = sum(
            len(event.speaker) + len(event.text) for event in self.public_events
        )
        while self.public_events and (
            len(self.public_events) >= MAX_RELAY_EVENTS
            or retained_chars >= MAX_RELAY_JOURNAL_CHARS
        ):
            removed = self.public_events.pop(0)
            retained_chars -= len(removed.speaker) + len(removed.text)
            for index, count in enumerate(self.seen_event_count):
                if count:
                    self.seen_event_count[index] = count - 1
                elif index < len(active) and active[index]:
                    self.history_truncated[index] = True

    def unseen_updates(self, agent_index: int, *, excluding: int | None) -> str:
        """Render public events not included in this agent's prior prompts."""
        start = self.seen_event_count[agent_index]
        parts = [
            f"{event.speaker}:\n{event.text}"
            for index, event in enumerate(self.public_events[start:], start)
            if index != excluding and event.text
        ]
        rendered = "\n\n".join(parts)
        if self.history_truncated[agent_index]:
            marker = "[CodeSwarm omitted older unseen updates to protect context.]"
            rendered = f"{marker}\n\n{rendered}" if rendered else marker
            self.history_truncated[agent_index] = False
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

    @staticmethod
    def name(agent: AgentContextLike) -> str:
        return str(agent.get_info())

    def build_turn_prompt(
        self,
        task: str,
        context: str,
        *,
        active_agents: Sequence[AgentContextLike],
        previous_agent: AgentContextLike | None,
        unseen_updates: str = "",
        can_stop: bool = False,
        stop_token: str = STOP_TOKEN,
    ) -> str:
        previous = (
            "This is the first turn."
            if previous_agent is None
            else f"Previous participant: {self.name(previous_agent)}."
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
                f"{stop_token} as the final line. If you provide no emoji, "
                f"CodeSwarm displays {DEFAULT_STOP_ACKNOWLEDGMENT}. If you add "
                "substantive content, do not use the token; let the next "
                "participant review your contribution."
            )
        else:
            stop_instruction = (
                f"Do not use {stop_token} on this turn. Your response must be "
                "offered to another participant for review, even when you are "
                "highly confident it is correct."
            )
        return (
            f"You are one participant in a {len(active_agents)}-agent "
            "automated collaboration.\n\n"
            f"Shared task:\n{task}\n\n"
            f"{updates}"
            f"Turn context:\n{context}\n\n"
            f"{previous}\n"
            f"{work_instruction}\n\n"
            f"{stop_instruction}\n"
            "The token is internal: CodeSwarm hides it from the conversation."
        )
