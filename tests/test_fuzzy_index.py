import asyncio
import unittest

from wingmen.fuzzy_index import FuzzyIndex


class FuzzyIndexTests(unittest.TestCase):
    def test_search_prioritizes_compact_filename_matches(self) -> None:
        async def scenario() -> None:
            index = FuzzyIndex()
            await index.update_paths(
                [
                    "src/tiny/every/single/thing.py",
                    "src/test_runner.py",
                ]
            )

            self.assertEqual((await index.search("test"))[0], "src/test_runner.py")

        asyncio.run(scenario())

    def test_search_excludes_out_of_order_character_matches(self) -> None:
        async def scenario() -> None:
            index = FuzzyIndex()
            await index.update_paths(["src/ab.py", "src/ba.py"])

            self.assertEqual(await index.search("ba"), ["src/ba.py"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
