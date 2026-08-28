import asyncio
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, call, patch

from codeswarm.agent import AgentBase
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.db import decode_session_meta
from codeswarm.acp.pinned import PinnedConversation
from codeswarm.acp.relay import RelayConversation, RelayResult
from codeswarm.session import SessionCoordinator


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
        self.roster_introductions: list[str] = []
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

    def set_roster_introduction(self, introduction: str) -> None:
        self.roster_introductions.append(introduction)


class SessionCoordinatorTests(unittest.TestCase):
    def test_pair_mode_starts_each_batch_with_the_first_agent(self) -> None:
        async def scenario() -> None:
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
                Path("."),
                agent_data("claude.ai", "Claude", "claude"),
                peers=(agent_data("openai.com", "Codex", "codex"),),
                agent_factory=factory,
            )
            await coordinator.start(object())
            coordinator.set_collaboration_mode("pair")
            assert coordinator.relay is not None
            coordinator.relay.run = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    RelayResult(2, True, "stop_token"),
                    RelayResult(2, True, "stop_token"),
                ]
            )
            coordinator.relay.next_agent_index = 1

            await coordinator.send_prompt("first batch")
            await coordinator.send_prompt("second batch")

            self.assertEqual(
                coordinator.relay.run.await_args_list,  # type: ignore[attr-defined]
                [
                    call("first batch", first_agent=0),
                    call("second batch", first_agent=0),
                ],
            )

        asyncio.run(scenario())

    def test_manual_mode_uses_a_persistent_pinned_target(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())
            self.assertIsInstance(coordinator.relay, RelayConversation)

            coordinator.set_collaboration_mode("manual")
            self.assertIsInstance(coordinator.relay, PinnedConversation)
            coordinator.select_pinned_agent(1)
            await coordinator.send_prompt("send this to Codex")
            await coordinator.send_prompt("keep this with Codex")

            self.assertEqual(created[0].prompts, [])
            self.assertEqual(len(created[1].prompts), 2)
            self.assertEqual(coordinator.collaboration_mode, "manual")

        asyncio.run(scenario())

    def test_collaboration_mode_switch_preserves_runtime_relay_state(self) -> None:
        """Switching strategy must not revive dead slots or lose user work."""

        async def scenario() -> None:
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("claude.ai", "Claude", "claude"),
                peers=(
                    agent_data("openai.com", "Codex", "codex"),
                    agent_data("geminicli.com", "Gemini", "gemini"),
                ),
                agent_factory=factory,
            )
            await coordinator.start(object())
            assert coordinator.relay is not None
            coordinator.mark_failed(created[1])
            coordinator.relay.last_active_index = 2
            coordinator.relay.next_agent_index = 2
            self.assertTrue(coordinator.enqueue_human("preserve this correction"))
            coordinator.pause()

            coordinator.set_collaboration_mode("manual")

            relay = coordinator.relay
            assert isinstance(relay, PinnedConversation)
            self.assertEqual(relay.active, [True, False, True])
            self.assertTrue(relay.paused)
            self.assertEqual(relay.queued_prompt_count, 1)
            self.assertEqual(relay.last_active_index, 2)
            self.assertEqual(relay.next_agent_index, 2)
            self.assertEqual(relay.pinned_agent_index, 2)

            coordinator.resume()
            result = await coordinator.send_prompt("", resume_queued=True)

            self.assertEqual(result, RelayResult(1, True, "turn_complete"))
            self.assertEqual(created[1].prompts, [])
            self.assertIn("preserve this correction", created[2].prompts[0])

        asyncio.run(scenario())

    def test_manual_mode_never_pins_a_tombstoned_next_agent(self) -> None:
        async def scenario() -> None:
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("claude.ai", "Claude", "claude"),
                peers=(
                    agent_data("openai.com", "Codex", "codex"),
                    agent_data("geminicli.com", "Gemini", "gemini"),
                ),
                agent_factory=factory,
            )
            await coordinator.start(object())
            assert coordinator.relay is not None
            coordinator.relay.next_agent_index = 1
            coordinator.mark_failed(created[1])

            coordinator.set_collaboration_mode("manual")

            relay = coordinator.relay
            assert isinstance(relay, PinnedConversation)
            self.assertEqual(relay.pinned_agent_index, 2)
            await coordinator.send_prompt("continue")
            self.assertEqual(len(created[2].prompts), 1)

        asyncio.run(scenario())

    def test_manual_pin_advances_when_its_agent_is_dropped(self) -> None:
        async def scenario() -> None:
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("claude.ai", "Claude", "claude"),
                peers=(
                    agent_data("openai.com", "Codex", "codex"),
                    agent_data("geminicli.com", "Gemini", "gemini"),
                ),
                agent_factory=factory,
            )
            await coordinator.start(object())
            coordinator.set_collaboration_mode("manual")
            coordinator.select_pinned_agent(1)

            await coordinator.drop(1)

            relay = coordinator.relay
            assert isinstance(relay, PinnedConversation)
            self.assertEqual(relay.pinned_agent_index, 2)

        asyncio.run(scenario())

    def test_manual_failure_keeps_an_unrelated_live_pin(self) -> None:
        async def scenario() -> None:
            created: list[FakeAgent] = []

            def factory(*args: Any, **kwargs: Any) -> FakeAgent:
                del kwargs
                agent = FakeAgent(args[0], args[1]["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("owner", "Owner", "owner"),
                peers=(
                    agent_data("pin", "Pin", "pin"),
                    agent_data("other", "Other", "other"),
                ),
                agent_factory=factory,
            )
            await coordinator.start(object())
            coordinator.set_collaboration_mode("manual")
            coordinator.select_pinned_agent(1)

            coordinator.mark_failed(created[2])

            relay = coordinator.relay
            assert isinstance(relay, PinnedConversation)
            self.assertEqual(relay.pinned_agent_index, 1)

        asyncio.run(scenario())

    def test_final_manual_failure_tombstones_without_requiring_a_live_pin(self) -> None:
        async def scenario() -> None:
            created: list[FakeAgent] = []

            def factory(*args: Any, **kwargs: Any) -> FakeAgent:
                del kwargs
                agent = FakeAgent(args[0], args[1]["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("owner", "Owner", "owner"),
                peers=(agent_data("peer", "Peer", "peer"),),
                agent_factory=factory,
            )
            await coordinator.start(object())
            coordinator.set_collaboration_mode("manual")
            coordinator.mark_failed(created[1])

            self.assertEqual(coordinator.mark_failed(created[0]), 0)
            self.assertEqual(coordinator.active_agents, [])

        asyncio.run(scenario())

    def test_failed_manual_slot_keeps_queue_until_reload_or_decline(self) -> None:
        async def scenario() -> None:
            created: list[FakeAgent] = []
            discarded: list[tuple[str, bool]] = []

            def factory(*args: Any, **kwargs: Any) -> FakeAgent:
                del kwargs
                agent = FakeAgent(args[0], args[1]["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("owner", "Owner", "owner"),
                peers=(agent_data("peer", "Peer", "peer"),),
                agent_factory=factory,
            )
            await coordinator.start(object())
            coordinator.set_collaboration_mode("manual")
            coordinator.select_pinned_agent(1)
            assert coordinator.relay is not None
            coordinator.relay.on_queued_turn_discarded = (
                lambda prompt, direct: discarded.append((prompt, direct))
            )
            self.assertTrue(coordinator.enqueue_human("accepted work"))

            coordinator.mark_failed(created[1])

            self.assertEqual(coordinator.queued_prompt_count, 1)
            self.assertEqual(discarded, [])
            replacement = await coordinator.reload_agent(created[1], object())
            assert replacement is not None
            self.assertEqual(coordinator.queued_prompt_count, 1)
            await coordinator.send_prompt("", resume_queued=True)
            self.assertEqual(coordinator.queued_prompt_count, 0)
            self.assertEqual(len(cast(FakeAgent, replacement).prompts), 1)

            coordinator.select_pinned_agent(1)
            self.assertTrue(coordinator.enqueue_human("declined work"))
            coordinator.mark_failed(replacement)
            coordinator.discard_failed_agent_queue(replacement)
            self.assertEqual(coordinator.queued_prompt_count, 0)
            self.assertEqual(discarded, [("declined work", False)])

        asyncio.run(scenario())

    def test_invalid_pinned_selection_does_not_change_target(self) -> None:
        coordinator = SessionCoordinator(
            Path("."), agent_data("claude.ai", "Claude", "claude"),
            peers=(agent_data("openai.com", "Codex", "codex"),),
        )

        with self.assertRaises(IndexError):
            coordinator.select_pinned_agent(3)

    def test_start_introduces_each_agent_to_the_roster(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            created: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())

            self.assertEqual(len(created), 2)
            self.assertIn("You are Claude", created[0].roster_introductions[0])
            self.assertIn("Codex", created[0].roster_introductions[0])
            self.assertIn("You are Codex", created[1].roster_introductions[0])
            self.assertIn("Claude", created[1].roster_introductions[0])

        asyncio.run(scenario())

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

    def test_startup_full_access_restart_preserves_roster_and_session(self) -> None:
        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data(
                "antigravity.google.com", "Antigravity CLI", "antigravity"
            )
            created: list[FakeAgent] = []
            calls: list[tuple[str, str | None, int | None, bool]] = []

            class StartupAgent(FakeAgent):
                def __init__(self, project_root: Path, name: str) -> None:
                    super().__init__(project_root, name)
                    self.enabled = True

                @property
                def supports_startup_full_access(self) -> bool:
                    return True

                @property
                def startup_full_access(self) -> bool:
                    return self.enabled

                def configure_startup_full_access(self, enabled: bool) -> None:
                    self.enabled = enabled

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                calls.append((data["short_name"], session_id, session_pk, persist))
                agent: FakeAgent
                if data["identity"] == "antigravity.google.com":
                    agent = StartupAgent(project_root, data["name"])
                else:
                    agent = FakeAgent(project_root, data["name"])
                agent.session_id = session_id  # type: ignore[attr-defined]
                created.append(agent)
                return agent

            target = object()
            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(target)
            antigravity = created[1]
            antigravity.session_id = "agy-session"  # type: ignore[attr-defined]

            replacement = await coordinator.restart_for_startup_full_access(
                antigravity, target, enabled=False
            )

            self.assertIs(replacement, created[2])
            self.assertTrue(antigravity.stopped)
            self.assertFalse(replacement.startup_full_access)
            self.assertIs(coordinator.roster[1].agent, replacement)
            self.assertIs(coordinator.agent_at(1), replacement)
            self.assertEqual(
                calls[-1], ("antigravity", "agy-session", None, False)
            )
            self.assertEqual(replacement.started_with, [target])

        asyncio.run(scenario())

    def test_failed_startup_full_access_restart_tombstones_stopped_agent(self) -> None:
        """A failed replacement must not leave the stopped adapter dispatchable."""

        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data(
                "antigravity.google.com", "Antigravity CLI", "antigravity"
            )
            created: list[FakeAgent] = []

            class StartupAgent(FakeAgent):
                @property
                def supports_startup_full_access(self) -> bool:
                    return True

                @property
                def startup_full_access(self) -> bool:
                    return True

                def configure_startup_full_access(self, enabled: bool) -> None:
                    pass

            class FailingReplacement(StartupAgent):
                async def start(self, message_target: Any) -> None:
                    raise RuntimeError("replacement failed")

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                if len(created) < 2:
                    agent: FakeAgent = StartupAgent(project_root, data["name"])
                else:
                    agent = FailingReplacement(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())
            failed = created[1]

            replacement = await coordinator.restart_for_startup_full_access(
                failed, object(), enabled=False
            )

            self.assertIsNone(replacement)
            self.assertTrue(failed.stopped)
            self.assertFalse(coordinator.roster[1].active)
            assert coordinator.relay is not None
            self.assertFalse(coordinator.relay.active[1])
            self.assertEqual(coordinator.active_agents, [created[0]])
            self.assertTrue(created[2].stopped)

        asyncio.run(scenario())

    def test_mode_restart_reload_uses_the_requested_startup_access(self) -> None:
        async def scenario(initial: bool, requested: bool) -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data(
                "antigravity.google.com", "Antigravity CLI", "antigravity"
            )
            startup_agents: list[Any] = []

            class StartupAgent(FakeAgent):
                def __init__(self, project_root: Path, name: str, fail: bool) -> None:
                    super().__init__(project_root, name)
                    self.enabled = initial
                    self.fail = fail
                    self.configured: list[bool] = []
                    self.started_access: list[bool] = []

                @property
                def supports_startup_full_access(self) -> bool:
                    return True

                @property
                def startup_full_access(self) -> bool:
                    return self.enabled

                def configure_startup_full_access(self, enabled: bool) -> None:
                    self.configured.append(enabled)
                    self.enabled = enabled

                async def start(self, message_target: Any) -> None:
                    self.started_access.append(self.enabled)
                    if self.fail:
                        raise RuntimeError("replacement failed")
                    await super().start(message_target)

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                del session_id, session_pk, persist
                if data["identity"] != "antigravity.google.com":
                    return FakeAgent(project_root, data["name"])
                agent = StartupAgent(
                    project_root,
                    data["name"],
                    fail=len(startup_agents) == 1,
                )
                startup_agents.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=(peer,), agent_factory=factory
            )
            await coordinator.start(object())
            original = startup_agents[0]

            self.assertIsNone(
                await coordinator.restart_for_startup_full_access(
                    original, object(), enabled=requested
                )
            )
            retry = await coordinator.reload_agent(original, object())

            assert isinstance(retry, StartupAgent)
            self.assertEqual(retry.configured, [requested])
            self.assertEqual(retry.started_access, [requested])
            self.assertEqual(retry.startup_full_access, requested)

        for initial, requested in ((True, False), (False, True)):
            with self.subTest(initial=initial, requested=requested):
                asyncio.run(scenario(initial, requested))

    def test_crash_reload_preserves_restricted_startup_access(self) -> None:
        async def scenario() -> None:
            created: list[Any] = []

            class RestrictedAgent(FakeAgent):
                def __init__(self, project_root: Path, name: str) -> None:
                    super().__init__(project_root, name)
                    # Match adapters whose constructor defaults to full access.
                    self.enabled = True
                    self.configured: list[bool] = []
                    self.started_access: list[bool] = []

                @property
                def supports_startup_full_access(self) -> bool:
                    return True

                @property
                def startup_full_access(self) -> bool:
                    return self.enabled

                def configure_startup_full_access(self, enabled: bool) -> None:
                    self.configured.append(enabled)
                    self.enabled = enabled

                async def start(self, message_target: Any) -> None:
                    self.started_access.append(self.enabled)
                    await super().start(message_target)

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> RestrictedAgent:
                del session_id, session_pk, persist
                agent = RestrictedAgent(project_root, data["name"])
                created.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."),
                agent_data("antigravity.google.com", "Antigravity", "agy"),
                agent_factory=factory,
            )
            await coordinator.start(object())
            failed = created[0]
            failed.enabled = False
            coordinator.mark_failed(failed)

            replacement = await coordinator.reload_agent(failed, object())

            assert isinstance(replacement, RestrictedAgent)
            self.assertEqual(replacement.configured, [False])
            self.assertEqual(replacement.started_access, [False])

        asyncio.run(scenario())

    def test_declining_solo_mode_restart_clears_pending_access(self) -> None:
        coordinator = SessionCoordinator(
            Path("."),
            agent_data("antigravity.google.com", "Antigravity", "agy"),
        )
        failed = FakeAgent(Path("."), "Antigravity")
        coordinator.roster[0].agent = failed
        coordinator._pending_startup_full_access[0] = False

        coordinator.discard_failed_agent_queue(failed)

        self.assertEqual(coordinator._pending_startup_full_access, {})

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

            with patch("codeswarm.session.CANCEL_TIMEOUT_SECONDS", 0.01):
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

    def test_persist_roster_can_keep_a_separate_next_launch_order(self) -> None:
        async def scenario() -> None:
            claude = agent_data("claude.ai", "Claude", "claude")
            codex = agent_data("openai.com", "Codex", "codex")
            coordinator = SessionCoordinator(Path("."), claude, peers=(codex,))
            stored: dict[str, object] = {}

            class Settings:
                def set(self, key: str, value: object) -> None:
                    stored[key] = value

            async def save_settings() -> None:
                pass

            await coordinator.persist_roster(
                Settings(), save_settings, ["openai.com", "claude.ai"]
            )

            self.assertEqual(
                stored["launcher.roster"], "openai.com\nclaude.ai"
            )

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

    def test_reload_replaces_a_failed_agent_in_its_own_slot(self) -> None:
        """A crash must not cost the slot, the peers' indices, or the context."""

        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peers = (
                agent_data("openai.com", "Codex", "codex"),
                agent_data("geminicli.com", "Gemini", "gemini"),
            )
            built: list[FakeAgent] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                agent = FakeAgent(project_root, data["name"])
                agent.requested_session_id = session_id  # type: ignore[attr-defined]
                built.append(agent)
                return agent

            coordinator = SessionCoordinator(
                Path("."), owner, peers=peers, agent_factory=factory
            )
            await coordinator.start(object())
            relay = coordinator.relay
            assert relay is not None
            failed = coordinator.roster[1].agent
            assert failed is not None

            # The slot is tombstoned first, exactly as a failure leaves it.
            self.assertEqual(coordinator.mark_failed(failed), 1)
            self.assertFalse(coordinator.roster[1].active)
            self.assertIsNone(coordinator.index_of_agent(failed))
            # ...but the slot is still findable, or there is nowhere to reload.
            self.assertEqual(coordinator.index_of_agent_slot(failed), 1)

            replacement = await coordinator.reload_agent(failed, object())

            self.assertIsNotNone(replacement)
            self.assertIsNot(replacement, failed)
            self.assertTrue(failed.stopped)
            # Same slot: peers keep their indices and their colours.
            self.assertIs(coordinator.roster[1].agent, replacement)
            self.assertTrue(coordinator.roster[1].active)
            self.assertIs(relay.agents[1], replacement)
            self.assertTrue(relay.active[1])
            self.assertEqual(coordinator.roster[0].data["name"], "Claude")
            self.assertEqual(coordinator.roster[2].data["name"], "Gemini")
            # And it is told who it is working with again.
            assert replacement is not None
            self.assertTrue(replacement.roster_introductions)

        asyncio.run(scenario())

    def test_reload_replays_the_context_the_slot_missed(self) -> None:
        """Otherwise the reloaded agent returns with no idea of the task."""

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
            relay = coordinator.relay
            assert relay is not None
            context = relay.context

            # Both agents have caught up on a conversation so far.
            context.shared_task = "port the parser"
            context.record_event("Human", "start with the lexer", relay.active)
            context.record_event("Claude", "lexer done", relay.active)
            context.seen_event_count[0] = len(context.public_events)
            context.seen_event_count[1] = len(context.public_events)

            failed = coordinator.roster[1].agent
            assert failed is not None
            coordinator.mark_failed(failed)
            replacement = await coordinator.reload_agent(failed, object())
            self.assertIsNotNone(replacement)

            # The reloaded slot is rewound, so its next turn replays the
            # conversation; its healthy peer is left alone.
            self.assertEqual(context.seen_event_count[1], 0)
            self.assertEqual(
                context.seen_event_count[0], len(context.public_events)
            )
            replay = context.unseen_updates(1, excluding=None)
            self.assertIn("start with the lexer", replay)
            self.assertIn("lexer done", replay)
            # The shared task survives, so the turn prompt still states it.
            self.assertEqual(context.shared_task, "port the parser")

        asyncio.run(scenario())

    def test_reload_only_resumes_a_session_the_adapter_can_load(self) -> None:
        """Handing a session id to an adapter that cannot load one re-crashes it."""

        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peer = agent_data("openai.com", "Codex", "codex")
            requested: list[str | None] = []

            def factory(
                project_root: Path,
                data: AgentData,
                session_id: str | None,
                session_pk: int | None,
                *,
                persist: bool = True,
            ) -> FakeAgent:
                requested.append(session_id)
                return FakeAgent(project_root, data["name"])

            for supports_load, expected in ((False, None), (True, "session-7")):
                with self.subTest(supports_load_session=supports_load):
                    requested.clear()
                    coordinator = SessionCoordinator(
                        Path("."), owner, peers=(peer,), agent_factory=factory
                    )
                    await coordinator.start(object())
                    failed = coordinator.roster[1].agent
                    assert failed is not None
                    failed.session_id = "session-7"  # type: ignore[attr-defined]
                    failed.supports_load_session = supports_load  # type: ignore[attr-defined]

                    coordinator.mark_failed(failed)
                    self.assertIsNotNone(
                        await coordinator.reload_agent(failed, object())
                    )
                    self.assertEqual(requested[-1], expected)

        asyncio.run(scenario())

    def test_queued_message_follows_an_explicit_selection(self) -> None:
        """A queued message goes to the selected agent, not the working one.

        The prompt footer already names the selection as the next recipient,
        so queueing to whichever agent happened to be working meant the UI
        promised one agent and delivered to another.
        """

        async def scenario() -> None:
            owner = agent_data("claude.ai", "Claude", "claude")
            peers = (
                agent_data("openai.com", "Codex", "codex"),
                agent_data("geminicli.com", "Gemini", "gemini"),
            )

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
                Path("."), owner, peers=peers, agent_factory=factory
            )
            await coordinator.start(object())
            relay = coordinator.relay
            assert relay is not None

            # The agent at index 2 owns the active turn.
            relay.last_active_index = 2

            # With no selection the active agent keeps the follow-up.
            self.assertTrue(coordinator.enqueue_human("no selection"))
            self.assertEqual(relay._steering_queue[-1][0], 2)

            # An explicit selection takes precedence.
            coordinator.select_agent(0)
            self.assertTrue(coordinator.enqueue_human("for agent zero"))
            self.assertEqual(relay._steering_queue[-1][0], 0)

            # A selection naming a dropped agent is refused rather than
            # silently retargeted at whoever is working.
            relay.active[1] = False
            coordinator.selected_agent_index = 1
            self.assertFalse(coordinator.enqueue_human("for a dropped agent"))

        asyncio.run(scenario())

    def test_select_agent_sets_the_next_relay_recipient(self) -> None:
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

            coordinator.select_agent(1)

            self.assertEqual(coordinator.first_agent, 1)

            coordinator.roster[1].active = False
            with self.assertRaises(IndexError):
                coordinator.select_agent(1)

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

    def test_duplicate_display_names_include_their_roster_number(self) -> None:
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
                "Claude (1)",
            )
            self.assertEqual(
                coordinator.display_name(coordinator.agent_at(1)),
                "Claude (2)",
            )

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
            with self.assertRaises(IndexError):
                coordinator.select_agent(1)
            coordinator.select_agent(2)
            self.assertEqual(coordinator.first_agent, 2)

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
