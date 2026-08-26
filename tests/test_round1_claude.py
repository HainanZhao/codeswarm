"""Round 1 bug-hunt reproductions against the uncommitted live-roster diff."""

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import AsyncMock

from typing import cast
from unittest.mock import patch

from codeswarm.acp.agent import Agent, _prepare_node_environment
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.app import CodeSwarmApp
from codeswarm.screens.config import ConfigScreen
from codeswarm.session import RosterEntry
from codeswarm.widgets.conversation import Conversation


async def _eof_bytes() -> bytes:
    return b""


async def _eof_read(size: int = -1) -> bytes:
    return b""


class _ExitedProcess:
    """Minimal subprocess facade that reaches the ACP EOF path immediately."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = type("_EOF", (), {"readline": staticmethod(_eof_bytes)})()
        self.stdin = object()
        self.stderr = type("_Err", (), {"read": staticmethod(_eof_read)})()

    async def wait(self) -> int:
        return self.returncode


def _fake_nvm_root(root: Path, versions: tuple[str, ...], delay: str) -> Path:
    """Build an NVM layout whose `node --version` probes take measurable time."""
    for version in versions:
        node = root / "versions" / "node" / f"v{version}" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_text(f"#!/bin/sh\nsleep {delay}\necho v{version}\n")
        node.chmod(0o755)
    return root


class NodeEnvironmentTests(unittest.TestCase):
    def test_node_probe_does_not_block_the_event_loop(self) -> None:
        """Probing every installed NVM runtime must not freeze the UI."""

        async def scenario() -> None:
            with TemporaryDirectory() as tmp:
                nvm = _fake_nvm_root(
                    Path(tmp), ("20.11.0", "22.18.0", "24.1.0"), "0.3"
                )
                data = cast(
                    AgentData,
                    {
                        "name": "Antigravity CLI",
                        "identity": "antigravity.google.com",
                        "run_command": {"*": "npx -y agy-acp"},
                        "minimum_node_version": "22.18.0",
                    },
                )
                agent = Agent(Path.cwd(), data, None)
                agent._stopping = True
                ticks: list[float] = []

                async def heartbeat() -> None:
                    while True:
                        ticks.append(monotonic())
                        await asyncio.sleep(0.01)

                beat = asyncio.create_task(heartbeat())
                await asyncio.sleep(0.05)
                with (
                    patch.dict(
                        os.environ,
                        {"NVM_DIR": str(nvm), "PATH": "/nonexistent-for-test"},
                    ),
                    patch(
                        "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                        new=AsyncMock(return_value=_ExitedProcess()),
                    ),
                ):
                    await agent._run_agent()
                beat.cancel()

                gaps = [b - a for a, b in zip(ticks, ticks[1:])]
                self.assertTrue(gaps, "heartbeat never ran")
                self.assertLess(
                    max(gaps),
                    0.25,
                    "the Node probe blocked the asyncio event loop; the whole "
                    "TUI is frozen for the duration of every `node --version` "
                    f"call (largest observed stall: {max(gaps):.2f}s)",
                )

        asyncio.run(scenario())

    def test_prepared_path_has_no_empty_element(self) -> None:
        """An empty PATH element means "current directory" to the shell."""
        with TemporaryDirectory() as tmp:
            nvm = _fake_nvm_root(Path(tmp), ("22.18.0",), "0")
            prepared = _prepare_node_environment({"NVM_DIR": str(nvm)}, "22.18.0")
        elements = prepared["PATH"].split(os.pathsep)
        self.assertNotIn(
            "",
            elements,
            "PATH contains an empty element, which POSIX shells resolve to the "
            f"current working directory: {prepared['PATH']!r}",
        )


class LiveRosterTests(unittest.TestCase):
    def test_saving_config_keeps_a_live_peer_absent_from_the_catalog(self) -> None:
        """A peer with no checkbox must not be silently dropped on save."""

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
                    ),
                    RosterEntry(
                        AgentData(
                            identity="custom.local",
                            name="My Local Adapter",
                            short_name="mine",
                        )
                    ),
                ]
                reconcile = AsyncMock(return_value=[])
                conversation.reconcile_roster = reconcile  # type: ignore[method-assign]

                self.assertTrue(await conversation.slash_command("/config"))
                await pilot.pause(0.1)
                config = pilot.app.screen
                assert isinstance(config, ConfigScreen)

                await config.action_save()
                reconcile.assert_awaited_once()
                requested = reconcile.await_args.args[0]
                self.assertIn(
                    "custom.local",
                    requested,
                    "the live peer 'custom.local' has no checkbox in /config, so "
                    "an untouched save asks reconcile_roster for a roster that "
                    "excludes it — reconcile_roster then stops the agent",
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
