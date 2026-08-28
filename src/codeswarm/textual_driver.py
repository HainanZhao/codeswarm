"""Textual driver tweaks for slow SSH and tmux terminals."""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Full, Queue

from textual.drivers import linux_driver
from textual.drivers._writer_thread import WriterThread


class NonBlockingWriterThread(WriterThread):
    """Keep terminal backpressure off Textual's asyncio event loop.

    Textual's default writer uses a queue capped at 30 frames and calls a
    blocking ``put`` when that queue is full. A slow SSH/tmux client can fill
    it, which blocks the UI thread and stops timers and input. We keep the
    queue bounded, but replace blocking backpressure with a full repaint: any
    stale queued diffs are discarded and the current screen is rendered again.
    """

    def __init__(self, file, *, on_overflow: Callable[[], None] | None = None) -> None:
        super().__init__(file)
        self._queue = Queue(30)
        self._on_overflow = on_overflow

    def write(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except Full:
            # Intermediate compositor diffs are no longer useful once the
            # terminal has fallen behind. Drop them and ask the app for a
            # complete repaint after this frame, restoring terminal state.
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
            if self._on_overflow is not None:
                self._on_overflow()
            self._queue.put_nowait(text)

    def stop(self) -> None:
        """Request shutdown without waiting forever on a stalled terminal."""
        try:
            self._queue.put_nowait(None)
        except Full:
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
            self._queue.put_nowait(None)
        self.join(timeout=0.5)


class ResponsiveLinuxDriver(linux_driver.LinuxDriver):
    """Linux driver whose output enqueue operation never blocks the UI."""

    def start_application_mode(self) -> None:
        # LinuxDriver looks up WriterThread as a module global and does not
        # expose an injection point. Swap it only while the driver constructs
        # its writer, then restore the dependency for other Textual apps.
        original_writer = linux_driver.WriterThread
        app = self._app

        class AppWriterThread(NonBlockingWriterThread):
            def __init__(self, file) -> None:
                super().__init__(
                    file,
                    on_overflow=lambda: app.call_later(
                        app._request_full_terminal_refresh
                    ),
                )

        linux_driver.WriterThread = AppWriterThread
        try:
            super().start_application_mode()
        finally:
            linux_driver.WriterThread = original_writer
