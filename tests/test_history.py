import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeswarm.history import History


class HistoryTests(unittest.TestCase):
    def test_open_skips_malformed_entries_and_keeps_valid_completion(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.jsonl"
                path.write_text(
                    '{"input": "git status", "timestamp": 1}\n'
                    "not json\n"
                    '{"input": "pytest -q", "timestamp": 2}\n'
                )
                history = History(path)

                self.assertTrue(await history.open())
                self.assertEqual(history.complete("g"), ["it"])
                self.assertEqual(history.complete("p"), ["ytest"])

        asyncio.run(scenario())

    def test_failed_append_does_not_create_a_phantom_history_entry(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                history = History(Path(directory) / "history.jsonl")
                await history.open()

                with patch("pathlib.Path.open", side_effect=OSError("disk full")):
                    self.assertFalse(await history.append("git status"))

                self.assertEqual(history.size, 0)
                self.assertEqual(history.complete("g"), [])

        asyncio.run(scenario())


    def test_navigation_survives_a_partially_written_history_line(self) -> None:
        """An interrupted append must not break the prompt history."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.jsonl"
                path.write_text(
                    '{"input": "git status", "timestamp": 1}\n'
                    '{"input": "pytest -q", "timestamp": 2}\n'
                    '{"input": "make ver\n'  # torn write
                )
                history = History(path)
                await history.open()

                self.assertEqual(history.size, 2)
                self.assertEqual(
                    (await history.get_entry(-1))["input"], "pytest -q"
                )
                self.assertEqual(
                    (await history.get_entry(-2))["input"], "git status"
                )
                with self.assertRaises(IndexError):
                    await history.get_entry(-3)

        asyncio.run(scenario())

    def test_entries_that_are_not_objects_are_skipped(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.jsonl"
                path.write_text(
                    "12345\n"
                    '{"timestamp": 2}\n'
                    '{"input": 7, "timestamp": 3}\n'
                    '{"input": "git log"}\n'
                )
                history = History(path)
                await history.open()

                self.assertEqual(history.size, 1)
                entry = await history.get_entry(-1)
                self.assertEqual(entry["input"], "git log")
                self.assertEqual(entry["timestamp"], 0.0)

        asyncio.run(scenario())

    def test_appended_entries_are_readable_without_reopening(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                history = History(Path(directory) / "history.jsonl")
                await history.open()
                self.assertTrue(await history.append("git status"))

                self.assertEqual(history.size, 1)
                self.assertEqual(
                    (await history.get_entry(-1))["input"], "git status"
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
