from typing import TypedDict
import asyncio
import json
from pathlib import Path
from time import time

import rich.repr

from codeswarm.complete import Complete


class HistoryEntry(TypedDict):
    """An entry in the history file."""

    input: str
    timestamp: float


def parse_entry(line: str) -> HistoryEntry | None:
    """Decode one history line, or `None` if the line was damaged.

    An interrupted append leaves a partial line behind; navigation must skip
    it rather than fail for every entry written before it.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    input = entry.get("input")
    if not isinstance(input, str):
        return None
    timestamp = entry.get("timestamp")
    return {
        "input": input,
        "timestamp": float(timestamp) if isinstance(timestamp, (int, float)) else 0.0,
    }


@rich.repr.auto
class History:
    """Manages a history file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[HistoryEntry] = []
        self._opened: bool = False
        self._current: str | None = None
        self.complete = Complete()

    def __rich_repr__(self) -> rich.repr.Result:
        yield self.path

    @property
    def current(self) -> str | None:
        return self._current

    @current.setter
    def current(self, current: str) -> None:
        self._current = current

    @property
    def size(self) -> int:
        return len(self._entries)

    async def open(self) -> bool:
        """Open the history file, read initial lines.

        Returns:
            `True` if lines were read, otherwise `False`.
        """
        if self._opened:
            return True

        def read_history() -> bool:
            """Read the history file (in a thread).

            Returns:
                `True` on success.
            """
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(exist_ok=True)
                with self.path.open("r") as history_file:
                    lines = history_file.readlines()
            except OSError:
                return False

            entries: list[HistoryEntry] = []
            inputs: list[str] = []
            for line in lines:
                if (entry := parse_entry(line)) is None:
                    continue
                entries.append(entry)
                inputs.append(entry["input"].split(" ", 1)[0])
            self._entries = entries
            self.complete.add_words(inputs)
            return True

        self._opened = await asyncio.to_thread(read_history)
        return self._opened

    async def append(self, input: str) -> bool:
        """Append a history entry.

        Args:
            text: Text in the history.
        Returns:
            `True` on success.
        """

        if not input:
            return True

        def write_line() -> bool:
            """Append a line to the history.

            Returns:
                `True` on success, `False` if write failed.
            """
            history_entry: HistoryEntry = {
                "input": input,
                "timestamp": time(),
            }
            line = json.dumps(history_entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as history_file:
                    history_file.write(f"{line}\n")
            except OSError:
                return False
            self._entries.append(history_entry)
            self.complete.add_words([input.split(" ")[0]])
            self._current = None
            return True

        if not self._opened:
            await self.open()

        return await asyncio.to_thread(write_line)

    async def get_entry(self, index: int) -> HistoryEntry:
        """Get a history entry via its index.

        Args:
            index: Index of entry. 0 for the last entry, negative indexes for previous entries.

        Returns:
            A history entry dict.
        """
        if index > 0:
            raise IndexError("History indices must be 0 or negative.")
        if not self._opened:
            await self.open()

        if index == 0:
            return {"input": self.current or "", "timestamp": time()}
        try:
            return self._entries[index]
        except IndexError:
            raise IndexError(f"No history entry at index {index}") from None
