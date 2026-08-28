import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, Checkbox, Footer, Input, Switch

from codeswarm.app import CodeSwarmApp
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.screens.config import ConfigScreen
from codeswarm.session import RosterEntry
from codeswarm.widgets.conversation import Conversation


class ConfigScreenTests(unittest.TestCase):
    def test_flash_duration_uses_decimal_validation(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                size=(120, 40)
            ) as pilot:
                await pilot.app.push_screen(ConfigScreen())
                await pilot.pause(0.1)

                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)
                flash_duration = config.query_one(
                    "#config-ui-flash_duration", Input
                )
                self.assertEqual(flash_duration.value, "3.0")
                self.assertEqual(flash_duration.type, "number")
                self.assertTrue(flash_duration.is_valid)

                flash_duration.value = "not-a-number"
                await pilot.pause()
                self.assertFalse(flash_duration.is_valid)

        asyncio.run(scenario())

    def test_narrow_config_keeps_roster_and_actions_usable_without_a_footer(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
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
                    async with CodeSwarmApp(setup_prompt=False).run_test(
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
                        scrollbar = next(
                            field
                            for _group, fields in config._fields()
                            for field in fields
                            if field.key == "ui.scrollbar"
                        )
                        self.assertEqual(
                            scrollbar.choices,
                            [("Normal", "normal"), ("Hidden", "hidden")],
                        )

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
                                "Antigravity CLI  (antigravity)",
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
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
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

    def test_inline_roster_controls_move_without_toggling_agents(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                pilot.app.settings.set(
                    "launcher.roster", "geminicli.com\nclaude.com"
                )
                await pilot.app.push_screen(ConfigScreen())
                await pilot.pause(0.1)

                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)
                gemini = config.query_one("#config-roster-agent-0", Checkbox)
                claude = config.query_one("#config-roster-agent-1", Checkbox)
                self.assertTrue(gemini.value)
                self.assertTrue(claude.value)

                self.assertTrue(
                    config.query_one("#config-roster-agent-0-up", Button).disabled
                )
                config.query_one("#config-roster-agent-1-up", Button).press()
                await pilot.pause()

                self.assertEqual(config._read_roster()[:2], ["claude.com", "geminicli.com"])
                self.assertTrue(gemini.value)
                self.assertTrue(claude.value)
                self.assertTrue(
                    config.query_one("#config-roster-agent-1-up", Button).disabled
                )
                self.assertTrue(
                    config.query_one("#config-roster-agent-0-down", Button).disabled
                )

                unchecked = next(
                    option
                    for option in config.query("#config-roster-options Checkbox")
                    if not option.value
                )
                assert unchecked.id is not None
                self.assertTrue(
                    config.query_one(f"#{unchecked.id}-up", Button).disabled
                )
                self.assertTrue(
                    config.query_one(f"#{unchecked.id}-down", Button).disabled
                )

                config.query_one("#config-roster-agent-1-down", Button).focus()
                await pilot.press("alt+down")
                await pilot.pause()
                self.assertEqual(config._read_roster()[:2], ["geminicli.com", "claude.com"])

        asyncio.run(scenario())

    def test_live_config_transfers_owner_on_save(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="geminicli.com",
                            name="Gemini CLI",
                            short_name="gemini",
                        )
                    )
                ]
                reconcile = AsyncMock(return_value=[])
                conversation.reconcile_roster = reconcile  # type: ignore[method-assign]

                self.assertTrue(await conversation.slash_command("/config"))
                await pilot.pause(0.1)
                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)

                owner = next(
                    option
                    for option in config.query("#config-roster-options Checkbox")
                    if config._roster_controls[option.id] == "geminicli.com"
                )
                claude = next(
                    option
                    for option in config.query("#config-roster-options Checkbox")
                    if config._roster_controls[option.id] == "claude.com"
                )
                self.assertTrue(owner.value)
                self.assertFalse(owner.disabled)
                self.assertFalse(claude.value)
                owner.value = False
                claude.value = True
                expected_roster = config._read_roster()

                await config.action_save()
                reconcile.assert_awaited_once_with(
                    expected_roster, config._agents
                )
                self.assertEqual(set(expected_roster), {"claude.com"})

        asyncio.run(scenario())

    def test_live_config_stays_open_when_agent_work_is_active(self) -> None:
        async def scenario() -> None:
            async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
                conversation = pilot.app.screen.query_one(Conversation)
                conversation.session.roster = [
                    RosterEntry(
                        AgentData(
                            identity="geminicli.com",
                            name="Gemini CLI",
                            short_name="gemini",
                        )
                    )
                ]
                conversation.turn = "agent"
                reconcile = AsyncMock(return_value=[])
                conversation.reconcile_roster = reconcile  # type: ignore[method-assign]

                self.assertTrue(await conversation.slash_command("/config"))
                await pilot.pause(0.1)
                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)

                await config.action_save()
                await pilot.pause()

                self.assertIs(pilot.app.screen, config)
                reconcile.assert_not_awaited()

        asyncio.run(scenario())
