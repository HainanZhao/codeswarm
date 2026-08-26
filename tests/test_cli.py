import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from codeswarm.cli import main
from codeswarm.app import CodeSwarmApp
from codeswarm.screens.main import MainScreen
from codeswarm.widgets.conversation import Conversation
from codeswarm.widgets.prompt import PromptTextArea


class _FakeApp:
    instances: list["_FakeApp"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.ran = False
        self.instances.append(self)

    def run(self) -> None:
        self.ran = True


class CLIEntryPointTests(unittest.TestCase):
    def test_short_help_flag_is_supported(self) -> None:
        result = CliRunner().invoke(main, ["-h"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Commands:", result.output)

    def test_ctrl_c_is_the_only_application_quit_binding(self) -> None:
        bindings = {(binding.key, binding.action) for binding in CodeSwarmApp.BINDINGS}
        self.assertIn(("ctrl+c", "interrupt_or_quit"), bindings)
        self.assertFalse(any(key == "ctrl+q" for key, _action in bindings))

    def test_prompt_uses_standard_send_and_newline_keys(self) -> None:
        bindings = {binding.action: binding for binding in PromptTextArea.BINDINGS}

        self.assertEqual(bindings["submit"].key, "enter")
        self.assertEqual(bindings["newline"].key, "ctrl+j,shift+enter")
        self.assertNotIn("multiline_submit", bindings)

    def test_no_persistent_footer_or_command_palette_is_exposed(self) -> None:
        app_bindings = {binding.action: binding for binding in CodeSwarmApp.BINDINGS}
        screen_bindings = {binding.action: binding for binding in MainScreen.BINDINGS}
        conversation_bindings = {
            binding.action: binding for binding in Conversation.BINDINGS
        }

        self.assertTrue(app_bindings["interrupt_or_quit"].show)
        self.assertFalse(CodeSwarmApp.ENABLE_COMMAND_PALETTE)
        self.assertNotIn("toggle_help_panel", app_bindings)
        self.assertNotIn("cancel", conversation_bindings)
        self.assertTrue(conversation_bindings["toggle_pause"].show)
        self.assertEqual(conversation_bindings["toggle_pause"].key_display, "⌃⇧P")
        self.assertNotIn("focus_terminal", conversation_bindings)
        self.assertFalse(conversation_bindings["mode_switcher"].show)
        self.assertFalse(conversation_bindings["close_session"].show)
        self.assertNotIn("go_home", screen_bindings)
        self.assertTrue(all(not binding.show for binding in screen_bindings.values()))

    def test_run_entry_point_exits_cleanly_after_app_run(self) -> None:
        runner = CliRunner()
        with (
            tempfile.TemporaryDirectory() as config_dir,
            tempfile.TemporaryDirectory() as project_dir,
        ):
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": config_dir}), patch(
                "codeswarm.cli.CodeSwarmApp", _FakeApp
            ):
                result = runner.invoke(main, ["run", project_dir])

        self.assertIsNone(result.exception, result.output)
        self.assertTrue(_FakeApp.instances[-1].ran)

    def test_acp_entry_point_exits_cleanly_after_app_run(self) -> None:
        with patch("codeswarm.cli.CodeSwarmApp", _FakeApp):
            result = CliRunner().invoke(main, ["acp", "test-agent"])

        self.assertIsNone(result.exception, result.output)
        self.assertTrue(_FakeApp.instances[-1].ran)

    def test_acp_command_uses_a_safe_name_for_quoted_executables(self) -> None:
        with patch("codeswarm.cli.CodeSwarmApp", _FakeApp):
            result = CliRunner().invoke(
                main, ["acp", '"/tmp/My Agent" --stdio']
            )

        self.assertIsNone(result.exception, result.output)
        agent_data = _FakeApp.instances[-1].kwargs["agent_data"]
        self.assertEqual(agent_data["identity"], "my-agent.custom.batrachian.ai")
        self.assertEqual(agent_data["name"], "my-agent")

    def test_legacy_agent2_option_is_not_supported(self) -> None:
        result = CliRunner().invoke(main, ["run", "--agent2", "claude"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such option", result.output)
        self.assertIn("--agent2", result.output)

    def test_invalid_project_directory_is_a_cli_error(self) -> None:
        result = CliRunner().invoke(main, ["run", "/definitely/not/a/project"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Not a directory", result.output)


if __name__ == "__main__":
    unittest.main()
