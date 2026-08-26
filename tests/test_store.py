import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from textual import widgets

from codeswarm.app import CodeSwarmApp
from codeswarm import messages
from codeswarm.screens.store import AgentGridSelect, AgentItem, StoreScreen


_CATALOG = {
    "example.test": {
        "identity": "example.test",
        "name": "Example Agent",
        "short_name": "example",
        "url": "https://example.test",
        "protocol": "acp",
        "author_name": "Example",
        "author_url": "https://example.test",
        "publisher_name": "Example",
        "publisher_url": "https://example.test",
        "type": "coding",
        "description": "A test agent.",
        "tags": [],
        "run_command": {"*": "example-agent"},
        "help": "",
        "actions": {},
    }
}


class StoreScreenTests(unittest.TestCase):
    def test_landing_logo_is_an_airplane_codeswarm_formation(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set(_CATALOG)),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        logos = list(pilot.app.screen.query("#codeswarm-formation"))
                        self.assertEqual(len(logos), 1)
                        logo = logos[0]
                        lines = logo.render().plain.splitlines()

                        self.assertEqual(sum(line.count("✈") for line in lines), 5)
                        self.assertEqual([line.count("✈") for line in lines], [1, 2, 2])
                        self.assertLess(lines[0].index("✈"), lines[1].rindex("✈"))

        asyncio.run(scenario())

    def test_notifications_before_the_conversation_prompt_remain_toasts(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set(_CATALOG)),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        pilot.app._notifications.clear()

                        pilot.app.screen.notify(
                            "Install the agent CLI",
                            title="Agent unavailable",
                            severity="warning",
                        )
                        await pilot.pause()

                        notifications = list(pilot.app._notifications)
                        self.assertEqual(len(notifications), 1)
                        self.assertEqual(
                            notifications[0].message, "Install the agent CLI"
                        )

        asyncio.run(scenario())

    def test_click_toggles_agent_roster_selection(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set(_CATALOG)),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        assert isinstance(screen, StoreScreen)
                        item = screen.query_one(AgentItem)

                        await pilot.click("AgentItem")
                        await pilot.pause()
                        self.assertIn("example.test", screen._roster_selection)
                        self.assertTrue(item.selected)
                        self.assertFalse(item.has_class("-highlight"))

                        await pilot.click("AgentItem")
                        await pilot.pause()
                        self.assertNotIn("example.test", screen._roster_selection)
                        self.assertFalse(item.selected)
                        self.assertFalse(item.has_class("-highlight"))

        asyncio.run(scenario())

    def test_enter_launches_selected_roster_after_click(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set(_CATALOG)),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        assert isinstance(screen, StoreScreen)
                        await pilot.click("AgentItem")
                        await pilot.pause()
                        self.assertIs(screen.focused, screen.query_one(AgentGridSelect))
                        with patch.object(screen.app, "launch_agent", new=MagicMock()) as launch:
                            await pilot.press("enter")
                            await pilot.pause()

                        launch.assert_called_once_with(
                            "example.test", agent_session_id=None, session_pk=None,
                            initial_prompt=None, peer_identities=()
                        )

        asyncio.run(scenario())

    def test_roster_launch_preserves_selection_order(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value={"codex.test", "claude.test"}),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        assert isinstance(screen, StoreScreen)
                        codex = {**_CATALOG["example.test"], "identity": "codex.test"}
                        claude = {**_CATALOG["example.test"], "identity": "claude.test"}
                        screen._roster_selection = {
                            codex["identity"]: codex,
                            claude["identity"]: claude,
                        }
                        screen._installed = {"codex.test", "claude.test"}
                        with patch.object(screen, "post_message") as post:
                            screen.on_agent_grid_select_launch_roster(
                                AgentGridSelect.LaunchRoster(None)
                            )
                        post.assert_called_once_with(
                            messages.LaunchAgent(
                                "codex.test", peers=("claude.test",)
                            )
                        )

        asyncio.run(scenario())

    def test_unavailable_roster_is_explained_without_launching(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set()),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        assert isinstance(screen, StoreScreen)
                        status = screen.query_one(AgentItem).query_one(
                            "#availability", widgets.Label
                        )
                        self.assertIn("Not detected", status.render().plain)
                        self.assertFalse(screen.query(AgentItem).first().query("#type"))
                        screen._roster_selection = dict(_CATALOG)
                        with patch.object(screen, "post_message") as post, patch.object(
                            screen, "notify"
                        ) as notify:
                            screen.on_agent_grid_select_launch_roster(
                                AgentGridSelect.LaunchRoster(None)
                            )

                        post.assert_not_called()
                        self.assertIn("Not detected", notify.call_args.args[0])

        asyncio.run(scenario())

    def test_store_keeps_only_roster_launch_controls(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "codeswarm.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "codeswarm.screens.store.available_identities",
                    new=AsyncMock(return_value=set()),
                ), patch(
                    "codeswarm.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with CodeSwarmApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        self.assertIsInstance(screen, StoreScreen)
                        self.assertFalse(
                            any(binding.key == "ctrl+r" for binding in screen.BINDINGS)
                        )
                        grid = screen.query_one(AgentGridSelect)
                        self.assertFalse(any(binding.key == "d" for binding in grid.BINDINGS))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
