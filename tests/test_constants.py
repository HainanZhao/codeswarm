import os
import unittest
from unittest.mock import patch

from wingmen.constants import _get_environ_int


class ConstantsTests(unittest.TestCase):
    def test_integer_setting_uses_default_for_missing_or_invalid_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_environ_int("WINGMEN_TEST_LIMIT", 10), 10)
        with patch.dict(os.environ, {"WINGMEN_TEST_LIMIT": "not-a-number"}):
            self.assertEqual(_get_environ_int("WINGMEN_TEST_LIMIT", 10), 10)

    def test_integer_setting_applies_both_bounds(self) -> None:
        with patch.dict(os.environ, {"WINGMEN_TEST_LIMIT": "-5"}):
            self.assertEqual(
                _get_environ_int("WINGMEN_TEST_LIMIT", 10, minimum=1, maximum=20),
                1,
            )
        with patch.dict(os.environ, {"WINGMEN_TEST_LIMIT": "50"}):
            self.assertEqual(
                _get_environ_int("WINGMEN_TEST_LIMIT", 10, minimum=1, maximum=20),
                20,
            )


if __name__ == "__main__":
    unittest.main()
