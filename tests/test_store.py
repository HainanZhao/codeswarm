import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual import widgets

from wingmen.app import WingmenApp
from wingmen import messages
from wingmen.screens.store import AgentGridSelect, AgentItem, StoreScreen


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
    def test_click_toggles_agent_roster_selection(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "wingmen.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "wingmen.screens.store.available_identities",
                    new=AsyncMock(return_value=set(_CATALOG)),
                ), patch(
                    "wingmen.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
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

    def test_roster_launch_preserves_selection_order(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "wingmen.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "wingmen.screens.store.available_identities",
                    new=AsyncMock(return_value={"codex.test", "claude.test"}),
                ), patch(
                    "wingmen.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
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
                    "wingmen.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "wingmen.screens.store.available_identities",
                    new=AsyncMock(return_value=set()),
                ), patch(
                    "wingmen.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
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

    def test_filter_accepts_digits_as_search_text(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "wingmen.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "wingmen.screens.store.available_identities",
                    new=AsyncMock(return_value=set()),
                ), patch(
                    "wingmen.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        screen = pilot.app.screen
                        self.assertIsInstance(screen, StoreScreen)
                        info = screen.get_info().plain
                        self.assertIn("Choose one or more coding agents", info)
                        self.assertNotIn("fractal", info)
                        self.assertNotIn("fighter", info)
                        agent_filter = screen.query_one("#agent-filter", widgets.Input)
                        agent_filter.focus()
                        await pilot.press("1")
                        self.assertEqual(agent_filter.value, "1")

        asyncio.run(scenario())

    def test_store_keeps_only_roster_launch_controls(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch(
                    "wingmen.screens.store.read_agents",
                    new=AsyncMock(return_value=_CATALOG),
                ), patch(
                    "wingmen.screens.store.available_identities",
                    new=AsyncMock(return_value=set()),
                ), patch(
                    "wingmen.screens.store.detect_preferred_agents",
                    new=AsyncMock(return_value=[]),
                ):
                    async with WingmenApp(mode="store", setup_prompt=False).run_test(
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
