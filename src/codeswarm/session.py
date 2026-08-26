"""Agent and session orchestration independent of the conversation widget."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence, cast

from textual.message_pump import MessagePump

from codeswarm.acp.relay import AgentLike, RelayConversation, RelayResult
from codeswarm.agent import AgentBase
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.db import decode_session_meta

CANCEL_TIMEOUT_SECONDS = 2.0


class AgentFactory(Protocol):
    def __call__(
        self,
        project_root: Path,
        agent: AgentData,
        session_id: str | None,
        session_pk: int | None,
        *,
        persist: bool = True,
    ) -> AgentBase: ...


class SettingsStore(Protocol):
    def set(self, key: str, value: object) -> None: ...


class StoppableAgent(Protocol):
    async def stop(self) -> None: ...


SaveSettings = Callable[[], Awaitable[None]]
RelayTurnStart = Callable[[int, AgentBase], Awaitable[None] | None]
RelayQueuedTurnStart = Callable[[int, AgentBase, str, bool], Awaitable[None] | None]
RelayQueuedTurnDiscarded = Callable[[str, bool], None]
RelayTurn = Callable[[int, AgentBase, str], Awaitable[None] | None]


@dataclass
class RosterEntry:
    """One agent in a session roster.

    Roster indices are stable for the life of the session. Dropping an agent
    tombstones the entry so queued direct prompts cannot be retargeted.
    """

    data: AgentData
    agent: AgentBase | None = None
    active: bool = True


class SessionCoordinator:
    """Own ACP agent lifecycles, roster state, and relay execution."""

    def __init__(
        self,
        project_root: Path,
        owner: AgentData | None = None,
        *,
        session_id: str | None = None,
        session_pk: int | None = None,
        peers: Sequence[AgentData] = (),
        first_agent: int = 0,
        max_rounds: int = 100,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.project_root = project_root.resolve().absolute()
        self.owner_data = owner
        self.session_id = session_id
        self.session_pk = session_pk
        self.first_agent = first_agent
        self.max_rounds = max_rounds
        self._agent_factory = agent_factory or self._default_agent_factory
        self.roster: list[RosterEntry] = (
            [RosterEntry(owner)] if owner is not None else []
        )
        self.roster.extend(RosterEntry(peer) for peer in peers)
        roster_size = len(self.roster) or 1
        if not 0 <= first_agent < roster_size:
            raise ValueError("first_agent out of range")
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.relay: RelayConversation | None = None
        self.selected_agent_index: int | None = None
        self._turn_instructions = ""
        self._gemini_restart_attempted: set[int] = set()

    @staticmethod
    def _default_agent_factory(
        project_root: Path,
        agent: AgentData,
        session_id: str | None,
        session_pk: int | None,
        *,
        persist: bool = True,
    ) -> AgentBase:
        if agent["identity"] == "antigravity.google.com":
            from codeswarm.agy import AgyAgent

            return AgyAgent(
                project_root,
                agent,
                session_id,
                session_pk,
                persist=persist,
            )
        from codeswarm.acp.agent import Agent

        return Agent(
            project_root,
            agent,
            session_id,
            session_pk,
            persist=persist,
        )

    @property
    def owner(self) -> AgentBase | None:
        return self.roster[0].agent if self.roster else None

    @property
    def primary_agent(self) -> AgentBase | None:
        """Return the best available agent for a non-relay turn.

        Normally this is the session owner. If that adapter fails while a
        peer is healthy, use the first surviving roster member rather than
        sending prompts to the dead owner process.
        """
        return next(iter(self.active_agents), None)

    @property
    def relay_active(self) -> bool:
        return self.relay is not None and len(self.relay.active_agents) > 1

    @property
    def active_agents(self) -> list[AgentBase]:
        return [
            entry.agent
            for entry in self.roster
            if entry.active and entry.agent is not None
        ]

    @property
    def roster_subtitle(self) -> str:
        names = [entry.data["name"] for entry in self.roster if entry.active]
        if len(names) <= 3:
            return " ↔ ".join(names)
        return f"{names[0]} +{len(names) - 1}"

    async def start(
        self,
        message_target: MessagePump,
        *,
        on_turn_start: RelayTurnStart | None = None,
        on_queued_turn_start: RelayQueuedTurnStart | None = None,
        on_queued_turn_discarded: RelayQueuedTurnDiscarded | None = None,
        on_turn: RelayTurn | None = None,
    ) -> None:
        """Start the owner and peers, then construct the relay if needed."""
        if self.owner_data is None:
            return

        try:
            owner = self._agent_factory(
                self.project_root,
                self.owner_data,
                self.session_id,
                self.session_pk,
            )
            self.roster[0].agent = owner
            await owner.start(message_target)

            for entry in self.roster[1:]:
                entry.agent = self._agent_factory(
                    self.project_root,
                    entry.data,
                    None,
                    None,
                    persist=False,
                )
                await entry.agent.start(message_target)
        except Exception:
            # Starting a roster is all-or-nothing. An unavailable peer must
            # not leave already-started adapters running in the background.
            await self.stop()
            raise

        self._build_relay(
            on_turn_start=on_turn_start,
            on_queued_turn_start=on_queued_turn_start,
            on_queued_turn_discarded=on_queued_turn_discarded,
            on_turn=on_turn,
        )
        self._introduce_roster()

    def _introduce_roster(self) -> None:
        """Give each agent a concise introduction to its active collaborators."""
        agents = self.active_agents
        for agent in agents:
            name = self.display_name(agent)
            collaborators = [
                self.display_name(candidate)
                for candidate in agents
                if candidate is not agent
            ]
            if collaborators:
                roster = ", ".join(collaborators)
                introduction = (
                    "## CodeSwarm conversation roster\n\n"
                    f"You are {name}. Your collaborators are {roster}. "
                    "CodeSwarm shares agent replies sequentially."
                )
            else:
                introduction = (
                    "## CodeSwarm conversation roster\n\n"
                    f"You are {name}, the only agent in this conversation."
                )
            agent.set_roster_introduction(introduction)

    def _build_relay(
        self,
        *,
        on_turn_start: RelayTurnStart | None = None,
        on_queued_turn_start: RelayQueuedTurnStart | None = None,
        on_queued_turn_discarded: RelayQueuedTurnDiscarded | None = None,
        on_turn: RelayTurn | None = None,
    ) -> None:
        agents = [entry.agent for entry in self.roster]
        if len(agents) <= 1 or any(agent is None for agent in agents):
            self.relay = None
            return
        self.relay = RelayConversation(
            cast(
                list[AgentLike],
                [agent for agent in agents if agent is not None],
            ),
            max_rounds=self.max_rounds,
            on_turn_start=cast(
                Callable[[int, AgentLike], Awaitable[None] | None], on_turn_start
            ),
            on_queued_turn_start=cast(
                Callable[[int, AgentLike, str, bool], Awaitable[None] | None],
                on_queued_turn_start,
            ),
            on_queued_turn_discarded=on_queued_turn_discarded,
            on_turn=cast(
                Callable[[int, AgentLike, str], Awaitable[None] | None], on_turn
            ),
        )
        self.relay.set_turn_instructions(self._turn_instructions)

    def set_turn_instructions(self, instructions: str) -> None:
        """Apply CodeSwarm-owned turn guidance to the current or future relay."""
        self._turn_instructions = instructions.strip()
        if self.relay is not None:
            self.relay.set_turn_instructions(self._turn_instructions)

    async def stop(self) -> None:
        agents = [entry.agent for entry in self.roster if entry.agent is not None]
        if agents:
            # Every adapter must get its shutdown chance. A broken process or
            # third-party adapter must not prevent the remaining CLI agents
            # from being terminated when the workspace closes.
            await asyncio.gather(
                *(agent.stop() for agent in agents), return_exceptions=True
            )

    async def restart_gemini_once(
        self,
        failed_agent: AgentBase | None,
        message_target: MessagePump,
        *,
        idle: bool,
    ) -> AgentBase | None:
        """Replace one unexpectedly exited idle Gemini adapter at most once."""
        if failed_agent is None:
            return None
        if not idle:
            return None
        failed = failed_agent
        failed_process = cast(StoppableAgent, failed_agent)
        for index, entry in enumerate(self.roster):
            if entry.agent is not failed:
                continue
            if (
                not entry.active
                or entry.data["identity"] != "geminicli.com"
                or index in self._gemini_restart_attempted
            ):
                return None
            self._gemini_restart_attempted.add(index)
            session_id = cast(
                str | None,
                getattr(failed, "session_id", None),
            )
            if index == 0 and session_id is None:
                session_id = self.session_id
            try:
                await failed_process.stop()
            except Exception:
                # Never create a second adapter if the failed process could
                # not be torn down; the normal failure path will tombstone it.
                return None
            replacement = self._agent_factory(
                self.project_root,
                entry.data,
                session_id,
                self.session_pk if index == 0 else None,
                persist=index == 0,
            )
            try:
                await replacement.start(message_target)
            except Exception:
                await replacement.stop()
                return None
            entry.agent = replacement
            if self.relay is not None:
                self.relay.agents[index] = cast(AgentLike, replacement)
            self._introduce_roster()
            return replacement
        return None

    async def restart_for_startup_full_access(
        self,
        agent: AgentBase,
        message_target: MessagePump,
        *,
        enabled: bool,
    ) -> AgentBase | None:
        """Replace one adapter when permission bypass is process-scoped."""
        for index, entry in enumerate(self.roster):
            current = entry.agent
            if current is None or current is not agent or not entry.active:
                continue
            if (
                not current.supports_startup_full_access
                or current.startup_full_access == enabled
            ):
                return None

            session_id = cast(str | None, getattr(current, "session_id", None))
            if index == 0 and session_id is None:
                session_id = self.session_id
            try:
                await current.stop()
            except Exception:
                return None

            replacement = self._agent_factory(
                self.project_root,
                entry.data,
                session_id,
                self.session_pk if index == 0 else None,
                persist=index == 0,
            )
            replacement.configure_startup_full_access(enabled)
            try:
                await replacement.start(message_target)
            except Exception:
                await asyncio.gather(replacement.stop(), return_exceptions=True)
                return None

            entry.agent = replacement
            if self.relay is not None:
                self.relay.agents[index] = cast(AgentLike, replacement)
            self._introduce_roster()
            return replacement
        return None

    def mark_failed(self, agent: AgentBase | None) -> int | None:
        """Remove a failed adapter from future turns without renumbering.

        This is the internal counterpart to ``drop``. It intentionally does
        not call ``agent.stop()`` because it commonly runs in response to the
        adapter's own exit task.
        """
        if agent is None:
            return None
        for index, entry in enumerate(self.roster):
            if entry.agent is not agent:
                continue
            if not entry.active:
                return index
            entry.active = False
            if self.relay is not None:
                self.relay.drop_agent(index)
            return index
        return None

    def display_name(self, agent: AgentBase) -> str:
        name = str(agent.get_info())
        if self.relay is None:
            return name
        index = next(
            (
                index
                for index, candidate in enumerate(self.relay.agents)
                if candidate is agent
            ),
            None,
        )
        if index is None:
            return name
        # Distinguish identical roster entries without exposing a message-tag
        # syntax in the prompt.
        matching_names = sum(
            str(candidate.get_info()).casefold() == name.casefold()
            for candidate in self.relay.agents
        )
        return (
            f"{name} ({index + 1})" if matching_names > 1 else name
        )

    def select_agent(self, index: int) -> None:
        """Select the active roster member for the next normal relay turn."""
        if not 0 <= index < len(self.roster):
            raise IndexError("agent index out of range")
        entry = self.roster[index]
        if not entry.active or entry.agent is None:
            raise IndexError("agent is not active")
        self.first_agent = index
        self.selected_agent_index = index

    def agent_at(self, index: int) -> AgentBase:
        if self.relay is None:
            raise IndexError("no relay is active")
        return cast(AgentBase, self.relay.agents[index])

    def index_of_agent(self, agent: AgentBase) -> int | None:
        """Return an agent's stable roster index, if it is active."""
        for index, entry in enumerate(self.roster):
            if entry.active and entry.agent is agent:
                return index
        return None

    async def send_prompt(
        self, prompt: str
    ) -> RelayResult | str | None:
        if self.relay_active:
            assert self.relay is not None
            if self.selected_agent_index is not None:
                self.relay.next_agent_index = self.selected_agent_index
                self.selected_agent_index = None
            return await self.relay.run(prompt, first_agent=self.first_agent)
        if (agent := self.primary_agent) is not None:
            return await agent.send_prompt(prompt)
        return None

    async def send_direct_prompt(self, agent_index: int, prompt: str) -> str | None:
        if not self.relay_active:
            return None
        assert self.relay is not None
        return await self.relay.send_direct_prompt(agent_index, prompt)

    def enqueue_human(self, prompt: str) -> bool:
        if self.relay is not None:
            return self.relay.enqueue_human(prompt)
        return False

    def enqueue_direct(self, agent_index: int, prompt: str) -> bool:
        if self.relay is not None:
            return self.relay.enqueue_direct(agent_index, prompt)
        return False

    def cancel_queued_prompt(
        self, prompt: str, direct: bool, *, occurrence: int = 0
    ) -> bool:
        if self.relay is None:
            return False
        return self.relay.cancel_queued(prompt, direct, occurrence=occurrence)

    def drain_relay_prompts_for_solo_agent(self) -> list[str]:
        """Preserve queued relay work if only one healthy agent remains."""
        if self.relay is None or self.primary_agent is None:
            return []
        return self.relay.drain_for_solo_agent()

    def pause(self) -> None:
        if self.relay is not None:
            self.relay.pause()

    def resume(self) -> None:
        if self.relay is not None:
            self.relay.resume()

    async def cancel_active(self) -> bool:
        """Ask every active adapter to cancel without letting one freeze Ctrl+C."""

        async def cancel_one(agent: AgentBase) -> bool:
            try:
                async with asyncio.timeout(CANCEL_TIMEOUT_SECONDS):
                    return await agent.cancel()
            except Exception:
                return False

        results = await asyncio.gather(
            *(cancel_one(agent) for agent in self.active_agents)
        )
        return any(results)

    async def add(
        self,
        data: AgentData,
        message_target: MessagePump,
        *,
        on_turn_start: RelayTurnStart | None = None,
        on_queued_turn_start: RelayQueuedTurnStart | None = None,
        on_queued_turn_discarded: RelayQueuedTurnDiscarded | None = None,
        on_turn: RelayTurn | None = None,
    ) -> None:
        """Start and append a peer, creating the relay when it becomes needed."""
        agent = self._agent_factory(
            self.project_root,
            data,
            None,
            None,
            persist=False,
        )
        try:
            await agent.start(message_target)
        except Exception:
            # A custom or third-party adapter may allocate resources before
            # reporting startup failure. It is not in the roster yet, so the
            # normal coordinator shutdown cannot reach it.
            await asyncio.gather(agent.stop(), return_exceptions=True)
            raise
        self.roster.append(RosterEntry(data, agent))
        if self.relay is None:
            self._build_relay(
                on_turn_start=on_turn_start,
                on_queued_turn_start=on_queued_turn_start,
                on_queued_turn_discarded=on_queued_turn_discarded,
                on_turn=on_turn,
            )
        else:
            index = self.relay.add_agent(cast(AgentLike, agent))
            if index != len(self.roster) - 1:
                raise RuntimeError("relay and roster indices diverged")
        self._introduce_roster()

    async def drop(self, index: int) -> RosterEntry:
        """Tombstone and stop a peer; roster index zero is protected."""
        if index == 0:
            raise ValueError("the session owner cannot be dropped")
        if not 0 <= index < len(self.roster):
            raise IndexError("roster index out of range")
        entry = self.roster[index]
        if not entry.active:
            raise ValueError("agent is already dropped")
        entry.active = False
        if self.selected_agent_index == index:
            self.selected_agent_index = None
        if self.relay is not None:
            self.relay.drop_agent(index)
        if entry.agent is not None:
            await entry.agent.stop()
        return entry

    async def persist_roster(
        self,
        settings: SettingsStore,
        save_settings: SaveSettings,
        launcher_identities: Sequence[str] | None = None,
    ) -> None:
        """Persist launcher and session roster state without UI dependencies."""
        active_identities = [
            entry.data["identity"] for entry in self.roster if entry.active
        ]
        launcher_roster = (
            active_identities
            if launcher_identities is None
            else [
                identity
                for identity in launcher_identities
                if identity in active_identities
            ]
        )
        settings.set("launcher.roster", "\n".join(launcher_roster))
        await save_settings()
        if len(self.roster) <= 1 or self.owner is None:
            return

        from codeswarm.db import DB

        session_pk = getattr(self.owner, "session_pk", None)
        if session_pk is None:
            return
        db = DB()
        session = await db.session_get(session_pk)
        if session is None:
            return
        meta = decode_session_meta(session["meta_json"])
        meta["roster"] = active_identities
        await db.session_update_meta(session_pk, meta)
