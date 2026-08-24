import asyncio
import os
import signal
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace
from unittest.mock import patch

from wingmen.widgets.terminal_tool import (
    DEFAULT_OUTPUT_BYTE_LIMIT,
    MAX_OUTPUT_BYTE_LIMIT,
    Command,
    TerminalTool,
)
from wingmen.widgets.terminal import Terminal


class TerminalToolTests(unittest.TestCase):
    def test_terminal_resize_does_not_query_a_conversation_owner(self) -> None:
        terminal = Terminal()
        terminal.query_ancestor = Mock()  # type: ignore[method-assign]

        terminal.update_size(100, 24)

        terminal.query_ancestor.assert_not_called()

    def test_terminal_output_is_bounded_when_an_agent_omits_a_limit(self) -> None:
        terminal = TerminalTool(Command("echo", [], os.environ, os.curdir))

        self.assertEqual(terminal._output_byte_limit, DEFAULT_OUTPUT_BYTE_LIMIT)

    def test_terminal_output_reports_truncation_at_the_limit(self) -> None:
        terminal = TerminalTool(
            Command("echo", [], os.environ, os.curdir), output_byte_limit=4
        )
        terminal._record_output(b"abc")
        terminal._record_output(b"def")

        self.assertEqual(terminal.get_output(), ("cdef", True))

    def test_adapter_terminal_limit_is_validated_and_capped(self) -> None:
        terminal = TerminalTool(
            Command("echo", [], os.environ, os.curdir),
            output_byte_limit=MAX_OUTPUT_BYTE_LIMIT * 100,
        )
        self.assertEqual(terminal._output_byte_limit, MAX_OUTPUT_BYTE_LIMIT)
        with self.assertRaises(ValueError):
            TerminalTool(
                Command("echo", [], os.environ, os.curdir), output_byte_limit=-1
            )

    def test_terminal_output_reports_evicted_chunks_as_truncated(self) -> None:
        terminal = TerminalTool(
            Command("echo", [], os.environ, os.curdir), output_byte_limit=4
        )
        terminal._record_output(b"ab")
        terminal._record_output(b"cd")
        terminal._record_output(b"ef")

        self.assertEqual(terminal.get_output(), ("cdef", True))

    def test_kill_terminates_the_entire_terminal_process_group(self) -> None:
        terminal = TerminalTool(Command("sleep", ["60"], os.environ, os.curdir))
        terminal._process = SimpleNamespace(pid=12345, returncode=None)  # type: ignore[assignment]

        with patch("wingmen.widgets.terminal_tool.os.killpg") as killpg:
            self.assertTrue(terminal.kill())

        killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_start_reports_a_setup_failure_without_hanging(self) -> None:
        async def scenario() -> None:
            terminal = TerminalTool(Command("false", [], os.environ, os.curdir))
            terminal._run = AsyncMock(side_effect=OSError("no PTY"))  # type: ignore[method-assign]

            with self.assertRaisesRegex(RuntimeError, "Unable to start"):
                await asyncio.wait_for(terminal.start(), timeout=0.5)

            self.assertTrue(terminal._exit_event.is_set())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
