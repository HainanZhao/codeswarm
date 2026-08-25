import unittest
from pathlib import Path

from wingmen._path_match import MAX_MATCH_COMBINATIONS, PathFuzzySearch
from wingmen.widgets.path_search import MAX_SEARCH_RESULTS, PathSearch


class PathMatchTests(unittest.TestCase):
    def test_repeated_characters_have_a_bounded_search_space(self) -> None:
        matches = list(PathFuzzySearch()._match("aaaaaa", "a" * 20))

        self.assertEqual(len(matches), MAX_MATCH_COMBINATIONS)

    def test_ranked_results_are_filtered_sorted_and_bounded(self) -> None:
        matches = [
            (float(index), (index,), f"path-{index}") for index in range(50)
        ]
        matches.append((0.0, (), "no-match"))

        ranked = PathSearch.rank_matches(matches)

        self.assertEqual(len(ranked), MAX_SEARCH_RESULTS)
        self.assertEqual(ranked[0][2], "path-49")
        self.assertEqual(ranked[-1][2], "path-20")
        self.assertNotIn("no-match", {match[2] for match in ranked})

    def test_path_search_cache_is_intentionally_small(self) -> None:
        search = PathSearch(Path("."))

        self.assertLessEqual(search.search_cache.maxsize, 64)


if __name__ == "__main__":
    unittest.main()
