import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Checkbox, Footer, Switch

from wingmen.app import WingmenApp
from wingmen.screens.config import ConfigScreen
from wingmen.widgets.conversation import Conversation


class ConfigScreenTests(unittest.TestCase):
    def test_narrow_config_keeps_roster_and_actions_usable_without_a_footer(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
                        size=(80, 24)
                    ) as pilot:
                        await pilot.app.push_screen(ConfigScreen())
                        await pilot.pause(0.1)

                        config = pilot.app.screen
                        assert isinstance(config, ConfigScreen)
                        options = list(
                            config.query("#config-roster-options Checkbox")
                        )
                        self.assertTrue(options)
                        self.assertTrue(
                            all(option.label.plain.strip() for option in options)
                        )
                        self.assertTrue(config.query_one("#save").display)
                        self.assertTrue(config.query_one("#cancel").display)
                        self.assertFalse(pilot.app.query(Footer))

        asyncio.run(scenario())

    def test_config_command_opens_preferences_and_saves_a_live_setting(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with WingmenApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        self.assertTrue(await conversation.slash_command("/config"))
                        await pilot.pause(0.1)

                        config = pilot.app.screen
                        self.assertIsInstance(config, ConfigScreen)
                        assert isinstance(config, ConfigScreen)
                        keys = {
                            field.key
                            for _group, fields in config._fields()
                            for field in fields
                        }
                        self.assertNotIn("ui.theme", keys)
                        self.assertIn("notifications.turn_over", keys)
                        self.assertIn("diff.wrap", keys)
                        self.assertNotIn("launcher.roster", keys)
                        self.assertNotIn("ui.prune_low_mark", keys)
                        self.assertNotIn("ui.prune_excess", keys)
                        self.assertNotIn("notifications.hide_low_severity", keys)
                        self.assertNotIn("diff.annotations", keys)

                        config.query_one("#config-agent-thoughts", Switch).value = True
                        roster_options = list(
                            config.query("#config-roster-options Checkbox")
                        )
                        for option in roster_options:
                            self.assertGreaterEqual(option.content_region.height, 1)
                            self.assertGreaterEqual(option.outer_size.height, 3)
                        self.assertEqual(
                            {
                                option.label.plain.strip().split(" — ", 1)[0]
                                for option in roster_options
                            },
                            {
                                "Claude Code  (claude)",
                                "Gemini CLI  (gemini)",
                                "Codex CLI  (codex)",
                            },
                        )
                        for option in roster_options:
                            option.value = any(
                                name in option.label.plain
                                for name in ("Gemini", "Claude")
                            )
                        expected_roster = "\n".join(config._read_roster())
                        await config.action_save()
                        await pilot.pause(0.1)

                        self.assertTrue(pilot.app.settings.get("agent.thoughts", bool))
                        self.assertTrue(pilot.app.has_class("-hide-thoughts") is False)
                        self.assertEqual(
                            pilot.app.settings.get("launcher.roster", str),
                            expected_roster,
                        )

        asyncio.run(scenario())

    def test_existing_roster_order_is_preserved_by_checkbox_list(self) -> None:
        async def scenario() -> None:
            async with WingmenApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                pilot.app.settings.set(
                    "launcher.roster", "geminicli.com\nclaude.com"
                )
                await pilot.app.push_screen(ConfigScreen())
                await pilot.pause(0.1)

                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)
                options = list(config.query("#config-roster-options Checkbox"))
                self.assertEqual(
                    [
                        option.label.plain.strip().split(" — ", 1)[0]
                        for option in options[:2]
                    ],
                    ["1. Gemini CLI  (gemini)", "2. Claude Code  (claude)"],
                )
                self.assertEqual(config._read_roster(), ["geminicli.com", "claude.com"])

        asyncio.run(scenario())
