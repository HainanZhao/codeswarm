import asyncio
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace
from unittest.mock import patch

from codeswarm.ansi._ansi import MAX_SCROLLBACK_LINES
from codeswarm.app import CodeSwarmApp
from codeswarm.widgets.conversation import Conversation
from codeswarm.widgets.terminal_tool import (
    DEFAULT_OUTPUT_BYTE_LIMIT,
    MAX_OUTPUT_BYTE_LIMIT,
    Command,
    TerminalTool,
)
from codeswarm.widgets.terminal import Terminal


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

        with patch("codeswarm.widgets.terminal_tool.os.killpg") as killpg:
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


    def test_scrollback_is_capped_for_a_verbose_command(self) -> None:
        """A verbose build must not grow the buffer without limit.

        Scrollback costs roughly a kilobyte per unfolded line and nothing
        bounded it, so one noisy command could hold tens of megabytes for as
        long as the terminal stayed in the transcript.
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(100, 30)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        terminal = TerminalTool(
                            Command("printf", ["x"], {}, Path.cwd()),
                            minimum_terminal_width=60,
                        )
                        await conversation.post(terminal)
                        await pilot.pause(0.1)

                        overflow = MAX_SCROLLBACK_LINES + 800
                        # Fed through the widget, which is the production path
                        # and the owner of the row-keyed render cache.
                        await terminal.write(
                            "".join(
                                f"line {index:06d} compiling\r\n"
                                for index in range(overflow)
                            )
                        )
                        await pilot.pause(0.2)

                        buffer = terminal.state.scrollback_buffer
                        self.assertLessEqual(
                            len(buffer.lines), MAX_SCROLLBACK_LINES
                        )
                        # The tail survives: that is where a failure appears.
                        self.assertIn(
                            f"line {overflow - 1:06d}",
                            buffer.lines[-1].content.plain,
                        )
                        # And the renderer's indices still line up.
                        self.assertEqual(
                            len(buffer.line_to_fold), len(buffer.lines)
                        )
                        for fold in buffer.folded_lines:
                            self.assertLess(fold.line_no, len(buffer.lines))
                        # Rows were renumbered. The render cache is keyed on
                        # the row, so a surviving entry would paint an evicted
                        # line; check what is actually rendered, not the cache.
                        first_row = terminal._render_line(0, 0, 60)
                        rendered = "".join(
                            segment.text for segment in first_row
                        )
                        self.assertIn(
                            buffer.lines[0].content.plain.strip(),
                            rendered,
                        )
                        self.assertNotIn("line 000000", rendered)
                        terminal.kill()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
