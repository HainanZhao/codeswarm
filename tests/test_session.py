import asyncio
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from wingmen.agent import AgentBase
from wingmen.agent_schema import Agent as AgentData
from wingmen.db import decode_session_meta
from wingmen.session import SessionCoordinator


def agent_data(identity: str, name: str, short_name: str) -> AgentData:
    return cast(
        AgentData,
        {
            "identity": identity,
            "name": name,
            "short_name": short_name,
        },
    )


class FakeAgent(AgentBase):
    def __init__(self, project_root: Path, name: str) -> None:
        super().__init__(project_root)
        self.name = name
        self.started_with: list[Any] = []
        self.prompts: list[str] = []
        self.stopped = False
        self.last_response = ""

    async def start(self, message_target: Any) -> None:
        self.started_with.append(message_target)

    async def send_prompt(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.last_response = f"{self.name} response"
        return "end_turn"

    def get_info(self) -> str:
        return self.name

    async def stop(self) -> None:
        self.stopped = True


class SessionCoordinatorTests(unittest.TestCase):
    def test_idle_gemini_failure_restarts_once_in_the_same_roster_slot(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            gemini = agent_data("geminicli.com", "Gemini CLI", "gemini")
            calls: list[tuple[str, str | None, int | None, bool]] = []
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                calls.append((data["short_name"], session_id, session_pk, persist))
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            target = object()
            coordinator = SessionCoordinator(
                Path("."), owner, peers=(gemini,), agent_factory=factory
            )
            await coordinator.start(target)
            failed = created[1]
            failed.session_id = "gemini-session"  # type: ignore[attr-defined]

            replacement = await coordinator.restart_gemini_once(
                failed, target, idle=True
            )

            self.assertIs(replacement, created[2])
            self.assertTrue(failed.stopped)
            self.assertIs(coordinator.roster[1].agent, replacement)
            self.assertIs(coordinator.agent_at(1), replacement)
            self.assertTrue(coordinator.roster[1].active)
            self.assertEqual(
                calls[-1], ("gemini", "gemini-session", None, False)
            )
            self.assertEqual(created[2].started_with, [target])
            self.assertIsNone(
                await coordinator.restart_gemini_once(
                    created[2], target, idle=True
                )
            )

        asyncio.run(scenario())

    def test_busy_gemini_failure_is_not_automatically_restarted(self) -> None:
        async def scenario() -> None:
            gemini = agent_data("geminicli.com", "Gemini CLI", "gemini")
            coordinator = SessionCoordinator(Path("."), gemini)
            failed = FakeAgent(Path("."), "Gemini CLI")
            coordinator.roster[0].agent = failed

            replacement = await coordinator.restart_gemini_once(
                failed, object(), idle=False
            )

            self.assertIsNone(replacement)
            self.assertIs(coordinator.roster[0].agent, failed)

        asyncio.run(scenario())

    def test_cancel_is_concurrent_and_a_hung_adapter_is_bounded(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            coordinator = SessionCoordinator(Path("."), owner, peers=(peer,))
            never_returns = asyncio.Event()

            class HangingAgent(FakeAgent):
                async def cancel(self) -> bool:
                    await never_returns.wait()
                    return True

            class HealthyAgent(FakeAgent):
                def __init__(self, project_root: Path, name: str) -> None:
                    super().__init__(project_root, name)
                    self.cancelled = False

                async def cancel(self) -> bool:
                    self.cancelled = True
                    return True

            hanging = HangingAgent(Path("."), "Claude")
            healthy = HealthyAgent(Path("."), "Codex")
            coordinator.roster[0].agent = hanging
            coordinator.roster[1].agent = healthy

            with patch("wingmen.session.CANCEL_TIMEOUT_SECONDS", 0.01):
                cancelled = await coordinator.cancel_active()

            self.assertTrue(cancelled)
            self.assertTrue(healthy.cancelled)

        asyncio.run(scenario())

    def test_corrupt_session_metadata_decodes_to_an_empty_mapping(self) -> None:
        self.assertEqual(decode_session_meta("not-json"), {})
        self.assertEqual(decode_session_meta("[]"), {})
        self.assertEqual(decode_session_meta('{"cwd": "/project"}'), {"cwd": "/project"})

    def test_stop_attempts_every_agent_when_one_adapter_fails(self) -> None:
        async def scenario() -> None:
            class FailingAgent(FakeAgent):
                async def stop(self) -> None:
                    raise RuntimeError("adapter teardown failed")

            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            coordinator = SessionCoordinator(Path("."), owner, peers=(peer,))
            failing = FailingAgent(Path("."), "Claude")
            healthy = FakeAgent(Path("."), "Codex")
            coordinator.roster[0].agent = failing
            coordinator.roster[1].agent = healthy

            await coordinator.stop()

            self.assertTrue(healthy.stopped)

        asyncio.run(scenario())

    def test_persist_roster_records_active_agents_in_relay_order(self) -> None:
        async def scenario() -> None:
            claude = agent_data("claude.ai", "Claude", "claude")
            codex = agent_data("openai.com", "Codex", "codex")
            gemini = agent_data("google.com", "Gemini", "gemini")
            coordinator = SessionCoordinator(Path("."), claude, peers=(codex, gemini))
            coordinator.roster[1].active = False
            stored: dict[str, object] = {}
            saved = 0

            class Settings:
                def set(self, key: str, value: object) -> None:
                    stored[key] = value

            async def save_settings() -> None:
                nonlocal saved
                saved += 1

            await coordinator.persist_roster(Settings(), save_settings)

            self.assertEqual(
                stored["launcher.roster"], "claude.ai\ngoogle.com"
            )
            self.assertEqual(saved, 1)

        asyncio.run(scenario())

    def test_start_keeps_owner_session_and_disables_peer_persistence(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            calls: list[tuple[str, str | None, int | None, bool]] = []
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                calls.append((data["short_name"], session_id, session_pk, persist))
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            target = object()
            coordinator = SessionCoordinator(
                Path("."),
                owner,
                session_id="session-1",
                session_pk=42,
                peers=(peer,),
                agent_factory=factory,
            )
            await coordinator.start(target)

            self.assertEqual(
                calls,
                [
                    ("claude", "session-1", 42, True),
                    ("codex", None, None, False),
                ],
            )
            self.assertTrue(all(agent.started_with == [target] for agent in created))
            self.assertTrue(coordinator.relay_active)

            await coordinator.stop()
            self.assertTrue(all(agent.stopped for agent in created))

        asyncio.run(scenario())

    def test_start_stops_already_started_agents_when_a_peer_fails(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            created: list[FakeAgent] = []

            class FailingStartAgent(FakeAgent):
                async def start(self, message_target: Any) -> None:
                    raise RuntimeError("adapter is unavailable")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                if data["short_name"] == "codex":
                    return FailingStartAgent(project_root, data["name"])
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await coordinator.start(object())

            self.assertTrue(created[0].stopped)
            self.assertIsNone(coordinator.relay)

        asyncio.run(scenario())

    def test_add_stops_a_partially_started_agent_on_failure(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            created: list[FakeAgent] = []

            class FailingStartAgent(FakeAgent):
                async def start(self, message_target: Any) -> None:
                    raise RuntimeError("adapter is unavailable")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FailingStartAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, agent_factory=factory
            )
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await coordinator.add(peer, object())

            self.assertTrue(created[0].stopped)
            self.assertEqual(len(coordinator.roster), 1)
            self.assertIsNone(coordinator.relay)

        asyncio.run(scenario())

    def test_tag_parsing_and_relay_execution_stay_out_of_the_ui(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                return FakeAgent(project_root, data["name"])

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())

            self.assertEqual(
                coordinator.parse_agent_tag("@codex-2: inspect this"),
                (1, "inspect this"),
            )
            self.assertIsNone(coordinator.parse_agent_tag("@src/main.py"))

            await coordinator.send_direct_prompt(1, "inspect this")
            self.assertIn("Turn context:\ninspect this", coordinator.agent_at(1).prompts[0])

        asyncio.run(scenario())

    def test_display_name_only_uses_an_index_for_duplicate_names(self) -> None:
        async def scenario() -> None:
            claude = agent_data("claude.ai", "Claude", "claude")
            gemini = agent_data("google.com", "Gemini", "gemini")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                return FakeAgent(project_root, data["name"])

            coordinator = SessionCoordinator(
                Path("."), claude, peers=(gemini,), agent_factory=factory
            )
            await coordinator.start(object())
            self.assertEqual(coordinator.display_name(coordinator.agent_at(0)), "Claude")
            self.assertEqual(coordinator.display_name(coordinator.agent_at(1)), "Gemini")

        asyncio.run(scenario())

    def test_duplicate_display_names_include_their_copyable_tags(self) -> None:
        async def scenario() -> None:
            first = agent_data("claude.one", "Claude", "claude")
            second = agent_data("claude.two", "Claude", "claude")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                return FakeAgent(project_root, data["name"])

            coordinator = SessionCoordinator(
                Path("."), first, peers=(second,), agent_factory=factory
            )
            await coordinator.start(object())

            self.assertEqual(
                coordinator.display_name(coordinator.agent_at(0)),
                "Claude (@claude-1)",
            )
            self.assertEqual(
                coordinator.display_name(coordinator.agent_at(1)),
                "Claude (@claude-2)",
            )
            self.assertEqual(coordinator.agent_tag(0), "@claude-1")

        asyncio.run(scenario())

    def test_drop_preserves_indices_and_skips_agent(self) -> None:
        async def scenario() -> None:
            entries = [
                agent_data("claude.ai", "Claude", "claude"),
                agent_data("openai.com", "Codex", "codex"),
                agent_data("google.com", "Gemini", "gemini"),
            ]

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                return FakeAgent(project_root, data["name"])

            coordinator = SessionCoordinator(
                Path("."), entries[0], peers=entries[1:], agent_factory=factory
            )
            await coordinator.start(object())
            dropped = await coordinator.drop(1)

            self.assertEqual(dropped.data["short_name"], "codex")
            self.assertFalse(coordinator.roster[1].active)
            self.assertTrue(coordinator.roster[2].active)
            self.assertTrue(coordinator.relay_active)
            self.assertIsNone(coordinator.parse_agent_tag("@codex-2: no"))

        asyncio.run(scenario())

    def test_failed_owner_falls_back_to_a_healthy_peer(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                return FakeAgent(project_root, data["name"])

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())
            failed_owner = coordinator.owner
            self.assertIsNotNone(failed_owner)
            coordinator.mark_failed(failed_owner)

            self.assertFalse(coordinator.roster[0].active)
            self.assertFalse(coordinator.relay_active)
            self.assertIs(coordinator.primary_agent, coordinator.roster[1].agent)

            await coordinator.send_prompt("continue without Claude")
            peer_agent = coordinator.roster[1].agent
            assert isinstance(peer_agent, FakeAgent)
            self.assertEqual(peer_agent.prompts, ["continue without Claude"])

        asyncio.run(scenario())
