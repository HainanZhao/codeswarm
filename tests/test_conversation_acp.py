import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from textual.color import Color
from textual.widgets import Label
from textual.widgets._markdown import (
    MarkdownBlockQuote,
    MarkdownFence,
    MarkdownH1,
    MarkdownHorizontalRule,
    MarkdownParagraph,
    MarkdownTable,
    MarkdownTableContent,
)

from codeswarm import jsonrpc
from codeswarm.acp import messages as acp_messages
from codeswarm.acp.agent import Mode
from codeswarm.acp.relay import MAX_QUEUED_PROMPTS, RelayConversation, RelayResult
from codeswarm.agent import AgentFail, AgentReady
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.app import CodeSwarmApp
from codeswarm import messages
from codeswarm.session import RosterEntry
from codeswarm.slash_command import SlashCommand
from codeswarm.widgets.conversation import AGENT_FAIL_HELP, Conversation
from codeswarm.widgets.agent_response import (
    AgentMessage,
    AgentResponse,
    AgentToolActivity,
)
from codeswarm.widgets.agent_thought import AgentThought
from codeswarm.widgets import agent_response as agent_response_widget
from codeswarm.widgets.markdown_note import MarkdownNote
from codeswarm.widgets.note import Note
from codeswarm.widgets.path_search import PathSearch
from codeswarm.widgets.prompt import (
    AgentInfo,
    CollaborationInfo,
    InvokeFileSearch,
    QueuedMessages,
)
from codeswarm.widgets.flash import Flash
from codeswarm.screens.config import ConfigScreen
from codeswarm.screens.permissions import PermissionsQuestion
from codeswarm.answer import Answer
from codeswarm.widgets.terminal_tool import TerminalTool
from codeswarm.widgets.tool_call import ToolCall
from codeswarm.widgets.user_input import UserInput
from codeswarm.widgets.conversation_acp import is_mode_update_notice


class _RosterAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.set_mode = AsyncMock(return_value=None)

    def get_info(self) -> str:
        return self.name

    async def stop(self) -> None:
        pass


class ConversationACPDispatchTests(unittest.TestCase):
    def test_live_roster_adds_before_dropping_and_preserves_launcher_order(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                owner = _RosterAgent("Claude")
                peer = _RosterAgent("Gemini")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(identity="claude.com", name="Claude", short_name="claude"),
                        owner,  # type: ignore[arg-type]
                    ),
                    RosterEntry(
                        AgentData(identity="geminicli.com", name="Gemini", short_name="gemini"),
                        peer,  # type: ignore[arg-type]
                    ),
                ]
                conversation.agent = owner  # type: ignore[assignment]
                conversation._ready_agents = {id(owner), id(peer)}
                events: list[tuple[str, object]] = []

                async def add(data: AgentData, _target: object, **_kwargs: object) -> None:
                    events.append(("add", data["identity"]))
                    conversation.session.roster.append(
                        RosterEntry(data, _RosterAgent(data["name"]))  # type: ignore[arg-type]
                    )

                async def drop(index: int) -> RosterEntry:
                    events.append(("drop", index))
                    entry = conversation.session.roster[index]
                    entry.active = False
                    return entry

                conversation.session.add = add  # type: ignore[method-assign]
                conversation.session.drop = drop  # type: ignore[method-assign]
                persist = AsyncMock()
                conversation._persist_roster = persist  # type: ignore[method-assign]
                catalog = {
                    "claude.com": conversation.session.roster[0].data,
                    "openai.com": AgentData(
                        identity="openai.com", name="Codex", short_name="codex"
                    ),
                }

                failures = await conversation.reconcile_roster(
                    ["openai.com", "claude.com"], catalog
                )

                self.assertEqual(failures, [])
                self.assertEqual(events, [("add", "openai.com"), ("drop", 1)])
                self.assertEqual(
                    [entry.data["identity"] for entry in conversation.session.roster if entry.active],
                    ["claude.com", "openai.com"],
                )
                self.assertNotIn(id(peer), conversation._ready_agents)
                persist.assert_awaited_once_with(["openai.com", "claude.com"])

        asyncio.run(scenario())

    def test_live_roster_add_failure_keeps_healthy_peer(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                owner = _RosterAgent("Claude")
                peer = _RosterAgent("Gemini")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(identity="claude.com", name="Claude", short_name="claude"),
                        owner,  # type: ignore[arg-type]
                    ),
                    RosterEntry(
                        AgentData(identity="geminicli.com", name="Gemini", short_name="gemini"),
                        peer,  # type: ignore[arg-type]
                    ),
                ]
                conversation.session.add = AsyncMock(  # type: ignore[method-assign]
                    side_effect=RuntimeError("adapter unavailable")
                )
                conversation.session.drop = AsyncMock()  # type: ignore[method-assign]
                persist = AsyncMock()
                conversation._persist_roster = persist  # type: ignore[method-assign]
                catalog = {
                    "claude.com": conversation.session.roster[0].data,
                    "openai.com": AgentData(
                        identity="openai.com", name="Codex", short_name="codex"
                    ),
                }

                failures = await conversation.reconcile_roster(
                    ["claude.com", "openai.com"], catalog
                )

                self.assertEqual(failures, ["Codex"])
                conversation.session.drop.assert_not_awaited()  # type: ignore[attr-defined]
                self.assertTrue(conversation.session.roster[1].active)
                persist.assert_awaited_once_with(["claude.com", "geminicli.com"])

        asyncio.run(scenario())

    def test_token_only_reviewer_stop_renders_default_acknowledgment(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        reviewer = _RosterAgent("Gemini")
                        reviewer.last_response = "[CODESWARM:STOP]"
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                reviewer,  # type: ignore[arg-type]
                            )
                        ]

                        await conversation._label_relay_turn(
                            2, reviewer, "👍"  # type: ignore[arg-type]
                        )
                        await pilot.pause(0.1)

                        responses = list(conversation.query(AgentResponse))
                        self.assertEqual(len(responses), 1)
                        self.assertIn("👍", responses[0].source)

        asyncio.run(scenario())

    def test_reply_timestamp_uses_compact_local_friendly_dates(self) -> None:
        now = datetime(2026, 8, 24, 17, 45, tzinfo=timezone.utc)
        format_reply_timestamp = getattr(
            agent_response_widget,
            "format_reply_timestamp",
            lambda *_args, **_kwargs: "timestamp formatter missing",
        )

        self.assertEqual(
            format_reply_timestamp(
                datetime(2026, 8, 24, 17, 42, tzinfo=timezone.utc), now=now
            ),
            "5:42 PM",
        )
        self.assertEqual(
            format_reply_timestamp(
                datetime(2026, 8, 23, 17, 42, tzinfo=timezone.utc), now=now
            ),
            "Aug 23, 5:42 PM",
        )
        self.assertEqual(
            format_reply_timestamp(
                datetime(2025, 8, 23, 17, 42, tzinfo=timezone.utc), now=now
            ),
            "Aug 23, 2025, 5:42 PM",
        )

    def test_adapter_mode_notices_are_not_conversation_content(self) -> None:
        self.assertTrue(is_mode_update_notice("Mode update: YOLO"))
        self.assertTrue(is_mode_update_notice("MODE_UPDATE yolo"))
        self.assertTrue(is_mode_update_notice("mode-update fully-auto"))
        self.assertTrue(is_mode_update_notice("mode changed; fully-auto"))
        self.assertTrue(is_mode_update_notice("[Mode updated: Plan]"))
        self.assertFalse(is_mode_update_notice("The mode update fixed the issue."))

    def test_connection_status_names_every_agent_in_the_roster(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.agent_ready = False
                        conversation._ready_agents.clear()

                        with patch.object(conversation, "flash") as flash:
                            await conversation.on_agent_ready(AgentReady(claude))
                            first_status = flash.call_args.args[0]
                            self.assertEqual(
                                first_status.plain,
                                "COMMS // Claude LINK ESTABLISHED · AWAITING Gemini",
                            )
                            self.assertFalse(conversation.agent_ready)

                            await conversation.on_agent_ready(AgentReady(gemini))
                            final_status = flash.call_args.args[0]
                            self.assertEqual(
                                final_status.plain,
                                "FORMATION // Claude + Gemini ON STATION",
                            )
                            self.assertTrue(conversation.agent_ready)

        asyncio.run(scenario())

    def test_permission_confirmation_requires_a_deliberate_selection(self) -> None:
        question = PermissionsQuestion(
            options=[Answer("Allow", "allow", "allow_once")]
        )

        self.assertEqual(question.selection, -1)

    def test_agent_work_and_completion_are_shown_only_in_the_roster(self) -> None:
        """Agent status stays compact instead of adding transcript blocks."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation._ready_agents = {id(claude), id(gemini)}
                        relay = RelayConversation((claude, gemini))  # type: ignore[arg-type]
                        conversation.session.relay = relay

                        clock = Mock(return_value=100.0)
                        with patch("codeswarm.widgets.conversation.monotonic", clock):
                            await conversation._label_relay_turn_start(
                                2, gemini  # type: ignore[arg-type]
                            )
                            self.assertFalse(
                                conversation.agent_info.plain.startswith("Agents:")
                            )
                            self.assertIn("→ ● Gemini · 0:00", conversation.agent_info.plain)
                            self.assertEqual(len(conversation.query("Loading")), 0)

                            clock.return_value = 105.0
                            await conversation._label_relay_turn(
                                2, gemini, ""  # type: ignore[arg-type]
                            )
                            relay.next_agent_index = 0
                            conversation._mark_collaboration_complete()

                        self.assertEqual(
                            conversation.agent_info.plain, "→ ○ Claude · ○ Gemini"
                        )
                        self.assertNotIn(
                            "Collaboration complete",
                            " ".join(
                                getattr(child, "content", "")
                                for child in conversation.contents.children
                            ),
                        )

        asyncio.run(scenario())

    def test_returning_relay_agent_preserves_batch_elapsed_timer(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation._ready_agents = {id(claude), id(gemini)}
                        conversation.session.relay = RelayConversation(
                            (claude, gemini)  # type: ignore[arg-type]
                        )

                        clock = Mock(return_value=100.0)
                        with patch("codeswarm.widgets.conversation.monotonic", clock):
                            await conversation._label_relay_turn_start(
                                1, claude  # type: ignore[arg-type]
                            )
                            clock.return_value = 105.0
                            await conversation._label_relay_turn(
                                1, claude, "first"  # type: ignore[arg-type]
                            )

                            await conversation._label_relay_turn_start(
                                2, gemini  # type: ignore[arg-type]
                            )
                            clock.return_value = 108.0
                            await conversation._label_relay_turn(
                                2, gemini, "second"  # type: ignore[arg-type]
                            )

                            await conversation._label_relay_turn_start(
                                3, claude  # type: ignore[arg-type]
                            )

                            self.assertIn(
                                "● Claude · 0:05", conversation.agent_info.plain
                            )
                            conversation._finish_agent_status(claude)  # type: ignore[arg-type]

        asyncio.run(scenario())

    def test_stop_token_does_not_add_a_completion_message(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session.relay = RelayConversation(
                            (claude, gemini)  # type: ignore[arg-type]
                        )
                        conversation.session.send_prompt = AsyncMock(
                            return_value=RelayResult(1, True, "stop_token")
                        )  # type: ignore[method-assign]

                        await conversation.send_prompt_to_agent.__wrapped__(
                            conversation, "Done?"
                        )
                        await pilot.pause()

                        transcript = "\n".join(
                            str(getattr(child, "content", ""))
                            for child in conversation.contents.children
                        )
                        self.assertNotIn("Collaboration complete", transcript)
                        self.assertTrue(conversation._collaboration_complete)

        asyncio.run(scenario())

    def test_stop_token_adds_per_agent_batch_elapsed_summary(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                gemini = _RosterAgent("Gemini")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.com", name="Claude", short_name="claude"
                        ),
                        claude,  # type: ignore[arg-type]
                    ),
                    RosterEntry(
                        AgentData(
                            identity="geminicli.com", name="Gemini", short_name="gemini"
                        ),
                        gemini,  # type: ignore[arg-type]
                    ),
                ]
                conversation.session.relay = RelayConversation(
                    (claude, gemini)  # type: ignore[arg-type]
                )
                conversation._agent_elapsed = {
                    id(claude): 42,
                    id(gemini): 8,
                }
                await conversation._post_collaboration_summary()
                await pilot.pause(0.1)

                summaries = [
                    note.render().plain
                    for note in conversation.query(Note)
                    if "Batch complete" in note.render().plain
                ]
                self.assertEqual(summaries, ["Batch complete · Claude 0:42 · Gemini 0:08"])
                conversation._begin_collaboration()
                self.assertEqual(conversation._agent_elapsed, {})

        asyncio.run(scenario())

    def test_batch_summary_waits_for_the_final_agent_reply_render(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.com", name="Claude", short_name="claude"
                        ),
                        claude,  # type: ignore[arg-type]
                    )
                ]
                conversation.post_message(
                    acp_messages.Update("text", "last agent reply", claude)  # type: ignore[arg-type]
                )
                await conversation._post_collaboration_summary()
                await pilot.pause()

                children = list(conversation.contents.children)
                summary_index = next(
                    index
                    for index, child in enumerate(children)
                    if isinstance(child, Note)
                    and "Batch complete" in child.render().plain
                )
                response_index = next(
                    index
                    for index, child in enumerate(children)
                    if isinstance(child, AgentMessage)
                )
                self.assertLess(response_index, summary_index)

        asyncio.run(scenario())

    def test_batch_elapsed_accumulates_repeated_agent_rounds(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.com", name="Claude", short_name="claude"
                        ),
                        claude,  # type: ignore[arg-type]
                    )
                ]
                clock = Mock(return_value=100.0)
                with patch("codeswarm.widgets.conversation.monotonic", clock):
                    conversation._begin_collaboration()
                    conversation._begin_agent_status(claude)  # type: ignore[arg-type]
                    clock.return_value = 106.0
                    conversation._finish_agent_status(claude)  # type: ignore[arg-type]

                    conversation._begin_agent_status(claude)  # type: ignore[arg-type]
                    clock.return_value = 110.0
                    conversation._finish_agent_status(claude)  # type: ignore[arg-type]

                self.assertEqual(conversation._agent_elapsed[id(claude)], 10)

        asyncio.run(scenario())

    def test_mode_selection_targets_the_agent_that_advertised_it(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            claude = _RosterAgent("Claude")
            gemini = _RosterAgent("Gemini")
            conversation._select_agent_modes(claude)  # type: ignore[arg-type]
            conversation.set_agent_modes(
                {"default": Mode("default", "Default", None)},
                "default",
                claude,  # type: ignore[arg-type]
            )
            conversation.set_agent_modes(
                {"yolo": Mode("yolo", "YOLO", None)},
                "yolo",
                gemini,  # type: ignore[arg-type]
            )

            # Gemini's update must not replace Claude's visible mode list.
            self.assertEqual(
                set(conversation.modes),
                {"default", "codeswarm:discuss"},
            )

            conversation._select_agent_modes(gemini)  # type: ignore[arg-type]
            with patch.object(conversation, "flash"):
                await conversation.set_mode("yolo")

            gemini.set_mode.assert_awaited_once_with("yolo")
            claude.set_mode.assert_not_awaited()

        asyncio.run(scenario())

    def test_roster_mode_is_translated_and_applied_to_every_agent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.set_agent_modes(
                            {
                                "auto": Mode("auto", "Auto", "Classified"),
                                "default": Mode("default", "Manual", "Prompt"),
                                "acceptEdits": Mode(
                                    "acceptEdits", "Accept Edits", "Approve edits"
                                ),
                                "plan": Mode("plan", "Plan Mode", "Read-only"),
                                "bypassPermissions": Mode(
                                    "bypassPermissions", "Bypass Permissions", "All"
                                ),
                                "dontAsk": Mode("dontAsk", "Don't Ask", "Deny"),
                            },
                            "auto",
                            claude,  # type: ignore[arg-type]
                        )
                        conversation.set_agent_modes(
                            {
                                "default": Mode("default", "Default", "Prompt"),
                                "autoEdit": Mode(
                                    "autoEdit", "Auto Edit", "Approve edits"
                                ),
                                "plan": Mode("plan", "Plan", "Read-only"),
                                "yolo": Mode("yolo", "YOLO", "All"),
                            },
                            "autoEdit",
                            gemini,  # type: ignore[arg-type]
                        )

                        self.assertEqual(
                            set(conversation.modes),
                            {
                                "codeswarm:discuss",
                                "codeswarm:mode:manual",
                                "codeswarm:mode:accept-edits",
                                "codeswarm:mode:plan",
                                "codeswarm:mode:full-access",
                            },
                        )
                        self.assertNotIn("auto", conversation.modes)
                        self.assertNotIn("dontAsk", conversation.modes)
                        self.assertIsNotNone(conversation.current_mode)
                        assert conversation.current_mode is not None
                        self.assertEqual(conversation.current_mode.name, "Auto pilot")
                        ordered_mode_ids = [
                            conversation.prompt.mode_switcher.get_option_at_index(
                                index
                            ).id
                            for index in range(5)
                        ]
                        self.assertEqual(
                            ordered_mode_ids,
                            [
                                "codeswarm:discuss",
                                "codeswarm:mode:plan",
                                "codeswarm:mode:manual",
                                "codeswarm:mode:accept-edits",
                                "codeswarm:mode:full-access",
                            ],
                        )
                        self.assertEqual(
                            [
                                conversation.modes[mode_id].name
                                for mode_id in ordered_mode_ids
                            ],
                            ["Chat", "Plan", "Manual", "Accept Edits", "Auto pilot"],
                        )

                        # Adapter defaults are synchronized to CodeSwarm's
                        # roster-wide default as soon as all catalogs arrive.
                        await conversation._sync_desired_mode()
                        claude.set_mode.assert_awaited_once_with(
                            "bypassPermissions"
                        )
                        gemini.set_mode.assert_awaited_once_with("yolo")
                        claude.set_mode.reset_mock()
                        gemini.set_mode.reset_mock()

                        with patch.object(conversation, "flash") as mode_flash:
                            await conversation.set_mode(
                                "codeswarm:mode:accept-edits"
                            )
                        mode_flash.assert_not_called()

                        claude.set_mode.assert_awaited_once_with("acceptEdits")
                        gemini.set_mode.assert_awaited_once_with("autoEdit")
                        self.assertIsNotNone(conversation.current_mode)
                        assert conversation.current_mode is not None
                        self.assertEqual(
                            conversation.current_mode.id,
                            "codeswarm:mode:accept-edits",
                        )
                        self.assertEqual(conversation.prompt.mode_owner, "")

                        # A native adapter update must not leave the roster in
                        # a mixed state; CodeSwarm restores the desired policy.
                        claude.set_mode.reset_mock()
                        gemini.set_mode.reset_mock()
                        await conversation.on_mode_update(
                            acp_messages.ModeUpdate(
                                "default", gemini  # type: ignore[arg-type]
                            )
                        )
                        gemini.set_mode.assert_awaited_once_with("autoEdit")
                        claude.set_mode.assert_not_awaited()
                        self.assertEqual(
                            conversation.current_mode.id,
                            "codeswarm:mode:accept-edits",
                        )

        asyncio.run(scenario())

    def test_startup_backed_auto_pilot_is_visible_and_switchable(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)

                        class StartupAgent(_RosterAgent):
                            def __init__(self, enabled: bool) -> None:
                                super().__init__("Antigravity CLI")
                                self.enabled = enabled

                            @property
                            def supports_startup_full_access(self) -> bool:
                                return True

                            @property
                            def startup_full_access(self) -> bool:
                                return self.enabled

                        modes = {
                            "default": Mode("default", "Default", "Prompt"),
                            "accept-edits": Mode(
                                "accept-edits", "Accept Edits", "Approve edits"
                            ),
                            "plan": Mode("plan", "Plan", "Read-only"),
                        }
                        agent = StartupAgent(True)
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="antigravity.google.com",
                                    name="Antigravity CLI",
                                    short_name="antigravity",
                                ),
                                agent,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.agent = agent  # type: ignore[assignment]
                        conversation.set_agent_modes(modes, "default", agent)  # type: ignore[arg-type]

                        self.assertIn(
                            "codeswarm:mode:full-access", conversation.modes
                        )
                        self.assertEqual(
                            conversation.current_mode.id,
                            "codeswarm:mode:full-access",
                        )
                        self.assertEqual(
                            conversation.prompt.mode_switcher.get_option_at_index(4).id,
                            "codeswarm:mode:full-access",
                        )

                        replacements: list[tuple[StartupAgent, bool]] = []

                        async def restart(
                            old_agent: StartupAgent,
                            _target: object,
                            *,
                            enabled: bool,
                        ) -> StartupAgent:
                            replacement = StartupAgent(enabled)
                            replacement.session_id = "agy-session"  # type: ignore[attr-defined]
                            conversation.session.roster[0].agent = replacement  # type: ignore[assignment]
                            replacements.append((replacement, enabled))
                            return replacement

                        conversation.session.restart_for_startup_full_access = restart  # type: ignore[method-assign]

                        conversation.turn = "agent"
                        await conversation.set_mode("codeswarm:mode:manual")
                        self.assertEqual(replacements, [])

                        await conversation.agent_turn_over("end_turn")
                        manual_agent = replacements[-1][0]
                        conversation.set_agent_modes(modes, "default", manual_agent)  # type: ignore[arg-type]
                        self.assertEqual(conversation.current_mode.name, "Manual")

                        await conversation.set_mode("codeswarm:mode:full-access")
                        auto_agent = replacements[-1][0]
                        conversation.set_agent_modes(modes, "default", auto_agent)  # type: ignore[arg-type]
                        self.assertEqual(replacements[-1][1], True)
                        self.assertEqual(conversation.current_mode.name, "Auto pilot")

        asyncio.run(scenario())

    def test_resume_failure_help_uses_current_launcher_controls(self) -> None:
        help_text = AGENT_FAIL_HELP["no_resume"]

        self.assertIn("/close", help_text)
        self.assertIn("Ctrl+C", help_text)
        self.assertNotIn("dropdown", help_text)
        self.assertNotIn("Install\"", help_text)

    def test_resume_is_not_a_codeswarm_command(self) -> None:
        conversation = Conversation(Path.cwd())

        self.assertNotIn(
            "/resume", [command.command for command in conversation._build_slash_commands()]
        )

    def test_failed_terminal_start_is_not_left_registered(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)
                        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                        with patch.object(
                            TerminalTool,
                            "start",
                            side_effect=RuntimeError("terminal startup failed"),
                        ):
                            conversation.post_message(
                                acp_messages.CreateTerminal(
                                    "retryable-terminal", "echo", result
                                )
                            )
                            await pilot.pause(0.2)

                        self.assertFalse(await result)
                        self.assertNotIn("retryable-terminal", conversation.terminals)

        asyncio.run(scenario())

    def test_file_indexing_is_on_demand(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch.object(PathSearch, "refresh_paths") as refresh_paths:
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        # Mounting a conversation must not recursively scan a
                        # repository just to render an unused file picker.
                        refresh_paths.assert_not_called()

                        conversation = pilot.app.screen.query_one(Conversation)
                        conversation.prompt.post_message(InvokeFileSearch())
                        await pilot.pause(0.1)
                        refresh_paths.assert_called_once_with()

        asyncio.run(scenario())

    def test_bang_command_runs_locally_and_is_not_routed_to_an_agent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        with patch.object(
                            conversation, "run_local_shell"
                        ) as run_shell, patch.object(
                            conversation, "send_prompt_to_agent"
                        ) as send_agent:
                            await conversation.on_user_input_submitted(
                                messages.UserInputSubmitted("! printf hello")
                            )

                        run_shell.assert_called_once_with("printf hello")
                        send_agent.assert_not_called()
                        self.assertEqual(
                            conversation.query(UserInput).last().content,
                            "! printf hello",
                        )
                        self.assertNotEqual(conversation.turn, "agent")

        asyncio.run(scenario())

    def test_user_message_is_a_right_aligned_hud_uplink(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        message = await conversation.post(UserInput("Hello agents"))
                        await pilot.pause()

                        bubbles = list(message.query("#user-bubble"))
                        self.assertEqual(len(bubbles), 1)
                        bubble = bubbles[0]
                        self.assertLessEqual(
                            bubble.region.width,
                            int(message.content_region.width * 0.8) + 1,
                        )
                        self.assertEqual(
                            bubble.region.right, message.content_region.right
                        )
                        self.assertEqual(bubble.styles.background.rgb, (23, 62, 67))
                        self.assertEqual(bubble.styles.background.a, 1.0)
                        self.assertEqual(bubble.styles.border_top[0], "tall")
                        self.assertEqual(bubble.styles.border_bottom[0], "tall")
                        self.assertEqual(bubble.styles.border_right[0], "tall")
                        self.assertEqual(bubble.styles.padding.top, 0)
                        self.assertEqual(bubble.styles.padding.bottom, 0)
                        self.assertIsNone(bubble.query_one_optional("#prompt"))

        asyncio.run(scenario())

    def test_prompt_footer_uses_compact_vertical_spacing(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        prompt_container = conversation.prompt.query_one(
                            "#prompt-container"
                        )
                        info_container = conversation.prompt.query_one(
                            "#info-container"
                        )
                        self.assertEqual(prompt_container.size.height, 1)
                        self.assertEqual(prompt_container.styles.border_top[0], "")
                        self.assertEqual(
                            prompt_container.styles.border_bottom[0], "solid"
                        )
                        self.assertEqual(prompt_container.styles.border_left[0], "")
                        self.assertEqual(conversation.prompt.styles.padding.bottom, 0)
                        self.assertEqual(prompt_container.styles.margin.bottom, 0)
                        self.assertEqual(info_container.styles.margin.top, 0)
                        self.assertEqual(info_container.styles.margin.bottom, 0)
                        self.assertEqual(
                            conversation.prompt.prompt_text_area.styles.padding.right,
                            1,
                        )

                        conversation.prompt.text = "\n".join(
                            f"line {index}" for index in range(10)
                        )
                        await pilot.pause()
                        self.assertLessEqual(
                            conversation.prompt.prompt_text_area.size.height, 3
                        )

        asyncio.run(scenario())

    def test_conversation_scrollbar_uses_one_terminal_cell(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)

                        self.assertEqual(
                            conversation.window.styles.scrollbar_size_vertical,
                            1,
                        )

        asyncio.run(scenario())

    def test_tab_advances_the_open_slash_command_popup(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        prompt = conversation.prompt
                        prompt.slash_commands = [
                            SlashCommand("/alpha", "First command"),
                            SlashCommand("/bravo", "Second command"),
                        ]
                        prompt.text = "/"
                        prompt.show_slash_complete = True
                        await pilot.pause()

                        popup = prompt.slash_complete
                        self.assertTrue(popup.input.has_focus)
                        self.assertEqual(popup.option_list.highlighted, 0)

                        await pilot.press("tab")

                        self.assertTrue(prompt.show_slash_complete)
                        self.assertTrue(popup.input.has_focus)
                        self.assertEqual(popup.option_list.highlighted, 1)
                        self.assertEqual(prompt.text, "/bravo")

        asyncio.run(scenario())

    def test_enter_executes_an_argument_free_slash_command_from_the_popup(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        prompt = conversation.prompt
                        prompt.agent_ready = False
                        prompt.slash_commands = [
                            SlashCommand("/config", "Configure CodeSwarm preferences")
                        ]
                        prompt.text = "/"
                        prompt.show_slash_complete = True
                        await pilot.pause()

                        await pilot.press("enter")
                        await pilot.pause(0.1)

                        self.assertIsInstance(pilot.app.screen, ConfigScreen)
                        self.assertEqual(prompt.text, "")

        asyncio.run(scenario())

    def test_config_command_opens_while_agent_is_loading(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        conversation.agent_ready = False
                        conversation.prompt.text = "/config"

                        conversation.prompt.prompt_text_area.action_submit()
                        await pilot.pause(0.1)

                        self.assertIsInstance(pilot.app.screen, ConfigScreen)

        asyncio.run(scenario())

    def test_conversation_notifications_use_a_spaced_full_width_flash_ribbon(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        await pilot.pause()
                        pilot.app._notifications.clear()

                        conversation.notify(
                            "Mode could not be synchronized",
                            title="Set Mode",
                            severity="error",
                        )
                        await pilot.pause()

                        ribbon = conversation.query_one(Flash)
                        self.assertTrue(ribbon.visible)
                        self.assertEqual(
                            ribbon.render().plain,
                            "Set Mode: Mode could not be synchronized",
                        )
                        self.assertTrue(ribbon.has_class("-error"))
                        self.assertEqual(
                            ribbon.styles.background.rgb,
                            Color.parse(pilot.app.current_theme.primary).rgb,
                        )
                        self.assertEqual(ribbon.styles.background.a, 0.18)
                        self.assertEqual(
                            ribbon.outer_size.width, conversation.content_region.width
                        )
                        self.assertEqual(
                            conversation.prompt.region.y - ribbon.region.bottom,
                            1,
                        )
                        self.assertEqual(len(pilot.app._notifications), 0)

        asyncio.run(scenario())

    def test_local_shell_command_smoke(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        await conversation.run_local_shell.__wrapped__(
                            conversation, "printf codeswarm-shell-smoke"
                        )

                        terminal = conversation.query(TerminalTool).last()
                        self.assertEqual(terminal.return_code, 0)
                        self.assertIn(
                            "codeswarm-shell-smoke", terminal.tool_state.output
                        )
                        self.assertFalse(conversation._local_shells)

        asyncio.run(scenario())

    def test_flash_severities_share_the_teal_hud_palette(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        ribbon = conversation.query_one(Flash)
                        primary = Color.parse(pilot.app.current_theme.primary).rgb

                        for style in ("success", "warning", "error"):
                            conversation.flash("HUD status", style=style)
                            await pilot.pause()
                            self.assertEqual(ribbon.styles.background.rgb, primary)
                            self.assertEqual(ribbon.styles.color.rgb, primary)

        asyncio.run(scenario())

    def test_active_relay_agent_gets_a_distinct_response_tint(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation._active_relay_agent = gemini  # type: ignore[assignment]

                        response = await conversation.post_agent_response("Done")
                        await pilot.pause()

                        self.assertIsNotNone(response)
                        assert response is not None
                        self.assertEqual(
                            response.parent.region.x,
                            conversation.window.region.x,
                        )
                        self.assertTrue(response.has_class("-agent-tone-1"))
                        self.assertEqual(response.styles.padding.top, 0)
                        self.assertEqual(response.styles.padding.left, 0)
                        self.assertEqual(response.styles.padding.right, 0)
                        self.assertEqual(response.styles.padding.bottom, 0)
                        self.assertEqual(response.parent.styles.padding.top, 0)
                        self.assertEqual(response.parent.styles.padding.left, 1)
                        self.assertEqual(response.parent.styles.padding.right, 1)
                        self.assertEqual(response.parent.styles.padding.bottom, 0)
                        self.assertEqual(response.parent.styles.border_left[0], "vkey")
                        self.assertEqual(response.parent.styles.border_bottom[0], "")
                        self.assertEqual(response.parent.styles.margin.top, 0)
                        self.assertEqual(response.parent.styles.margin.bottom, 1)

        asyncio.run(scenario())

    def test_agent_header_is_colored_and_timestamp_stays_stable_while_streaming(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        gemini = _RosterAgent("Gemini CLI")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini CLI",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(gemini)  # type: ignore[arg-type]

                        with patch(
                            "codeswarm.widgets.conversation.format_reply_timestamp",
                            return_value="5:42 PM",
                            create=True,
                        ) as format_timestamp:
                            response = await conversation.post_agent_response("First")
                            streamed = await conversation.post_agent_response(" chunk")
                        await pilot.pause()

                        self.assertIs(response, streamed)
                        assert response is not None
                        header = response.parent.query_one_optional(
                            "#agent-message-header"
                        )
                        self.assertIsNotNone(header)
                        assert header is not None
                        header_content = header.render()
                        self.assertEqual(
                            header.content_region.x,
                            response.content_region.x,
                        )
                        self.assertEqual(
                            header_content.plain,
                            "Gemini CLI · 5:42 PM",
                        )
                        self.assertEqual(
                            [span.style for span in header_content.spans],
                            ["$text-primary bold", "dim"],
                        )
                        format_timestamp.assert_called_once()

        asyncio.run(scenario())

    def test_adjacent_messages_from_the_same_agent_form_one_visual_stack(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        gemini = _RosterAgent("Gemini CLI")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini CLI",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            )
                        ]

                        conversation.begin_agent_output(gemini)  # type: ignore[arg-type]
                        await conversation.post_agent_response("First message")
                        conversation.new_block()
                        await conversation.post_agent_response("Second message")
                        await pilot.pause(0.1)

                        messages = list(conversation.query(AgentMessage))
                        self.assertEqual(len(messages), 2)
                        first, continuation = messages
                        self.assertTrue(first.has_class("-continues"))
                        self.assertTrue(continuation.has_class("-continuation"))
                        self.assertFalse(
                            continuation.query_one("#agent-message-header").display
                        )
                        self.assertEqual(first.styles.border_bottom[0], "")
                        self.assertEqual(
                            continuation.region.y - first.region.bottom,
                            0,
                        )

        asyncio.run(scenario())

    def test_focusing_agent_thought_does_not_change_panel_border_geometry(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        thought = await conversation.post_agent_thought(
                            "Inspecting the flight controls"
                        )
                        self.assertIsNotNone(thought)
                        assert thought is not None
                        await pilot.pause(0.1)
                        outer_size = thought.outer_size

                        thought.focus()
                        await pilot.pause()

                        focused = conversation.query_one(AgentThought)
                        self.assertTrue(focused.has_focus)
                        self.assertEqual(focused.styles.border_top[0], "")
                        self.assertEqual(focused.styles.border_right[0], "")
                        self.assertEqual(focused.styles.border_bottom[0], "solid")
                        self.assertEqual(focused.styles.border_left[0], "solid")
                        self.assertEqual(focused.outer_size, outer_size)

        asyncio.run(scenario())

    def test_clicking_an_agent_reply_selects_the_clicked_markdown_block(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        response = await conversation.post_agent_response(
                            "First paragraph\n\nSecond paragraph"
                        )
                        await pilot.pause(0.1)

                        self.assertIsNotNone(response)
                        assert response is not None
                        clicked_block = response.children[-1]
                        event = Mock(widget=clicked_block)

                        with patch.object(
                            conversation.screen,
                            "get_selected_text",
                            return_value="",
                        ):
                            conversation.on_click(event)

                        self.assertIs(
                            conversation.cursor_block_child,
                            clicked_block,
                        )
                        self.assertIsNone(
                            conversation.query_one_optional("#cursor-container")
                        )

        asyncio.run(scenario())

    def test_clicking_agent_response_background_does_not_select_the_container_as_a_child(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        response = await conversation.post_agent_response(
                            "First paragraph\n\nSecond paragraph"
                        )
                        await pilot.pause(0.1)

                        self.assertIsNotNone(response)
                        assert response is not None
                        event = Mock(widget=response)
                        with patch.object(
                            conversation.screen,
                            "get_selected_text",
                            return_value="",
                        ):
                            conversation.on_click(event)

                        self.assertIs(conversation.cursor_block, response.parent)
                        self.assertIsNone(response.get_cursor_block())

        asyncio.run(scenario())

    def test_clicking_an_agent_header_does_not_select_response_content(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        response = await conversation.post_agent_response("A reply")
                        await pilot.pause(0.1)

                        self.assertIsNotNone(response)
                        assert response is not None
                        header = response.parent.query_one("#agent-message-header")
                        event = Mock(widget=header)
                        with patch.object(
                            conversation.screen,
                            "get_selected_text",
                            return_value="",
                        ):
                            conversation.on_click(event)

                        self.assertIs(conversation.cursor_block, response.parent)
                        self.assertIsNone(response.get_cursor_block())

        asyncio.run(scenario())

    def test_tool_activity_and_reply_share_one_attributed_agent_turn(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        tool_message = acp_messages.ToolCall(
                            {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "read-conversation",
                                "status": "in_progress",
                                "title": "Read conversation.py",
                            }
                        )
                        tool_message.agent = claude  # type: ignore[attr-defined,assignment]

                        conversation.post_message(tool_message)
                        await pilot.pause(0.1)
                        conversation.post_message(
                            acp_messages.Update(
                                "text", "I found it.", claude  # type: ignore[arg-type]
                            )
                        )
                        await pilot.pause(0.1)

                        turns = list(conversation.query(AgentMessage))
                        self.assertEqual(len(turns), 1)
                        turn = turns[0]
                        self.assertIs(turn.query_one(ToolCall).parent.parent, turn)
                        self.assertIs(turn.query_one(AgentResponse).parent, turn)
                        self.assertGreater(
                            list(turn.children).index(turn.query_one(ToolCall).parent),
                            list(turn.children).index(turn.query_one(AgentResponse)),
                        )

        asyncio.run(scenario())

    def test_thinking_only_output_has_an_attributed_thinking_header(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai",
                            name="Claude",
                            short_name="claude",
                        ),
                        claude,  # type: ignore[arg-type]
                    )
                ]
                thinking = acp_messages.Thinking(
                    "text", "Inspecting the workspace", claude  # type: ignore[arg-type]
                )
                conversation.post_message(thinking)
                await pilot.pause(0.1)

                turn = conversation.query_one(AgentMessage)
                header = turn.query_one("#agent-message-header")
                self.assertTrue(header.display)
                self.assertIn("Claude", str(header.render()))
                self.assertIn("Thinking", str(header.render()))
                self.assertIsNotNone(turn.query_one(AgentThought))

                conversation.post_message(
                    acp_messages.Update("text", "Done", claude)  # type: ignore[arg-type]
                )
                await pilot.pause(0.1)
                self.assertNotIn("Thinking", str(header.render()))

        asyncio.run(scenario())

    def test_thinking_and_tools_share_one_attributed_turn(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai",
                            name="Claude",
                            short_name="claude",
                        ),
                        claude,  # type: ignore[arg-type]
                    )
                ]

                conversation.post_message(
                    acp_messages.Thinking(
                        "text", "Planning the next step", claude  # type: ignore[arg-type]
                    )
                )
                await pilot.pause(0.1)
                tool_message = acp_messages.ToolCall(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "inspect-workspace",
                        "status": "in_progress",
                        "title": "Inspect workspace",
                    },
                    claude,  # type: ignore[arg-type]
                )
                conversation.post_message(tool_message)
                await pilot.pause(0.1)

                turn = conversation.query_one(AgentMessage)
                self.assertIsNotNone(turn.query_one(AgentThought))
                self.assertIsNotNone(turn.query_one(AgentToolActivity))
                self.assertIsNotNone(turn.query_one(ToolCall))
                self.assertIn(
                    "Claude",
                    str(turn.query_one("#agent-message-header").render()),
                )

        asyncio.run(scenario())

    def test_export_writes_only_user_and_agent_conversation_markdown(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as export_dir:
                async with CodeSwarmApp(setup_prompt=False).run_test(
                    size=(120, 40)
                ) as pilot:
                    conversation = pilot.app.screen.query_one(Conversation)
                    conversation.project_path = Path(export_dir)
                    claude = _RosterAgent("Claude")
                    conversation.session.roster = [
                        RosterEntry(
                            AgentData(
                                identity="claude.ai",
                                name="Claude",
                                short_name="claude",
                            ),
                            claude,  # type: ignore[arg-type]
                        )
                    ]
                    await conversation.post(UserInput("What changed?"))
                    conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                    await conversation.post_agent_thought(
                        "Inspecting files", claude  # type: ignore[arg-type]
                    )
                    await conversation.post_agent_response(
                        "The launch flow now restores the saved roster."
                    )
                    await pilot.pause(0.1)

                    handled = await conversation.slash_command("/export")
                    self.assertTrue(handled)
                    exports = list(Path(export_dir).glob("codeswarm-conversation-*.md"))
                    self.assertEqual(len(exports), 1)
                    content = exports[0].read_text()
                    self.assertIn("What changed?", content)
                    self.assertIn("The launch flow now restores the saved roster.", content)
                    self.assertNotIn("Inspecting files", content)
                    self.assertNotIn("Thinking", content)

        asyncio.run(scenario())

    def test_streamed_agent_output_keeps_the_view_at_the_bottom(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(80, 12)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        agent = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                agent,  # type: ignore[arg-type]
                            )
                        ]
                        for index in range(12):
                            await conversation.post(Note(f"History {index}"))
                        await pilot.pause(0.1)
                        conversation.window.scroll_end(animate=False)
                        self.assertTrue(conversation.window.is_vertical_scroll_end)

                        conversation.begin_agent_output(agent)  # type: ignore[arg-type]
                        await conversation.post_agent_response("New streamed output")
                        await pilot.pause(0.1)

                        self.assertTrue(conversation.window.is_vertical_scroll_end)

        asyncio.run(scenario())

    def test_repeated_streamed_fragments_keep_following_the_bottom(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(80, 12)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                for index in range(40):
                    await conversation.post(Note(f"History {index}"))
                await pilot.pause(0.1)
                conversation.window.scroll_end(animate=False)

                conversation.begin_agent_output(None)
                for _ in range(80):
                    await conversation.post_agent_response("streamed output " * 4)
                    await pilot.pause()

                await pilot.pause(0.1)
                self.assertTrue(conversation.window.is_vertical_scroll_end)

        asyncio.run(scenario())

    def test_scrolling_back_to_bottom_resumes_stream_following(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(80, 12)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                for index in range(40):
                    await conversation.post(Note(f"History {index}"))
                await pilot.pause(0.1)
                conversation.window.scroll_home(animate=False)
                await pilot.pause()
                self.assertFalse(conversation.window.is_vertical_scroll_end)
                self.assertFalse(conversation.window.follow_output)

                conversation.window.scroll_end(animate=False, immediate=True)
                self.assertTrue(conversation.window.is_vertical_scroll_end)
                self.assertTrue(conversation.window.follow_output)
                conversation.begin_agent_output(None)
                for _ in range(80):
                    await conversation.post_agent_response("resumed output " * 4)
                    await pilot.pause()

                await pilot.pause(0.1)
                self.assertTrue(conversation.window.is_vertical_scroll_end)

        asyncio.run(scenario())

    def test_tool_history_aligns_with_its_agent_response_content(self) -> None:
        """Tool history should not add an indent inside an agent message."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        tool_message = acp_messages.ToolCall(
                            {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "read-conversation",
                                "status": "in_progress",
                                "title": "Read conversation.py",
                            }
                        )
                        tool_message.agent = claude  # type: ignore[attr-defined,assignment]

                        conversation.post_message(tool_message)
                        await pilot.pause(0.1)

                        turn = conversation.query_one(AgentMessage)
                        summary = turn.query_one("#tool-activity-summary")
                        self.assertEqual(summary.region.x, turn.content_region.x)

                        activity = turn.query_one(AgentToolActivity)
                        activity.focus()
                        await pilot.pause()
                        tool = turn.query_one(ToolCall)
                        self.assertEqual(tool.region.x, turn.content_region.x)

        asyncio.run(scenario())

    def test_tool_activity_shows_one_line_and_browses_history_when_focused(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]

                        for tool_id, title in (
                            ("read-file", "Read conversation.py"),
                            ("run-tests", "Run focused tests"),
                        ):
                            tool_message = acp_messages.ToolCall(
                                {
                                    "sessionUpdate": "tool_call",
                                    "toolCallId": tool_id,
                                    "status": "completed",
                                    "title": title,
                                }
                            )
                            tool_message.agent = claude  # type: ignore[attr-defined,assignment]
                            conversation.post_message(tool_message)
                            await pilot.pause(0.1)

                        activity = conversation.query_one(AgentToolActivity)
                        first, second = list(activity.query(ToolCall))
                        self.assertFalse(first.display)
                        self.assertFalse(second.display)
                        self.assertTrue(activity.summary.display)
                        self.assertEqual(
                            activity.summary.render().plain,
                            "🔧 Run focused tests · 2 tools",
                        )

                        activity.focus()
                        await pilot.press("up")

                        self.assertTrue(first.display)
                        self.assertFalse(second.display)

        asyncio.run(scenario())

    def test_tool_preview_treats_agent_text_as_literal_markup(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai", name="Claude", short_name="claude"
                        ),
                        claude,  # type: ignore[arg-type]
                    )
                ]
                tool_message = acp_messages.ToolCall(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "literal-markup",
                        "status": "in_progress",
                        "title": "[red]{+}1",
                    },
                    claude,  # type: ignore[arg-type]
                )
                conversation.post_message(tool_message)
                await pilot.pause(0.1)

                summary = conversation.query_one("#tool-activity-summary")
                self.assertIn("[red]{+}1", summary.render().plain)

        asyncio.run(scenario())

    def test_new_tool_is_hidden_before_mount_to_avoid_activity_reflow(self) -> None:
        class MountProbeToolCall(ToolCall):
            display_when_mounted: bool | None = None

            def on_mount(self) -> None:
                self.display_when_mounted = self.display
                super().on_mount()

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        agent_message = await conversation.ensure_agent_message(claude)  # type: ignore[arg-type]
                        tool = MountProbeToolCall(
                            {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "run-tests",
                                "status": "in_progress",
                                "title": "Run focused tests",
                                "rawInput": {"command": "pytest tests/test_agy.py"},
                            }
                        )

                        await agent_message.tool_activity.add_tool_call(tool)

                        self.assertFalse(tool.display_when_mounted)
                        self.assertTrue(agent_message.tool_activity.summary.display)
                        self.assertIn(
                            "Run focused tests",
                            agent_message.tool_activity.summary.render().plain,
                        )
                        self.assertIn(
                            "pytest tests/test_agy.py",
                            agent_message.tool_activity.summary.render().plain,
                        )
                        await pilot.pause()
                        self.assertEqual(agent_message.tool_activity.outer_size.height, 1)

        asyncio.run(scenario())

    def test_clicking_tool_activity_focuses_it_and_enter_toggles_details(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        tool_message = acp_messages.ToolCall(
                            {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "inspect-result",
                                "status": "completed",
                                "title": "Inspect result",
                                "content": [
                                    {
                                        "type": "content",
                                        "content": {
                                            "type": "text",
                                            "text": "Tool details",
                                        },
                                    }
                                ],
                            }
                        )
                        tool_message.agent = claude  # type: ignore[attr-defined,assignment]
                        conversation.post_message(tool_message)
                        await pilot.pause(0.1)

                        activity = conversation.query_one(AgentToolActivity)
                        tool = activity.query_one(ToolCall)
                        click = Mock()
                        tool.on_click_tool_call_header(click)
                        await pilot.pause()

                        self.assertTrue(activity.has_focus)
                        tool.expanded = False
                        await pilot.press("enter")
                        self.assertTrue(tool.expanded)

        asyncio.run(scenario())

    def test_completed_tool_activity_collapses_to_count_and_duration(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]

                        clock = Mock(return_value=100.0)
                        with patch("codeswarm.widgets.conversation.monotonic", clock):
                            conversation._begin_agent_status(claude)  # type: ignore[arg-type]
                            for tool_id in ("read-file", "run-tests"):
                                tool_message = acp_messages.ToolCall(
                                    {
                                        "sessionUpdate": "tool_call",
                                        "toolCallId": tool_id,
                                        "status": "completed",
                                        "title": tool_id.replace("-", " ").title(),
                                    }
                                )
                                tool_message.agent = claude  # type: ignore[attr-defined,assignment]
                                conversation.post_message(tool_message)
                                await pilot.pause(0.1)
                            clock.return_value = 114.0
                            conversation._finish_agent_status(claude)  # type: ignore[arg-type]

                        activity = conversation.query_one(AgentToolActivity)
                        summary = activity.query_one_optional(
                            "#tool-activity-summary"
                        )
                        self.assertIsNotNone(summary)
                        assert summary is not None
                        self.assertEqual(
                            summary.render().plain,
                            "✓ Run Tests · 2 tools · 14s",
                        )
                        self.assertTrue(summary.display)
                        self.assertTrue(
                            all(not tool.display for tool in activity.query(ToolCall))
                        )

                        click_handler = getattr(
                            activity,
                            "on_click",
                            lambda _event: None,
                        )
                        click_handler(Mock())
                        await pilot.pause()
                        self.assertTrue(activity.has_focus)
                        self.assertFalse(summary.display)
                        self.assertTrue(activity.query(ToolCall).last().display)

        asyncio.run(scenario())

    def test_roster_palette_colors_each_agent_reply_surface_and_border(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        agents = [_RosterAgent(f"Agent {index}") for index in range(4)]
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity=f"agent-{index}.test",
                                    name=agent.name,
                                    short_name=f"agent-{index}",
                                ),
                                agent,  # type: ignore[arg-type]
                            )
                            for index, agent in enumerate(agents)
                        ]

                        responses = []
                        for agent in agents:
                            conversation.begin_agent_output(agent)  # type: ignore[arg-type]
                            response = await conversation.post_agent_response("Reply")
                            assert response is not None
                            responses.append(response)
                        await pilot.pause(0.1)

                        self.assertEqual(
                            len(
                                {
                                    (
                                        response.parent.styles.background.rgb,
                                        response.parent.styles.background.a,
                                    )
                                    for response in responses
                                }
                            ),
                            4,
                        )
                        headers = [
                            response.parent.query_one("#agent-message-header")
                            for response in responses
                        ]
                        expected_colors = (
                            "#67E8F9",
                            "#A78BFA",
                            "#22D3EE",
                            "#FBBF24",
                        )
                        for index, (header, response, expected_color) in enumerate(
                            zip(headers, responses, expected_colors)
                        ):
                            self.assertTrue(
                                response.parent.has_class(f"-agent-tone-{index}")
                            )
                            self.assertEqual(
                                response.parent.styles.border_left[0],
                                "vkey",
                            )
                            self.assertEqual(
                                response.parent.styles.border_left[1].rgb,
                                Color.parse(expected_color).rgb,
                            )
                            self.assertEqual(
                                response.parent.styles.border_left[1].a,
                                0.25,
                            )
                            self.assertEqual(
                                response.parent.styles.border_bottom[0],
                                "",
                            )
                        self.assertEqual(
                            [
                                header.render().spans[0].style
                                for header in headers
                            ],
                            ["$text-primary bold"] * 4,
                        )

        asyncio.run(scenario())

    def test_agent_message_content_uses_a_vivid_format_palette(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        agent = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                agent,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(agent)  # type: ignore[arg-type]

                        response = await conversation.post_agent_response(
                            "# Heading\n\nBody with `codeswarm`, "
                            "src/codeswarm/app.py, and [the docs](https://example.com/docs).\n\n"
                            "> Quoted detail\n\n---\n\n"
                            "```python\nvalue = 'accent-free'\n```\n\n"
                            "| Name | Value |\n| --- | --- |\n| mode | normal |"
                        )
                        await pilot.pause()

                        assert response is not None
                        paragraph = response.query_one(MarkdownParagraph)
                        body_color = paragraph.styles.color
                        heading = response.query_one(MarkdownH1)
                        rule = response.query_one(MarkdownHorizontalRule)
                        fence = response.query_one(MarkdownFence)
                        table = response.query_one(MarkdownTable)
                        table_header = response.query_one(MarkdownTableContent).query_one(
                            ".header"
                        )
                        quote = response.query_one(MarkdownBlockQuote)
                        inline_code = paragraph.get_component_rich_style(
                            "code_inline"
                        )
                        assert inline_code.color is not None
                        assert inline_code.bgcolor is not None
                        inline_color = inline_code.color.get_truecolor()
                        inline_background = inline_code.bgcolor.get_truecolor()
                        inline_rgb = (
                            inline_color.red,
                            inline_color.green,
                            inline_color.blue,
                        )
                        inline_background_rgb = (
                            inline_background.red,
                            inline_background.green,
                            inline_background.blue,
                        )
                        self.assertEqual(inline_rgb, Color.parse("#7DD3FC").rgb)
                        self.assertEqual(
                            inline_background_rgb, Color.parse("#0B2233").rgb
                        )
                        file_reference = paragraph.get_component_rich_style(
                            "file_reference"
                        )
                        self.assertEqual(
                            (
                                file_reference.color.get_truecolor().red,
                                file_reference.color.get_truecolor().green,
                                file_reference.color.get_truecolor().blue,
                            ),
                            Color.parse("#F0ABFC").rgb,
                        )
                        self.assertEqual(
                            (
                                file_reference.bgcolor.get_truecolor().red,
                                file_reference.bgcolor.get_truecolor().green,
                                file_reference.bgcolor.get_truecolor().blue,
                            ),
                            Color.parse("#2D1B3B").rgb,
                        )
                        self.assertTrue(
                            any(
                                span.style == ".file_reference"
                                for span in paragraph._content.spans
                            )
                        )
                        self.assertEqual(
                            paragraph.styles.link_color.rgb,
                            Color.parse("#67E8F9").rgb,
                        )
                        self.assertTrue(paragraph.styles.link_style.underline)
                        format_colors = {
                            "inline code": inline_rgb,
                            "heading": heading.styles.color.rgb,
                            "divider": rule.styles.border_bottom[1].rgb,
                            "code fence": fence.styles.color.rgb,
                            "table": table.styles.color.rgb,
                        }

                        self.assertEqual(
                            fence.styles.color.rgb, Color.parse("#C4D7ED").rgb
                        )
                        self.assertEqual(
                            fence.styles.background.rgb,
                            Color.parse("#0D1B2A").rgb,
                        )
                        self.assertEqual(
                            table_header.styles.color.rgb,
                            Color.parse("#FBBF24").rgb,
                        )
                        for format_name, color in format_colors.items():
                            with self.subTest(format=format_name):
                                self.assertNotEqual(color, body_color.rgb)
                        self.assertEqual(
                            heading.styles.color.rgb,
                            Color.parse("#FDE68A").rgb,
                        )
                        self.assertLess(sum(fence.styles.color.rgb), sum(body_color.rgb))
                        self.assertLess(
                            sum(inline_background_rgb), sum(inline_rgb)
                        )
                        self.assertNotEqual(inline_background_rgb[0], inline_background_rgb[1])
                        self.assertEqual(heading.styles.background.a, 0)
                        self.assertFalse(fence._content.spans)

                        self.assertNotEqual(
                            quote.styles.border_left[1].rgb, body_color.rgb
                        )
                        self.assertEqual(quote.styles.color.rgb, Color.parse("#A7F3D0").rgb)
                        self.assertEqual(
                            quote.styles.border_left[1].rgb,
                            Color.parse("#2DD4BF").rgb,
                        )
                        self.assertEqual(
                            response.parent.styles.border_left[1].rgb,
                            Color.parse("#67E8F9").rgb,
                        )
                        header = response.parent.query_one("#agent-message-header")
                        self.assertEqual(
                            [span.style for span in header.render().spans],
                            ["$text-primary bold", "dim"],
                        )

        asyncio.run(scenario())

    def test_agent_reply_interiors_share_one_style_across_agents(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        claude_response = await conversation.post_agent_response(
                            "# Heading\n\n> Quoted detail\n\n---\n\nBody"
                        )
                        conversation.begin_agent_output(gemini)  # type: ignore[arg-type]
                        gemini_response = await conversation.post_agent_response(
                            "# Heading\n\n> Quoted detail\n\n---\n\nBody"
                        )
                        assert claude_response is not None
                        assert gemini_response is not None
                        await pilot.pause(0.1)

                        for selector, style_name in (
                            (MarkdownH1, "color"),
                            (MarkdownBlockQuote, "border_left"),
                            (MarkdownHorizontalRule, "border_bottom"),
                        ):
                            claude_style = getattr(
                                claude_response.query_one(selector).styles,
                                style_name,
                            )
                            gemini_style = getattr(
                                gemini_response.query_one(selector).styles,
                                style_name,
                            )
                            self.assertEqual(claude_style, gemini_style)

                        self.assertNotEqual(
                            claude_response.parent.styles.border_left[1].rgb,
                            gemini_response.parent.styles.border_left[1].rgb,
                        )

        asyncio.run(scenario())

    def test_agent_reply_horizontal_rules_are_compact(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(80, 20)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        response = await conversation.post_agent_response(
                            "Before\n\n---\n\nAfter"
                        )
                        assert response is not None
                        await pilot.pause(0.1)

                        rule = response.query_one(MarkdownHorizontalRule)
                        self.assertFalse(rule.display)
                        self.assertEqual(rule.styles.padding.top, 0)
                        self.assertEqual(rule.styles.margin.bottom, 0)

        asyncio.run(scenario())

    def test_agent_reply_blocks_use_compact_internal_spacing(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(80, 20)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        agent = _RosterAgent("Claude")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                agent,  # type: ignore[arg-type]
                            )
                        ]
                        conversation.begin_agent_output(agent)  # type: ignore[arg-type]
                        response = await conversation.post_agent_response(
                            "First\n\nSecond\n\n```text\ncode\n```\n\n> Quote"
                        )
                        assert response is not None
                        await pilot.pause(0.1)

                        blocks = list(response.children)
                        self.assertGreater(len(blocks), 2)
                        for block in blocks:
                            with self.subTest(block=type(block).__name__):
                                self.assertEqual(block.styles.margin.top, 0)
                                self.assertEqual(block.styles.margin.bottom, 0)
                        self.assertGreater(response.parent.styles.margin.bottom, 0)

        asyncio.run(scenario())

    def test_response_tint_uses_chunk_owner_after_relay_advances(self) -> None:
        """Queued ACP output keeps its source color after the next turn starts."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]

                        # Claude's chunk is queued, but the relay has already
                        # advanced its mutable current-speaker state to Gemini.
                        conversation.begin_agent_output(claude)  # type: ignore[arg-type]
                        conversation._active_relay_agent = gemini  # type: ignore[assignment]
                        response = await conversation.post_agent_response(
                            "Claude's delayed final chunk"
                        )

                        self.assertIsNotNone(response)
                        assert response is not None
                        self.assertTrue(response.has_class("-agent-tone-0"))
                        self.assertFalse(response.has_class("-agent-tone-1"))

        asyncio.run(scenario())

    def test_each_relay_agent_gets_a_separate_response_block(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.com",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="geminicli.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]

                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        conversation.post_message(
                            acp_messages.Update(
                                "text", "Claude answer", claude  # type: ignore[arg-type]
                            )
                        )
                        await pilot.pause(0.1)

                        conversation._active_relay_agent = gemini  # type: ignore[assignment]
                        conversation.post_message(
                            acp_messages.Update(
                                "text", "Gemini answer", gemini  # type: ignore[arg-type]
                            )
                        )
                        await pilot.pause(0.1)

                        responses = list(conversation.query(AgentResponse))
                        self.assertEqual(len(responses), 2)
                        self.assertTrue(responses[0].has_class("-agent-tone-0"))
                        self.assertTrue(responses[1].has_class("-agent-tone-1"))

        asyncio.run(scenario())

    def test_only_current_local_commands_override_agent_command_collisions(self) -> None:
        conversation = Conversation(Path.cwd())
        conversation.agent_slash_commands = [
            SlashCommand("/about", "An agent-defined about command"),
            SlashCommand("/agent", "An agent-defined agent command"),
            SlashCommand("/clear", "An agent-defined clear command"),
            SlashCommand("/review", "Review the current change"),
        ]

        commands = {
            command.command: command for command in conversation._build_slash_commands()
        }

        self.assertEqual(commands["/about"].help, "An agent-defined about command")
        self.assertEqual(commands["/help"].help, "Show CodeSwarm commands")
        self.assertEqual(commands["/config"].help, "Configure CodeSwarm preferences")
        self.assertEqual(commands["/agent"].help, "An agent-defined agent command")
        self.assertEqual(commands["/close"].help, "Close the current session")
        self.assertEqual(commands["/clear"].help, "An agent-defined clear command")
        self.assertNotIn("/codeswarm:agent", commands)
        self.assertEqual(commands["/review"].help, "Review the current change")
        self.assertNotIn("/codeswarm:rename", commands)

    def test_user_manual_documents_every_codeswarm_command(self) -> None:
        manual = (
            Path(__file__).parents[1] / "docs" / "USER_MANUAL.md"
        ).read_text("utf-8")
        conversation = Conversation(Path.cwd())

        local_commands = {
            command.command for command in conversation._build_slash_commands()
        }
        local_commands.add("/pause")
        for command in local_commands:
            self.assertIn(f"`{command}", manual)
        self.assertNotIn("`/about`", manual)

    def test_about_is_not_a_local_codeswarm_command(self) -> None:
        conversation = Conversation(Path.cwd())
        conversation.agent_slash_commands = [
            SlashCommand("/about", "Agent-owned command")
        ]

        self.assertFalse(asyncio.run(conversation.slash_command("/about")))

    def test_removed_local_commands_can_be_owned_by_an_agent(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            conversation.agent_slash_commands = [
                SlashCommand("/agent", "Manage agent state"),
                SlashCommand("/clear", "Clear agent state"),
                SlashCommand("/codeswarm:agent", "Inspect agent state"),
                SlashCommand("/codeswarm:pause", "Pause agent work"),
                SlashCommand("/codeswarm:clear", "Clear agent state"),
                SlashCommand("/codeswarm:session-close", "Close agent session"),
            ]

            for command in conversation.agent_slash_commands:
                with self.subTest(command=command.command):
                    self.assertFalse(
                        await conversation.slash_command(f"{command.command} now")
                    )

        asyncio.run(scenario())

    def test_unknown_slash_command_is_not_routed_to_an_agent(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            with patch.object(conversation, "flash") as flash:
                self.assertTrue(await conversation.slash_command("/cler"))
            flash.assert_called_once()
            self.assertIn("Unknown command", flash.call_args.args[0])

            conversation.agent_slash_commands = [
                SlashCommand("/review", "Review the current change")
            ]
            self.assertFalse(await conversation.slash_command("/review staged"))

        asyncio.run(scenario())

    def test_advertised_slash_command_targets_its_owner(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )
                        conversation._mode_agent = gemini  # type: ignore[assignment]
                        conversation.agent_slash_commands = [
                            SlashCommand("/review", "Review the current change")
                        ]

                        with patch.object(
                            conversation, "send_direct_prompt_to_agent"
                        ) as send:
                            await conversation.on_user_input_submitted(
                                messages.UserInputSubmitted("/review staged")
                            )

                        send.assert_called_once_with(1, "/review staged")

        asyncio.run(scenario())

    def test_clicking_a_roster_agent_selects_the_next_recipient(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )
                        conversation.agent_ready = True
                        conversation._ready_agents = {id(claude), id(gemini)}
                        conversation._refresh_roster_info()
                        await pilot.pause()

                        await pilot.click(AgentInfo, offset=(14, 0))

                        self.assertEqual(conversation.session.first_agent, 1)
                        self.assertTrue(
                            conversation.agent_info.plain.startswith("○ Claude · → ○ Gemini")
                        )

        asyncio.run(scenario())

    def test_manual_mode_pins_until_the_user_clicks_another_agent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )
                        conversation.agent_ready = True
                        conversation._ready_agents = {id(claude), id(gemini)}

                        await conversation.slash_command("/collab manual")
                        await pilot.pause()
                        self.assertEqual(conversation.session.collaboration_mode, "manual")
                        self.assertEqual(
                            pilot.app.screen.query_one(CollaborationInfo).render().plain,
                            "Manual",
                        )
                        self.assertIn("⌖ Claude", conversation.agent_info.plain)

                        await pilot.click(AgentInfo, offset=(14, 0))

                        self.assertEqual(
                            conversation.session.relay.pinned_agent_index,  # type: ignore[union-attr]
                            1,
                        )
                        self.assertIn("⌖ Gemini", conversation.agent_info.plain)

        asyncio.run(scenario())

    def test_pair_mode_reports_the_doer_as_the_next_recipient(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                gemini = _RosterAgent("Gemini")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai",
                            name="Claude",
                            short_name="claude",
                        ),
                        claude,  # type: ignore[arg-type]
                    ),
                    RosterEntry(
                        AgentData(
                            identity="gemini.google.com",
                            name="Gemini",
                            short_name="gemini",
                        ),
                        gemini,  # type: ignore[arg-type]
                    ),
                ]
                conversation.session._build_relay(
                    on_turn_start=None, on_turn=None
                )
                assert conversation.session.relay is not None
                conversation.session.relay.next_agent_index = 1

                await conversation.slash_command("/collab pair")
                await pilot.pause()

                self.assertEqual(conversation.session.collaboration_mode, "pair")
                self.assertEqual(
                    pilot.app.screen.query_one(CollaborationInfo).render().plain,
                    "Pair",
                )
                self.assertIs(conversation._routing_agent(), claude)

        asyncio.run(scenario())

    def test_batch_summary_text_aligns_with_agent_headers(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                agent = _RosterAgent("Claude")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai",
                            name="Claude",
                            short_name="claude",
                        ),
                        agent,  # type: ignore[arg-type]
                    )
                ]
                conversation.begin_agent_output(agent)  # type: ignore[arg-type]
                response = await conversation.post_agent_response("Reply")
                await conversation._post_collaboration_summary()
                await pilot.pause()

                assert response is not None
                summary = conversation.query(Note).last()
                header = response.parent.query_one("#agent-message-header")
                self.assertEqual(summary.region.x, response.parent.region.x)
                self.assertEqual(
                    summary.styles.content_align,
                    header.styles.content_align,
                )
                self.assertEqual(summary.styles.padding.left, response.parent.styles.padding.left)

        asyncio.run(scenario())

    def test_clicking_an_idle_agent_while_another_is_busy_keeps_the_selection(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )
                        conversation.agent_ready = True
                        conversation._ready_agents = {id(claude), id(gemini)}
                        conversation._working_agent = claude  # type: ignore[assignment]
                        conversation._active_relay_agent = claude  # type: ignore[assignment]
                        conversation._refresh_roster_info()
                        await pilot.pause()

                        await pilot.click(AgentInfo, offset=(17, 0))

                        self.assertEqual(conversation.session.first_agent, 1)
                        self.assertIn("● Claude", conversation.agent_info.plain)
                        self.assertIn("→ ○ Gemini", conversation.agent_info.plain)

        asyncio.run(scenario())

    def test_hash_prefixed_prompt_is_a_normal_message(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )

                        with patch.object(conversation, "send_prompt_to_agent") as send:
                            await conversation.on_user_input_submitted(
                                messages.UserInputSubmitted("#claude: inspect this")
                            )

                        send.assert_called_once_with("#claude: inspect this")

        asyncio.run(scenario())

    def test_pause_is_unavailable_without_a_relay(self) -> None:
        conversation = Conversation(Path.cwd())

        self.assertFalse(conversation.check_action("toggle_pause", ()))
        self.assertNotIn(
            "/pause",
            {command.command for command in conversation._build_slash_commands()},
        )

    def test_pause_command_is_available_for_a_relay(self) -> None:
        conversation = Conversation(Path.cwd())
        claude = _RosterAgent("Claude")
        gemini = _RosterAgent("Gemini")
        conversation.session.roster = [
            RosterEntry(
                AgentData(
                    identity="claude.ai", name="Claude", short_name="claude"
                ),
                claude,  # type: ignore[arg-type]
            ),
            RosterEntry(
                AgentData(
                    identity="gemini.google.com", name="Gemini", short_name="gemini"
                ),
                gemini,  # type: ignore[arg-type]
            ),
        ]
        conversation.session._build_relay(on_turn_start=None, on_turn=None)

        self.assertTrue(conversation.check_action("toggle_pause", ()))
        self.assertIn(
            "/pause",
            {command.command for command in conversation._build_slash_commands()},
        )

    def test_busy_follow_up_names_the_active_agent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_turn_start=None, on_turn=None
                        )
                        assert conversation.session.relay is not None
                        conversation.session.relay.last_active_index = 1
                        conversation._active_relay_agent = gemini  # type: ignore[assignment]
                        conversation.turn = "agent"

                        with patch.object(conversation, "flash") as flash:
                            await conversation.on_user_input_submitted(
                                messages.UserInputSubmitted(
                                    "use the existing parser"
                                )
                            )

                        flash.assert_not_called()
                        self.assertEqual(
                            conversation.queued_messages,
                            ("TX HOLD // Gemini · use the existing parser",),
                        )
                        self.assertEqual(list(conversation.query(UserInput)), [])

                        await conversation._label_queued_relay_turn_start(
                            2, gemini, "use the existing parser", False  # type: ignore[arg-type]
                        )
                        self.assertEqual(conversation.queued_messages, ())
                        self.assertEqual(
                            conversation.query(UserInput).last().content,
                            "use the existing parser",
                        )

        asyncio.run(scenario())

    def test_busy_follow_up_names_selected_next_agent(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                claude = _RosterAgent("Claude")
                gemini = _RosterAgent("Gemini")
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="claude.ai", name="Claude", short_name="claude"
                        ),
                        claude,  # type: ignore[arg-type]
                    ),
                    RosterEntry(
                        AgentData(
                            identity="gemini.google.com",
                            name="Gemini",
                            short_name="gemini",
                        ),
                        gemini,  # type: ignore[arg-type]
                    ),
                ]
                conversation.session._build_relay(on_turn_start=None, on_turn=None)
                conversation.session.select_agent(0)
                conversation._active_relay_agent = gemini  # type: ignore[assignment]
                conversation.turn = "agent"

                await conversation.on_user_input_submitted(
                    messages.UserInputSubmitted("send this to Claude")
                )

                self.assertEqual(
                    conversation.queued_messages,
                    ("TX HOLD // Claude · send this to Claude",),
                )

        asyncio.run(scenario())

    def test_queued_message_renders_user_markup_literally(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        conversation.queued_messages = (
                            "TX HOLD // Claude · [red]literal[/red]",
                        )
                        await pilot.pause()

                        queued = conversation.query_one(QueuedMessages)
                        self.assertEqual(
                            queued.query_one(Label).render().plain,
                            "TX HOLD // Claude · [red]literal[/red]",
                        )

        asyncio.run(scenario())

    def test_dropping_agent_removes_its_queued_message_preview(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.session._build_relay(
                            on_queued_turn_discarded=(
                                conversation._discard_queued_prompt
                            )
                        )
                        relay = conversation.session.relay
                        assert relay is not None
                        relay.last_active_index = 1

                        self.assertTrue(relay.enqueue_human("check the parser"))
                        conversation._add_queued_prompt(
                            "check the parser", False, "Queued for Gemini"
                        )
                        self.assertTrue(conversation.queued_messages)

                        await conversation.session.drop(1)

                        self.assertEqual(conversation.queued_messages, ())

        asyncio.run(scenario())

    def test_solo_follow_up_is_queued_until_the_current_turn_finishes(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)
                        conversation.agent = _RosterAgent("Claude")  # type: ignore[assignment]
                        conversation.agent_ready = True
                        conversation.turn = "agent"

                        await conversation.on_user_input_submitted(
                            messages.UserInputSubmitted("Follow up after this")
                        )
                        self.assertEqual(
                            list(conversation._pending_solo_prompts),
                            ["Follow up after this"],
                        )
                        self.assertEqual(
                            conversation.queued_messages,
                            ("TX HOLD // ACTIVE WINGMAN · Follow up after this",),
                        )

                        with patch.object(
                            conversation, "send_prompt_to_agent"
                        ) as send_prompt:
                            await conversation.agent_turn_over("end_turn")

                        send_prompt.assert_called_once_with("Follow up after this")
                        self.assertEqual(conversation.turn, "agent")
                        self.assertEqual(list(conversation._pending_solo_prompts), [])
                        self.assertEqual(conversation.queued_messages, ())
                        self.assertEqual(
                            conversation.query(UserInput).last().content,
                            "Follow up after this",
                        )

        asyncio.run(scenario())

    def test_cancel_button_removes_a_queued_solo_follow_up(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        conversation.agent = _RosterAgent("Claude")  # type: ignore[assignment]
                        conversation.agent_ready = True
                        conversation.turn = "agent"

                        await conversation.on_user_input_submitted(
                            messages.UserInputSubmitted("cancel this")
                        )
                        await conversation.on_user_input_submitted(
                            messages.UserInputSubmitted("keep this")
                        )
                        await pilot.pause()
                        await pilot.click("#queued-cancel-0")
                        await pilot.pause()

                        self.assertEqual(
                            list(conversation._pending_solo_prompts), ["keep this"]
                        )
                        self.assertEqual(
                            conversation.queued_messages,
                            ("TX HOLD // ACTIVE WINGMAN · keep this",),
                        )

        asyncio.run(scenario())

    def test_turn_completion_notification_has_no_sound(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        with patch.object(pilot.app, "system_notify") as notify:
                            await conversation.agent_turn_over("end_turn")

                        notify.assert_called_once()
                        self.assertEqual(
                            notify.call_args.args,
                            ("MISSION COMPLETE // Agent",),
                        )
                        self.assertEqual(
                            notify.call_args.kwargs["title"],
                            "AWAITING ORDERS",
                        )
                        self.assertNotIn("sound", notify.call_args.kwargs)

        asyncio.run(scenario())

    def test_solo_prompt_queue_has_a_bound(self) -> None:
        conversation = Conversation(Path.cwd())
        conversation.turn = "agent"
        conversation.flash = Mock()  # type: ignore[method-assign]
        conversation._pending_solo_prompts.extend(
            f"message {index}" for index in range(MAX_QUEUED_PROMPTS)
        )

        self.assertTrue(
            conversation._queue_solo_prompt_if_busy(
                "one too many"
            )
        )
        self.assertEqual(len(conversation._pending_solo_prompts), MAX_QUEUED_PROMPTS)
        self.assertIn("Queue is full", conversation.flash.call_args.args[0])

    def test_unexpected_solo_adapter_error_still_finishes_the_turn(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)
                        session = Mock()
                        session.relay_active = False
                        session.active_agents = []
                        session.roster = []
                        session.owner_data = None
                        session.stop = AsyncMock()
                        session.send_prompt = AsyncMock(
                            side_effect=RuntimeError("adapter lost")
                        )
                        conversation.session = session
                        conversation.agent = Mock()
                        conversation.post = AsyncMock()  # type: ignore[method-assign]
                        conversation.call_later = Mock()  # type: ignore[method-assign]

                        await conversation.send_prompt_to_agent.__wrapped__(
                            conversation, "Continue"
                        )

                        self.assertEqual(conversation.busy_count, 0)
                        conversation.call_later.assert_called_once_with(
                            conversation.agent_turn_over, None
                        )

        asyncio.run(scenario())

    def test_malformed_adapter_error_text_does_not_break_failure_ui(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)

                        await conversation.on_agent_fail(
                            AgentFail("[/unclosed]", "[still-not-markup]")
                        )

                        note = conversation.query(Note).last()
                        self.assertIn("[/unclosed]", note.render().plain)

        asyncio.run(scenario())

    def test_agent_failure_guidance_uses_error_presentation(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)

                        await conversation.on_agent_fail(
                            AgentFail("Failed to start agent", "adapter unavailable")
                        )

                        guidance = conversation.query(MarkdownNote).last()
                        self.assertTrue(guidance.has_class("-error"))

        asyncio.run(scenario())

    def test_prompt_failure_shows_upstream_error_in_error_presentation(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)

                        await conversation._post_agent_communication_error(
                            jsonrpc.APIError(
                                429,
                                "No capacity available for model gemini-3.5-flash",
                                None,
                            )
                        )

                        error = conversation.query(Note).last()
                        self.assertTrue(error.has_class("-error"))
                        rendered = error.render().plain
                        self.assertIn("Agent request failed", rendered)
                        self.assertIn("No capacity available", rendered)
                        self.assertNotIn("failed to start", rendered)

                        await conversation._post_agent_communication_error(
                            RuntimeError("```\n## Fake trusted heading\n" + "x" * 6_000)
                        )
                        literal = conversation.query(Note).last()
                        self.assertIn("```", literal.render().plain)
                        self.assertLess(len(literal.render().plain), 5_000)

        asyncio.run(scenario())

    def test_idle_gemini_failure_reconnects_without_a_failure_card(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        gemini = _RosterAgent("Gemini")
                        replacement = _RosterAgent("Gemini")
                        conversation.turn = "human"
                        conversation.session.restart_gemini_once = AsyncMock(
                            return_value=replacement
                        )
                        conversation.session.mark_failed = Mock()
                        conversation.flash = Mock()  # type: ignore[method-assign]
                        conversation.notify = Mock()  # type: ignore[method-assign]
                        conversation.post = AsyncMock()  # type: ignore[method-assign]

                        await conversation.on_agent_fail(
                            AgentFail(
                                "Agent returned a failure code: 1",
                                "Exit code: 1",
                                agent=gemini,  # type: ignore[arg-type]
                            )
                        )

                        conversation.session.restart_gemini_once.assert_awaited_once_with(
                            gemini, conversation, idle=True
                        )
                        conversation.session.mark_failed.assert_not_called()
                        conversation.notify.assert_not_called()
                        conversation.post.assert_not_awaited()
                        self.assertEqual(
                            conversation.flash.call_args.args[0],
                            "COMMS LOST // Gemini · REACQUIRING LINK",
                        )

        asyncio.run(scenario())

    def test_resuming_a_relay_marks_the_turn_busy_before_dispatch(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            session = Mock()
            session.relay_active = True
            conversation.session = session
            conversation.relay_paused = True
            conversation.post = AsyncMock(return_value=Mock())  # type: ignore[method-assign]
            conversation.flash = Mock()  # type: ignore[method-assign]
            conversation.send_prompt_to_agent = Mock()  # type: ignore[method-assign]

            await conversation.action_toggle_pause.__wrapped__(conversation)

            self.assertEqual(conversation.turn, "agent")
            session.resume.assert_called_once()
            conversation.flash.assert_called_once_with(
                "FORMATION // FLIGHT RESUMED",
                style="success",
            )
            conversation.send_prompt_to_agent.assert_called_once_with(
                "Resume the collaboration from the current shared workspace."
            )

        asyncio.run(scenario())

    def test_pausing_a_relay_uses_flight_status_copy(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            session = Mock()
            session.relay_active = True
            session.active_agents = []
            conversation.session = session
            conversation.flash = Mock()  # type: ignore[method-assign]

            await conversation.action_toggle_pause.__wrapped__(conversation)

            self.assertTrue(conversation.relay_paused)
            session.pause.assert_called_once()
            conversation.flash.assert_called_once_with(
                "FORMATION // HOLDING PATTERN",
                style="warning",
            )

        asyncio.run(scenario())

    def test_roster_start_failure_is_reported_in_the_conversation(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.1)
                        conversation = pilot.app.screen.query_one(Conversation)
                        session = Mock()
                        session.start = AsyncMock(
                            side_effect=RuntimeError("adapter missing")
                        )
                        session.stop = AsyncMock()
                        conversation.session = session
                        conversation.notify = Mock()  # type: ignore[method-assign]
                        conversation.post = AsyncMock()  # type: ignore[method-assign]

                        await conversation._start_agents()

                        self.assertFalse(conversation.agent_ready)
                        conversation.notify.assert_called_once()
                        posted = conversation.post.call_args.args[0]
                        message = str(posted.render())
                        self.assertIn("Unable to start", message)
                        self.assertIn("adapter missing", message)

        asyncio.run(scenario())

    def test_acp_handlers_are_registered_through_the_conversation_mro(self) -> None:
        expected = {
            "UpdateStatusLine",
            "Update",
            "UserMessage",
            "Thinking",
            "RequestPermission",
            "ToolCallUpdate",
            "ToolCall",
            "AvailableCommandsUpdate",
            "CreateTerminal",
            "KillTerminal",
            "GetTerminalState",
            "ReleaseTerminal",
            "WaitForTerminalExit",
            "SetModes",
        }
        registered = {
            message_type.__name__
            for cls in Conversation.__mro__
            for message_type in (getattr(cls, "_decorated_handlers", {}) or {})
        }
        self.assertEqual(expected - registered, set())

    def test_main_screen_mounts_conversation_and_dispatches_acp_update(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.5)
                        conversation = pilot.app.screen.query_one(Conversation)
                        self.assertFalse(hasattr(conversation, "_shell"))
                        placeholder = conversation.prompt.prompt_text_area.placeholder
                        self.assertNotIn("shell", str(placeholder).lower())
                        self.assertNotIn("!", str(placeholder))
                        conversation.post_message(
                            acp_messages.Update("text", "startup smoke response")
                        )
                        await pilot.pause(1.0)
                        self.assertEqual(
                            len(conversation.query(AgentResponse)), 1
                        )

                        conversation.modes = {
                            "auto": Mode("auto", "Auto", "Old mode")
                        }
                        conversation.current_mode = conversation.modes["auto"]
                        conversation.post_message(
                            acp_messages.SetModes(
                                "default",
                                {
                                    "default": Mode(
                                        "default", "Default", "Prompt first"
                                    ),
                                    "plan": Mode("plan", "Plan", "Read-only"),
                                },
                            )
                        )
                        await pilot.pause(0.2)
                        self.assertEqual(conversation.current_mode.id, "default")
                        self.assertEqual(
                            conversation.prompt.mode_switcher.highlighted,
                            conversation.prompt.mode_switcher.get_option_index(
                                "default"
                            ),
                        )
                        self.assertIsNotNone(
                            conversation.prompt.mode_switcher.get_option(
                                "codeswarm:discuss"
                            )
                        )
                        self.assertEqual(
                            conversation.prompt.mode_switcher.get_option_at_index(0).id,
                            "codeswarm:discuss",
                        )

                        conversation.post_message(
                            messages.ChangeMode("codeswarm:discuss")
                        )
                        await pilot.pause(0.2)
                        self.assertTrue(conversation.discussion_mode)
                        self.assertEqual(
                            conversation.current_mode.id,
                            "codeswarm:discuss",
                        )

                        # Any real ACP mode exits CodeSwarm's discussion policy.
                        conversation.post_message(messages.ChangeMode("default"))
                        await pilot.pause(0.2)
                        self.assertFalse(conversation.discussion_mode)

                        # ACP adapters occasionally report a current mode that
                        # is absent from a replacement mode list. This must not
                        # leave a stale selection or crash the OptionList.
                        conversation.post_message(
                            acp_messages.SetModes(
                                "missing",
                                {"safe": Mode("safe", "Safe", "Prompt first")},
                            )
                        )
                        await pilot.pause(0.2)
                        self.assertIsNone(conversation.current_mode)
                        self.assertIsNone(
                            conversation.prompt.mode_switcher.highlighted
                        )

                        conversation.post_message(acp_messages.ModeUpdate("missing"))
                        await pilot.pause(0.2)
                        self.assertIsNone(conversation.current_mode)

                        # A permissive adapter must not be able to break the
                        # prompt just by sending malformed command metadata.
                        conversation.post_message(
                            acp_messages.AvailableCommandsUpdate(
                                [
                                    {"name": "review", "description": "Review"},
                                    {"name": 42, "description": "invalid"},
                                    "invalid",
                                ]  # type: ignore[list-item]
                            )
                        )
                        await pilot.pause(0.2)
                        self.assertEqual(
                            [command.command for command in conversation.agent_slash_commands],
                            ["/review"],
                        )

                        # Exercise the same event path as pressing Enter in
                        # the prompt. Local slash commands must never leak to
                        # an agent as normal conversation turns.
                        with patch.object(conversation, "send_prompt_to_agent") as send:
                            conversation.post_message(
                                messages.UserInputSubmitted("/help")
                            )
                            await pilot.pause(0.2)
                            send.assert_not_called()
                        self.assertIn(
                            "CodeSwarm",
                            conversation.query(MarkdownNote).last().source,
                        )

                        claude = _RosterAgent("Claude")
                        gemini = _RosterAgent("Gemini")
                        conversation.session.roster = [
                            RosterEntry(
                                AgentData(
                                    identity="claude.ai",
                                    name="Claude",
                                    short_name="claude",
                                ),
                                claude,  # type: ignore[arg-type]
                            ),
                            RosterEntry(
                                AgentData(
                                    identity="gemini.google.com",
                                    name="Gemini",
                                    short_name="gemini",
                                ),
                                gemini,  # type: ignore[arg-type]
                            ),
                        ]
                        conversation.agent_ready = True
                        conversation._ready_agents = {id(claude)}
                        await conversation.on_agent_ready(AgentReady(gemini))
                        conversation._active_relay_agent = gemini  # type: ignore[assignment]
                        conversation._refresh_roster_info()
                        await pilot.pause(0.1)
                        roster_text = conversation.prompt.query_one(AgentInfo).render().plain
                        self.assertIn("Claude", roster_text)
                        self.assertIn("● Gemini", roster_text)

        asyncio.run(scenario())

    def test_mode_highlight_does_not_leak_into_the_prompt(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        mode = Mode(
                            "codeswarm:mode:full-access",
                            "Auto pilot",
                            "Approve all tools",
                        )
                        conversation.modes = {mode.id: mode}
                        conversation.current_mode = mode
                        await pilot.pause(0.1)

                        self.assertEqual(
                            conversation.prompt.prompt_text_area.suggestion,
                            "",
                        )

        asyncio.run(scenario())
