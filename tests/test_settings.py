import unittest

from codeswarm.settings import Schema, Settings
from codeswarm.settings_schema import SCHEMA


class SettingsRuntimeTests(unittest.TestCase):
    def test_defaults_are_nested_and_runtime_keys_are_flattened(self) -> None:
        schema = Schema(SCHEMA)
        self.assertEqual(
            schema.defaults["ui"]["theme"], "codeswarm-black"  # type: ignore[index]
        )
        self.assertIn("ui.prompt_message", schema.keys)
        self.assertNotIn("shell.allow_commands", schema.keys)
        self.assertNotIn("ui", schema.keys)

    def test_get_set_and_startup_callback_use_runtime_schema_only(self) -> None:
        schema = Schema(SCHEMA)
        changes: list[tuple[str, object]] = []
        settings = Settings(schema, {}, lambda key, value: changes.append((key, value)))

        settings.set_all()
        self.assertEqual(settings.get("ui.prompt_message", str), "How can I help you today?")

        settings.set("launcher.roster", "claude.ai\nopenai.com")
        self.assertEqual(
            settings.get("launcher.roster", str), "claude.ai\nopenai.com"
        )
        self.assertTrue(settings.changed)
        self.assertIn(("launcher.roster", "claude.ai\nopenai.com"), changes)
