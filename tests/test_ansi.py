import contextlib
import io
import unittest

from codeswarm.ansi._ansi import ANSIStream


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


if __name__ == "__main__":
    unittest.main()
