import threading
import time
import unittest

from codeswarm.textual_driver import NonBlockingWriterThread


class _BlockedFile:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def write(self, _text: str) -> int:
        self.started.set()
        self.release.wait(2)
        return 1

    def flush(self) -> None:
        return

    def fileno(self) -> int:
        return 1


class TextualDriverTests(unittest.TestCase):
    def test_output_enqueue_does_not_block_when_terminal_is_slow(self) -> None:
        output = _BlockedFile()
        overflowed = threading.Event()
        writer = NonBlockingWriterThread(output, on_overflow=overflowed.set)
        writer.start()
        try:
            writer.write("first")
            self.assertTrue(output.started.wait(1))

            started = time.monotonic()
            for _ in range(31):
                writer.write("second")
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
            self.assertTrue(overflowed.is_set())
        finally:
            output.release.set()
            writer.stop()


if __name__ == "__main__":
    unittest.main()
