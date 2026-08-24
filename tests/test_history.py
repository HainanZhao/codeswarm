import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wingmen.history import History


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


if __name__ == "__main__":
    unittest.main()
