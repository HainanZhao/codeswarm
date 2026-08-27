import asyncio
import contextlib
import io
import unittest

from codeswarm.ansi._ansi import ANSIStream, Buffer, TerminalState


class ANSIStreamTests(unittest.TestCase):
    def test_unsupported_dcs_sequences_do_not_write_to_stdout(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            list(ANSIStream().feed("\x1bP1;2q\x1b\\"))

        self.assertEqual(output.getvalue(), "")

    def test_unsupported_line_attributes_do_not_write_to_stdout(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            list(ANSIStream().feed("\x1b#3"))

        self.assertEqual(output.getvalue(), "")


async def _noop_stdin(_text: str) -> bool:
    return True


def _state(width: int = 40, height: int = 10) -> TerminalState:
    state = TerminalState(_noop_stdin)
    state.update_size(width, height)
    return state


class ScrollbackBufferTests(unittest.TestCase):
    """The renderer indexes folded_lines[y] -> line_no -> lines[line_no].

    Every one of those indices has to stay consistent, so each test asserts
    the whole invariant rather than the single field it exercises.
    """

    def assert_indices_consistent(self, buffer: Buffer) -> None:
        self.assertEqual(len(buffer.line_to_fold), len(buffer.lines))
        flattened = []
        for line_no, record in enumerate(buffer.lines):
            self.assertEqual(
                buffer.line_to_fold[line_no],
                len(flattened),
                f"line_to_fold[{line_no}] does not point at its first fold",
            )
            for fold in record.folds:
                self.assertEqual(
                    fold.line_no,
                    line_no,
                    "a fold carries the wrong line number",
                )
            flattened.extend(record.folds)
        self.assertEqual(len(flattened), len(buffer.folded_lines))
        self.assertEqual(list(buffer.folded_lines), flattened)
        # What render_line actually does for every visible row.
        for fold in buffer.folded_lines:
            self.assertLess(fold.line_no, len(buffer.lines))

    def test_plain_lines_keep_their_indices_consistent(self) -> None:
        async def scenario() -> None:
            state = _state()
            await state.write("".join(f"line {n:03d}\r\n" for n in range(20)))
            self.assert_indices_consistent(state.scrollback_buffer)

        asyncio.run(scenario())

    def test_wrapped_lines_produce_several_folds(self) -> None:
        async def scenario() -> None:
            state = _state(width=20)
            await state.write("x" * 95 + "\r\n")
            buffer = state.scrollback_buffer
            self.assert_indices_consistent(buffer)
            self.assertGreater(
                len(buffer.folded_lines),
                len(buffer.lines),
                "a line longer than the width must fold",
            )

        asyncio.run(scenario())

    def test_trim_keeps_the_newest_lines(self) -> None:
        async def scenario() -> None:
            state = _state()
            await state.write("".join(f"line {n:03d}\r\n" for n in range(50)))
            buffer = state.scrollback_buffer
            before = len(buffer.lines)

            self.assertTrue(buffer.trim(10))

            self.assertEqual(len(buffer.lines), 10)
            self.assertLess(len(buffer.lines), before)
            # The tail is what matters: an error is at the end of a build log.
            self.assertIn("line 049", buffer.lines[-1].content.plain)
            self.assertIn("line 040", buffer.lines[0].content.plain)
            self.assert_indices_consistent(buffer)

        asyncio.run(scenario())

    def test_trim_reindexes_wrapped_lines(self) -> None:
        async def scenario() -> None:
            state = _state(width=20)
            # Alternate wrapped and short lines so folds per line differ.
            await state.write(
                "".join(
                    ("y" * 55 if n % 2 else f"short {n}") + "\r\n"
                    for n in range(30)
                )
            )
            buffer = state.scrollback_buffer
            self.assertTrue(buffer.trim(8))
            self.assertEqual(len(buffer.lines), 8)
            self.assert_indices_consistent(buffer)

        asyncio.run(scenario())

    def test_trim_below_the_cap_changes_nothing(self) -> None:
        async def scenario() -> None:
            state = _state()
            await state.write("".join(f"line {n}\r\n" for n in range(5)))
            buffer = state.scrollback_buffer
            folds = list(buffer.folded_lines)

            self.assertFalse(buffer.trim(100))
            self.assertFalse(buffer.trim(0))
            self.assertFalse(buffer.trim(-1))

            self.assertEqual(list(buffer.folded_lines), folds)
            self.assert_indices_consistent(buffer)

        asyncio.run(scenario())

    def test_trim_keeps_writing_afterwards(self) -> None:
        """Trimming mid-stream must not corrupt subsequent output."""

        async def scenario() -> None:
            state = _state()
            await state.write("".join(f"old {n}\r\n" for n in range(40)))
            buffer = state.scrollback_buffer
            self.assertTrue(buffer.trim(5))

            await state.write("".join(f"new {n}\r\n" for n in range(10)))

            self.assert_indices_consistent(buffer)
            self.assertIn("new 9", buffer.lines[-1].content.plain)

        asyncio.run(scenario())

    def test_trim_moves_the_cursor_with_the_content(self) -> None:
        async def scenario() -> None:
            state = _state()
            await state.write("".join(f"line {n}\r\n" for n in range(30)))
            buffer = state.scrollback_buffer
            self.assertTrue(buffer.trim(6))

            self.assertGreaterEqual(buffer.cursor_line, 0)
            self.assertLessEqual(buffer.cursor_line, len(buffer.folded_lines))
            self.assert_indices_consistent(buffer)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
