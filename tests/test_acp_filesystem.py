from pathlib import Path
import asyncio
import tempfile
import unittest
from typing import cast

from wingmen.acp.agent import Agent, MAX_FILE_READ_BYTES
from wingmen.agent_schema import Agent as AgentData
from wingmen import jsonrpc


def agent_data() -> AgentData:
    return cast(
        AgentData,
        {
            "name": "Test agent",
            "identity": "test.agent",
            "run_command": {"*": "test-agent"},
        },
    )


class ACPFilesystemTests(unittest.TestCase):
    def test_empty_permission_options_are_rejected(self) -> None:
        async def scenario() -> None:
            agent = Agent(Path.cwd(), agent_data(), None)
            with self.assertRaises(jsonrpc.InvalidParams):
                await agent.rpc_request_permission(
                    "session",
                    [],
                    cast(object, {"toolCallId": "tool-1"}),
                )

        asyncio.run(scenario())

    def test_terminal_cwd_cannot_escape_the_project_root(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as other:
                agent = Agent(Path(project), agent_data(), None)
                for path in ("../", other):
                    with self.subTest(path=path):
                        with self.assertRaises(jsonrpc.InvalidParams):
                            await agent.rpc_terminal_create("echo", cwd=path)

        asyncio.run(scenario())

    def test_read_and_write_stay_within_the_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as other:
            root = Path(project)
            outside = Path(other) / "outside.txt"
            (root / "inside.txt").write_text("inside", encoding="utf-8")
            agent = Agent(root, agent_data(), None)

            self.assertEqual(
                agent.rpc_read_text_file("session", "inside.txt"), {"content": "inside"}
            )
            agent.rpc_write_text_file("session", "written.txt", "safe")
            self.assertEqual((root / "written.txt").read_text(), "safe")

            for path in ("../outside.txt", str(outside)):
                with self.subTest(path=path):
                    with self.assertRaises(jsonrpc.InvalidParams):
                        agent.rpc_read_text_file("session", path)
                    with self.assertRaises(jsonrpc.InvalidParams):
                        agent.rpc_write_text_file("session", path, "unsafe")
            self.assertFalse(outside.exists())

    def test_symlink_to_outside_the_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as other:
            root = Path(project)
            outside = Path(other) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            link = root / "outside-link"
            link.symlink_to(outside)
            agent = Agent(root, agent_data(), None)

            with self.assertRaises(jsonrpc.InvalidParams):
                agent.rpc_read_text_file("session", "outside-link")

    def test_large_file_reads_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            (root / "large.txt").write_bytes(b"x" * (MAX_FILE_READ_BYTES + 1024))
            agent = Agent(root, agent_data(), None)

            content = agent.rpc_read_text_file("session", "large.txt")["content"]

            self.assertLessEqual(len(content.encode("utf-8")), MAX_FILE_READ_BYTES)


if __name__ == "__main__":
    unittest.main()
