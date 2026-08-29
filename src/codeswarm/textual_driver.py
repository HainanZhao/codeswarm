"""Textual driver tweaks for slow SSH and tmux terminals."""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition

from textual.drivers import linux_driver
from textual.drivers._writer_thread import WriterThread


class NonBlockingWriterThread(WriterThread):
    """Keep terminal backpressure off Textual's asyncio event loop.

    Textual's compositor output is a chain of state-dependent diffs. Retaining
    that chain while a terminal is blocked makes a remote client replay stale
    screens before it can display the current one. Keep one physical write in
    flight, discard incremental output produced while it is busy, and request
    one complete repaint to re-establish the terminal state.
    """

    def __init__(self, file, *, on_overflow: Callable[[], None] | None = None) -> None:
        super().__init__(file)
        self._condition = Condition()
        self._pending_parts: list[tuple[bool, str]] = []
        self._writing = False
        self._needs_snapshot = False
        self._stopping = False
        self._on_overflow = on_overflow

    def write(self, text: str) -> None:
        request_snapshot = False
        with self._condition:
            if self._stopping:
                return
            has_pending_frame = any(is_frame for is_frame, _ in self._pending_parts)
            if self._writing or has_pending_frame or self._needs_snapshot:
                if not self._needs_snapshot:
                    self._needs_snapshot = True
                    request_snapshot = True
            else:
                self._pending_parts.append((True, text))
                self._condition.notify()
        if request_snapshot and self._on_overflow is not None:
            self._on_overflow()

    def write_snapshot(self, text: str) -> None:
        """Queue a complete screen image, replacing any pending candidate."""
        with self._condition:
            if self._stopping:
                return
            for index, (is_frame, _pending_text) in enumerate(self._pending_parts):
                if is_frame:
                    self._pending_parts[index] = (True, text)
                    break
            else:
                self._pending_parts.append((True, text))
            self._needs_snapshot = False
            self._condition.notify()

    def write_control(self, text: str) -> None:
        """Preserve terminal mode controls outside compositor frames."""
        with self._condition:
            if self._stopping:
                return
            if self._pending_parts and not self._pending_parts[-1][0]:
                _is_frame, pending_control = self._pending_parts[-1]
                self._pending_parts[-1] = (False, pending_control + text)
            else:
                self._pending_parts.append((False, text))
            self._condition.notify()

    def run(self) -> None:
        """Write pending output without holding the scheduler lock."""
        write = self._file.write
        flush = self._file.flush
        while True:
            with self._condition:
                while not self._pending_parts and not self._stopping:
                    self._condition.wait()
                if not self._pending_parts and self._stopping:
                    break
                text = "".join(text for _is_frame, text in self._pending_parts)
                self._pending_parts.clear()
                self._writing = True
            write(text)
            with self._condition:
                self._writing = False
                should_flush = not self._pending_parts
                self._condition.notify_all()
            if should_flush:
                flush()
        flush()

    def stop(self) -> None:
        """Request shutdown without waiting forever on a stalled terminal."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self.join(timeout=0.5)


class ResponsiveLinuxDriver(linux_driver.LinuxDriver):
    """Linux driver whose output enqueue operation never blocks the UI."""

    def prepare_full_refresh(self) -> None:
        """Treat the next Textual frame as a self-contained screen image."""
        self._full_refresh_expected = True

    def begin_frame(self) -> None:
        """Begin collecting one atomic compositor update."""
        self._frame_parts = []

    def end_frame(self) -> None:
        """Schedule the complete compositor update collected since begin."""
        if self._frame_parts is None:
            return
        frame = "".join(self._frame_parts)
        self._frame_parts = None
        if frame:
            self._write_frame(frame)

    def write(self, data: str) -> None:
        assert self._writer_thread is not None, "Driver must be in application mode"
        if self._frame_parts is None:
            assert isinstance(self._writer_thread, NonBlockingWriterThread)
            self._writer_thread.write_control(data)
        else:
            self._frame_parts.append(data)

    def _write_frame(self, data: str) -> None:
        """Schedule one complete compositor update."""
        assert self._writer_thread is not None, "Driver must be in application mode"
        if self._full_refresh_expected:
            self._full_refresh_expected = False
            assert isinstance(self._writer_thread, NonBlockingWriterThread)
            self._writer_thread.write_snapshot(data)
        else:
            self._writer_thread.write(data)

    def start_application_mode(self) -> None:
        # LinuxDriver looks up WriterThread as a module global and does not
        # expose an injection point. Swap it only while the driver constructs
        # its writer, then restore the dependency for other Textual apps.
        original_writer = linux_driver.WriterThread
        app = self._app
        self._full_refresh_expected = False
        self._frame_parts: list[str] | None = None

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
