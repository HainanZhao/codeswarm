import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.widgets import Checkbox

from codeswarm.acp.agent import _parse_node_version
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.app import CodeSwarmApp
from codeswarm.messages import UserInputSubmitted
from codeswarm.screens.config import ConfigScreen
from codeswarm.session import RosterEntry
from codeswarm.widgets.conversation import Conversation
from codeswarm.widgets.prompt import Prompt, PromptTextArea


class Round1AntigravityTests(unittest.TestCase):
    def test_live_config_screen_preserves_session_roster_order_over_saved_settings(
        self,
    ) -> None:
        """When /config opens in a live session, active agents must appear in live session order.

        If launcher.roster had [peer, owner] from a past session, ConfigScreen
        must not place the peer before the session owner in the UI.
        """
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                # Set saved launcher.roster in inverted order: peer first, owner second
                pilot.app.settings.set(
                    "launcher.roster", "claude.com\ngeminicli.com"
                )

                conversation = pilot.app.screen.query_one(Conversation)
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="geminicli.com",
                            name="Gemini CLI",
                            short_name="gemini",
                            type="coding",
                        )
                    ),
                    RosterEntry(
                        AgentData(
                            identity="claude.com",
                            name="Claude Code",
                            short_name="claude",
                            type="coding",
                        )
                    ),
                ]

                await pilot.app.push_screen(ConfigScreen(conversation))
                await pilot.pause(0.1)

                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)

                # The displayed / read roster order must reflect the live session
                # (owner gemini first, peer claude second), NOT the stale saved order.
                self.assertEqual(
                    config._read_roster()[:2],
                    ["geminicli.com", "claude.com"],
                )

                # The owner must be the first row and can be unchecked so a
                # replacement can take ownership when Save is pressed.
                first_checkbox = config.query("#config-roster-options Checkbox").first()
                assert first_checkbox is not None
                self.assertEqual(
                    config._roster_controls[first_checkbox.id], "geminicli.com"
                )
                self.assertFalse(first_checkbox.disabled)

        asyncio.run(scenario())

    def test_prompt_submit_allows_local_shell_commands_when_agent_not_ready(
        self,
    ) -> None:
        """Local shell commands (!command) do not use the agent and must be submitted when agent is offline."""
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                prompt = conversation.prompt
                prompt.agent_ready = False
                prompt.prompt_text_area.agent_ready = False
                prompt.prompt_text_area.text = "!ls -la"

                messages_posted: list[object] = []
                orig_post_message = prompt.prompt_text_area.post_message

                def fake_post_message(msg: object) -> object:
                    messages_posted.append(msg)
                    return orig_post_message(msg)  # type: ignore[arg-type]

                prompt.prompt_text_area.post_message = fake_post_message  # type: ignore[method-assign]

                prompt.prompt_text_area.action_submit()

                # Must post UserInputSubmitted("!ls -la") rather than being blocked by agent_ready
                submitted = [
                    msg for msg in messages_posted if isinstance(msg, UserInputSubmitted)
                ]
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0].body, "!ls -la")

        asyncio.run(scenario())

    def test_parse_node_version_supports_major_and_minor_specifiers(self) -> None:
        """Version requirements like '22' or '22.18' should parse to standard comparable tuples."""
        self.assertEqual(_parse_node_version("22"), (22, 0, 0))
        self.assertEqual(_parse_node_version("v22.18"), (22, 18, 0))
        self.assertEqual(_parse_node_version("22.18.0"), (22, 18, 0))
