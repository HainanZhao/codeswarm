import threading
import time
import unittest

from textual._ansi_sequences import SYNC_END, SYNC_START

from codeswarm.textual_driver import NonBlockingWriterThread, ResponsiveLinuxDriver


class _BlockedFile:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.started.set()
        self.release.wait(2)
        self.writes.append(text)
        return 1

    def flush(self) -> None:
        return

    def fileno(self) -> int:
        return 1


class _RecordingWriter:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> None:
        self.writes.append(text)


class TextualDriverTests(unittest.TestCase):
    def test_synchronized_terminal_update_is_scheduled_as_one_frame(self) -> None:
        output = _RecordingWriter()
        driver = object.__new__(ResponsiveLinuxDriver)
        driver._writer_thread = output  # type: ignore[assignment]
        driver._full_refresh_expected = False
        driver._frame_parts = None

        driver.begin_frame()
        driver.write(SYNC_START)
        driver.write("frame-body")
        driver.write(SYNC_END)
        driver.end_frame()

        self.assertEqual(output.writes, [f"{SYNC_START}frame-body{SYNC_END}"])

    def test_terminal_mode_controls_are_preserved_while_output_is_blocked(
        self,
    ) -> None:
        output = _BlockedFile()
        writer = NonBlockingWriterThread(output)
        writer.start()
        try:
            writer.write("frame")
            self.assertTrue(output.started.wait(1))

            writer.write_control("enable-mode")
            writer.write_control("disable-mode")
            output.release.set()
            writer.stop()

            self.assertEqual(
                output.writes,
                ["frame", "enable-modedisable-mode"],
            )
        finally:
            output.release.set()
            if writer.is_alive():
                writer.stop()

    def test_terminal_control_does_not_consume_full_repaint_marker(self) -> None:
        output = _BlockedFile()
        writer = NonBlockingWriterThread(output)
        writer.start()
        driver = object.__new__(ResponsiveLinuxDriver)
        driver._writer_thread = writer
        driver._full_refresh_expected = False
        driver._frame_parts = None
        try:
            writer.write("first")
            self.assertTrue(output.started.wait(1))

            driver.prepare_full_refresh()
            driver.write("\x1b]0;new title\x07")
            driver.begin_frame()
            driver.write(SYNC_START)
            driver.write("full-screen-frame")
            driver.write(SYNC_END)
            driver.end_frame()

            output.release.set()
            writer.stop()

            self.assertEqual(
                output.writes,
                [
                    "first",
                    f"\x1b]0;new title\x07{SYNC_START}full-screen-frame{SYNC_END}",
                ],
            )
        finally:
            output.release.set()
            if writer.is_alive():
                writer.stop()

    def test_slow_terminal_discards_incremental_frames_while_write_is_blocked(
        self,
    ) -> None:
        output = _BlockedFile()
        repaint_requested = threading.Event()
        writer = NonBlockingWriterThread(output, on_overflow=repaint_requested.set)
        writer.start()
        try:
            writer.write("first")
            self.assertTrue(output.started.wait(1))

            for index in range(12):
                writer.write(f"stale-{index}")

            self.assertTrue(repaint_requested.wait(1))
            output.release.set()
            writer.stop()

            self.assertEqual(output.writes, ["first"])
        finally:
            output.release.set()
            if writer.is_alive():
                writer.stop()

    def test_new_output_replaces_a_pending_snapshot_with_a_newer_snapshot(
        self,
    ) -> None:
        output = _BlockedFile()
        repaint_requests = 0
        repaint_requested = threading.Event()

        def request_repaint() -> None:
            nonlocal repaint_requests
            repaint_requests += 1
            repaint_requested.set()

        writer = NonBlockingWriterThread(output, on_overflow=request_repaint)
        writer.start()
        try:
            writer.write("first")
            self.assertTrue(output.started.wait(1))

            writer.write("stale-before-snapshot")
            self.assertTrue(repaint_requested.wait(1))
            writer.write_snapshot("older-snapshot")

            repaint_requested.clear()
            writer.write("stale-after-snapshot")
            self.assertTrue(repaint_requested.wait(1))
            writer.write_snapshot("latest-snapshot")

            output.release.set()
            writer.stop()

            self.assertEqual(repaint_requests, 2)
            self.assertEqual(output.writes, ["first", "latest-snapshot"])
        finally:
            output.release.set()
            if writer.is_alive():
                writer.stop()

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
