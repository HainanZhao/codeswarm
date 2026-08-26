import importlib
import unittest


class CodeSwarmIdentityTests(unittest.TestCase):
    def test_codeswarm_is_the_public_package_identity(self) -> None:
        package = importlib.import_module("codeswarm")

        self.assertEqual(package.NAME, "codeswarm")
        self.assertEqual(package.TITLE, "CodeSwarm")

    def test_codeswarm_cli_is_the_public_executable(self) -> None:
        package = importlib.import_module("codeswarm.cli")

        self.assertTrue(callable(package.main))
