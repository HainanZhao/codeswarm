import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from codeswarm.agents import (
    detect_preferred_agents,
    is_agent_available,
    read_agents,
    resolve_agent,
)


class AgentCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agents = asyncio.run(read_agents())

    def test_core_agents_declare_their_transport(self) -> None:
        self.assertEqual(
            {agent["short_name"] for agent in self.agents.values()},
            {"antigravity", "claude", "codex", "gemini"},
        )
        transports = {
            agent["short_name"]: agent["protocol"] for agent in self.agents.values()
        }
        self.assertEqual(transports["antigravity"], "native")
        self.assertTrue(
            all(transports[name] == "acp" for name in ("claude", "codex", "gemini"))
        )

    def test_codex_uses_the_current_official_acp_adapter(self) -> None:
        codex = asyncio.run(resolve_agent("codex"))
        self.assertIsNotNone(codex)
        assert codex is not None
        self.assertEqual(
            codex["run_command"]["*"],
            "npx -y @agentclientprotocol/codex-acp",
        )

    def test_antigravity_uses_the_native_cli_integration(self) -> None:
        antigravity = asyncio.run(resolve_agent("antigravity"))
        self.assertIsNotNone(antigravity)
        assert antigravity is not None
        self.assertEqual(
            antigravity["run_command"]["*"],
            "agy",
        )
        self.assertNotIn("minimum_node_version", antigravity)
        self.assertEqual(
            antigravity["full_access_startup_argument"],
            "--dangerously-skip-permissions",
        )
        self.assertEqual(antigravity["detect_command"]["*"], "agy")

    def test_claude_and_gemini_use_the_current_acp_invocations(self) -> None:
        claude = asyncio.run(resolve_agent("claude"))
        gemini = asyncio.run(resolve_agent("gemini"))
        self.assertIsNotNone(claude)
        self.assertIsNotNone(gemini)
        assert claude is not None and gemini is not None
        self.assertEqual(
            claude["run_command"]["*"],
            "npx -y @agentclientprotocol/claude-agent-acp",
        )
        self.assertEqual(gemini["run_command"]["*"], "gemini --acp")

    def test_resolve_agent_accepts_identity_case_insensitively(self) -> None:
        agent = asyncio.run(resolve_agent("OPENAI.COM"))
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent["short_name"], "codex")

    def test_resolve_agent_accepts_alias(self) -> None:
        agent = asyncio.run(resolve_agent("agy"))
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent["identity"], "antigravity.google.com")
        self.assertEqual(agent["short_name"], "antigravity")

        agent_codex = asyncio.run(resolve_agent("openai"))
        self.assertIsNotNone(agent_codex)
        assert agent_codex is not None
        self.assertEqual(agent_codex["identity"], "openai.com")
        self.assertEqual(agent_codex["short_name"], "codex")

    def test_preferred_detection_reuses_known_availability(self) -> None:
        async def scenario() -> None:
            with patch(
                "codeswarm.agents.available_identities", new=AsyncMock()
            ) as probe:
                agents = await detect_preferred_agents(
                    self.agents, {"claude.com", "geminicli.com"}
                )
            probe.assert_not_awaited()
            self.assertEqual(
                [agent["short_name"] for agent in agents], ["claude", "gemini"]
            )

        asyncio.run(scenario())

    def test_availability_uses_the_current_platform_command(self) -> None:
        agent = self.agents["claude.com"]
        with patch(
            "codeswarm.agents.codeswarm.get_os_matrix", return_value="platform-agent"
        ) as get_command, patch(
            "codeswarm.agents.shutil.which", return_value="/bin/acp"
        ) as which:
            self.assertTrue(is_agent_available(agent))

        get_command.assert_called_once_with(agent["detect_command"])
        which.assert_called_once_with("platform-agent")

    def test_npx_adapters_are_not_detected_from_node_alone(self) -> None:
        claude = self.agents["claude.com"]
        with patch("codeswarm.agents.shutil.which") as which:
            which.side_effect = lambda executable: (
                "/usr/bin/npx" if executable == "npx" else None
            )
            self.assertFalse(is_agent_available(claude))

        which.assert_called_once_with("claude")


if __name__ == "__main__":
    unittest.main()
