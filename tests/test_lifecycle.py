import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from typing import cast

from textual import events
from textual.color import Color

from codeswarm.acp.agent import Agent, LOG_TRUNCATED_MESSAGE, MAX_INFLIGHT_AGENT_REQUESTS
from codeswarm.agent import AgentFail, AgentReady
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.app import CodeSwarmApp
from codeswarm.agents import AgentReadError
from codeswarm import messages
from codeswarm.acp import messages as acp_messages
from codeswarm.screens.store import StoreScreen
from codeswarm import jsonrpc
from codeswarm.widgets.conversation import Conversation


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


class _EOFStream:
    async def readline(self) -> bytes:
        return b""


class _EmptyErrorStream:
    async def read(self, size: int = -1) -> bytes:
        return b""


class _RequestStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        return b""


class _OversizedStream:
    async def readline(self) -> bytes:
        raise ValueError("Separator is not found, and chunk exceeds limit")


class _RequestProcess:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _RequestStream(lines)
        self.stdin = object()
        self.stderr = _EmptyErrorStream()

    async def wait(self) -> int:
        return 0


class _ExitedProcess:
    """Minimal subprocess facade for the ACP EOF path."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = _EOFStream()
        self.stdin = object()
        self.stderr = _EmptyErrorStream()

    async def wait(self) -> int:
        return self.returncode


class _OversizedProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = _OversizedStream()
        self.stdin = object()
        self.stderr = _EmptyErrorStream()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode or 0


class _StoppingSession:
    def __init__(self) -> None:
        self.stopped = False
        self.cancelled = False
        self.active_agents: list[AgentBase] = []

    async def cancel_active(self) -> bool:
        self.cancelled = True
        return True

    async def stop(self) -> None:
        self.stopped = True


class AgentLifecycleTests(unittest.TestCase):
    def test_default_theme_uses_teal_as_its_primary_accent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        primary = Color.parse(pilot.app.current_theme.primary)
                        secondary = Color.parse(pilot.app.current_theme.secondary)

                        for color in (primary, secondary):
                            self.assertGreater(color.g, color.b)
                            self.assertGreater(color.b, color.r)
                            self.assertGreater(color.g - color.r, 100)

        asyncio.run(scenario())

    def test_default_theme_uses_a_cool_avionics_semantic_palette(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test() as pilot:
                        theme = pilot.app.current_theme

                        self.assertEqual(theme.warning, "#A78BFA")
                        self.assertEqual(theme.error, "#EF4444")
                        self.assertEqual(theme.success, "#34D399")
                        self.assertEqual(theme.accent, "#67E8F9")

        asyncio.run(scenario())

    def test_first_prompt_includes_codeswarm_operating_instructions(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            agent.session_id = "new-session"
            agent.set_roster_introduction(
                "You are Test agent. Your collaborators are Claude and Gemini."
            )

            with (
                patch(
                    "codeswarm.acp.agent.build_prompt", return_value=[]
                ) as build_prompt,
                patch.object(agent, "acp_session_prompt", new=AsyncMock()),
            ):
                await agent.send_prompt("Inspect the failing test")
                await agent.send_prompt("Summarize it")

            first_prompt = build_prompt.call_args_list[0].args[1]
            self.assertIn("Do not speculate", first_prompt)
            self.assertIn("Your collaborators are Claude and Gemini", first_prompt)
            self.assertIn("explicit user instructions", first_prompt)
            self.assertIn("Inspect the failing test", first_prompt)
            self.assertEqual(build_prompt.call_args_list[1].args[1], "Summarize it")

        asyncio.run(scenario())

    def test_failed_first_prompt_retries_with_operating_instructions(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Gemini CLI",
                    "identity": "geminicli.com",
                    "run_command": {"*": "gemini --acp"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            agent.session_id = "new-session"
            agent.set_roster_introduction(
                "You are Gemini CLI. Your collaborator is Claude Code."
            )

            prompt = AsyncMock(
                side_effect=[
                    jsonrpc.APIError(429, "No capacity available", None),
                    "end_turn",
                    "end_turn",
                ]
            )
            with patch("codeswarm.acp.agent.build_prompt", return_value=[]) as build, patch.object(
                agent, "acp_session_prompt", new=prompt
            ):
                with self.assertRaises(jsonrpc.APIError):
                    await agent.send_prompt("Start the task")
                await agent.send_prompt("Start the task")
                await agent.send_prompt("Continue")

            first_attempt = build.call_args_list[0].args[1]
            retry = build.call_args_list[1].args[1]
            follow_up = build.call_args_list[2].args[1]
            for submitted in (first_attempt, retry):
                self.assertIn("CodeSwarm operating instructions", submitted)
                self.assertIn("Your collaborator is Claude Code", submitted)
            self.assertNotIn("CodeSwarm operating instructions", follow_up)

        asyncio.run(scenario())

    def test_oversized_protocol_line_reports_failure_and_reaps_agent(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            process = _OversizedProcess()
            posted: list[AgentFail] = []

            class Target:
                def post_message(self, message: AgentFail) -> bool:
                    posted.append(message)
                    return True

                def call_later(self, callback, *args) -> None:
                    pass

            agent._message_target = Target()  # type: ignore[assignment]
            with patch(
                "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ):
                await agent._run_agent()

            self.assertTrue(process.terminated)
            self.assertIsNone(agent._process)
            self.assertEqual(len(posted), 1)
            self.assertIn("oversized", posted[0].message)

        asyncio.run(scenario())

    def test_launch_reports_an_unreadable_agent_catalog(self) -> None:
        async def scenario() -> None:
            app = CodeSwarmApp(mode="store")
            app.notify = Mock()  # type: ignore[method-assign]
            with patch(
                "codeswarm.agents.read_agents",
                new=AsyncMock(side_effect=AgentReadError("broken catalog")),
            ):
                await app._launch_agent("claude.com")

            app.notify.assert_called_once()
            self.assertIn("Unable to read the agent catalog", app.notify.call_args.args[0])

        asyncio.run(scenario())

    def test_agent_request_dispatch_is_backpressured(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            request = b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n'
            process = _RequestProcess([request] * (MAX_INFLIGHT_AGENT_REQUESTS + 1))
            agent = Agent(Path.cwd(), data, None)
            agent._stopping = True
            release = asyncio.Event()
            calls = 0

            async def call(_request: object) -> None:
                nonlocal calls
                calls += 1
                await release.wait()
                return None

            with (
                patch(
                    "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                    new=AsyncMock(return_value=process),
                ),
                patch.object(agent, "run", new=AsyncMock()),
            ):
                agent.server.call = call  # type: ignore[method-assign]
                task = asyncio.create_task(agent._run_agent())
                for _ in range(100):
                    if calls == MAX_INFLIGHT_AGENT_REQUESTS:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(calls, MAX_INFLIGHT_AGENT_REQUESTS)
                release.set()
                await task

            self.assertEqual(calls, MAX_INFLIGHT_AGENT_REQUESTS + 1)

        asyncio.run(scenario())

    def test_terminal_alert_count_never_becomes_negative(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(mode="store").run_test() as pilot:
                        pilot.app.terminal_alert(False)
                        self.assertEqual(pilot.app.terminal_title_flash, 0)

        asyncio.run(scenario())

    def test_mounting_a_workspace_does_not_freeze_garbage_collection(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ), patch("gc.freeze") as freeze:
                    async with CodeSwarmApp(setup_prompt=False).run_test() as pilot:
                        await pilot.pause(0.1)

                    freeze.assert_not_called()

        asyncio.run(scenario())

    def test_replacing_a_workspace_stops_and_removes_existing_conversations(self) -> None:
        async def scenario() -> None:
            app = CodeSwarmApp(mode="store")
            old_session = app.session_tracker.new_session()
            conversation = Mock()
            conversation.shutdown = AsyncMock()
            app.register_conversation(conversation)

            with patch.object(app, "remove_mode") as remove_mode:
                await app.replace_live_conversations()

            conversation.shutdown.assert_awaited_once()
            remove_mode.assert_called_once_with(old_session)
            self.assertEqual(app.session_tracker.sessions, set())

        asyncio.run(scenario())

    def test_invalid_settings_file_uses_defaults_without_overwriting_it(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                config_path = Path(state_dir) / "codeswarm" / "codeswarm.json"
                config_path.parent.mkdir()
                config_path.write_text("not valid json", encoding="utf-8")
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(mode="store").run_test(size=(120, 40)) as app:
                        self.assertEqual(
                            app.app.settings.get("ui.theme", str), "codeswarm-black"
                        )
                        self.assertEqual(
                            app.app.current_theme.background, "#000000"
                        )

                self.assertEqual(config_path.read_text(encoding="utf-8"), "not valid json")

        asyncio.run(scenario())

    def test_unsupported_saved_theme_is_migrated_before_styles_load(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                config_path = Path(state_dir) / "codeswarm" / "codeswarm.json"
                config_path.parent.mkdir()
                config_path.write_text(
                    json.dumps({"ui": {"theme": "textual-dark"}}),
                    encoding="utf-8",
                )
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(mode="store").run_test(
                        size=(120, 40)
                    ) as app:
                        self.assertEqual(
                            app.app.settings.get("ui.theme", str),
                            "codeswarm-black",
                        )
                        self.assertEqual(app.app.current_theme.name, "codeswarm-black")

                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["ui"]["theme"], "codeswarm-black")

        asyncio.run(scenario())

    def test_legacy_thin_scrollbar_is_migrated_to_normal(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                config_path = Path(state_dir) / "codeswarm" / "codeswarm.json"
                config_path.parent.mkdir()
                config_path.write_text(
                    json.dumps({"ui": {"scrollbar": "thin"}}),
                    encoding="utf-8",
                )
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(mode="store").run_test(
                        size=(120, 40)
                    ) as app:
                        self.assertEqual(
                            app.app.settings.get("ui.scrollbar", str),
                            "normal",
                        )
                        self.assertEqual(app.app.scrollbar, "normal")

                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["ui"]["scrollbar"], "normal")

        asyncio.run(scenario())

    def test_resume_with_invalid_session_metadata_uses_agent_catalog(self) -> None:
        async def scenario() -> None:
            app = CodeSwarmApp(mode="store", project_dir=Path.cwd())
            catalog_agent = cast(
                AgentData,
                {
                    "identity": "test.agent",
                    "name": "Test agent",
                    "short_name": "test",
                    "run_command": {"*": "test-agent"},
                },
            )
            db = Mock()
            db.session_get = AsyncMock(
                return_value={"meta_json": "not valid json"}
            )
            launched: list[object] = []

            async def new_session_screen(factory):
                launched.append(factory())
                return "session-1"

            with (
                patch("codeswarm.app.DB", return_value=db),
                patch(
                    "codeswarm.agents.read_agents",
                    new=AsyncMock(return_value={"test.agent": catalog_agent}),
                ),
                patch.object(app, "replace_live_conversations", new=AsyncMock()),
                patch.object(app, "new_session_screen", side_effect=new_session_screen),
            ):
                await app._launch_agent("test.agent", session_pk=1)

            self.assertEqual(len(launched), 1)

        asyncio.run(scenario())

    def test_resume_with_invalid_agent_snapshot_uses_agent_catalog(self) -> None:
        async def scenario() -> None:
            app = CodeSwarmApp(mode="store", project_dir=Path.cwd())
            catalog_agent = cast(
                AgentData,
                {
                    "identity": "test.agent",
                    "name": "Catalog agent",
                    "short_name": "test",
                    "url": "https://example.test",
                    "protocol": "acp",
                    "type": "coding",
                    "author_name": "Example",
                    "author_url": "https://example.test",
                    "publisher_name": "Example",
                    "publisher_url": "https://example.test",
                    "description": "Catalog fallback",
                    "tags": [],
                    "help": "",
                    "run_command": {"*": "test-agent"},
                    "actions": {},
                },
            )
            db = Mock()
            db.session_get = AsyncMock(return_value={"meta_json": '{"agent_data": {}}'})
            launched: list[object] = []

            async def new_session_screen(factory):
                launched.append(factory())
                return "session-1"

            with (
                patch("codeswarm.app.DB", return_value=db),
                patch(
                    "codeswarm.agents.read_agents",
                    new=AsyncMock(return_value={"test.agent": catalog_agent}),
                ),
                patch.object(app, "replace_live_conversations", new=AsyncMock()),
                patch.object(app, "new_session_screen", side_effect=new_session_screen),
            ):
                await app._launch_agent("test.agent", session_pk=1)

            self.assertEqual(len(launched), 1)
            screen = launched[0]
            self.assertEqual(screen._agent["name"], "Catalog agent")

        asyncio.run(scenario())

    def test_conversation_shutdown_releases_agent_terminals(self) -> None:
        async def scenario() -> None:
            conversation = Conversation(Path.cwd())
            conversation.session = _StoppingSession()  # type: ignore[assignment]
            terminal = Mock()
            conversation.terminals["agent-terminal"] = terminal

            await conversation.shutdown()

            terminal.kill.assert_called_once()
            terminal.release.assert_called_once()
            self.assertEqual(conversation.terminals, {})
            self.assertTrue(conversation.session.stopped)

        asyncio.run(scenario())

    def test_stderr_is_drained_and_bounded_for_a_noisy_adapter(self) -> None:
        async def scenario() -> None:
            script = "import sys; sys.stderr.write('x' * 100000)"
            data = cast(
                AgentData,
                {
                    "name": "Noisy test agent",
                    "identity": "noisy.test.agent",
                    "run_command": {
                        "*": (
                            f"{shlex.quote(sys.executable)} -c "
                            f"{shlex.quote(script)}"
                        )
                    },
                },
            )
            agent = Agent(Path.cwd(), data, None)
            posted: list[AgentFail] = []

            class Target:
                def post_message(self, message: AgentFail) -> bool:
                    posted.append(message)
                    return True

                def call_later(self, callback, *args) -> None:
                    pass

            agent._message_target = Target()  # type: ignore[assignment]
            await asyncio.wait_for(agent._run_agent(), timeout=2)

            self.assertEqual(posted[0].message, "Agent exited unexpectedly")
            self.assertLessEqual(len(posted[0].details), 32_000)

        asyncio.run(scenario())

    def test_gemini_acp_disables_telemetry_in_its_subprocess(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Gemini CLI",
                    "identity": "geminicli.com",
                    "run_command": {"*": "gemini --acp"},
                },
            )
            project_path = Path("/tmp/codeswarm-gemini-project")
            agent = Agent(project_path, data, None)
            agent._stopping = True
            process = _ExitedProcess()

            with patch(
                "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ) as create_process:
                await agent._run_agent()

            launch_env = create_process.await_args.kwargs["env"]
            self.assertEqual(launch_env.get("GEMINI_TELEMETRY_ENABLED"), "false")
            self.assertEqual(launch_env.get("CODESWARM_CWD"), str(project_path))

        asyncio.run(scenario())

    def test_agent_uses_a_compatible_installed_nvm_node(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as nvm_dir:
                nvm_root = Path(nvm_dir)
                old_bin = nvm_root / "versions" / "node" / "v22.0.0" / "bin"
                new_bin = nvm_root / "versions" / "node" / "v22.18.0" / "bin"
                for bin_path, version in (
                    (old_bin, "v22.0.0"),
                    (new_bin, "v22.18.0"),
                ):
                    bin_path.mkdir(parents=True)
                    node = bin_path / "node"
                    node.write_text(f"#!/bin/sh\necho {version}\n")
                    node.chmod(0o755)

                data = cast(
                    AgentData,
                    {
                        "name": "Antigravity CLI",
                        "identity": "antigravity.google.com",
                        "run_command": {"*": "npx -y agy-acp"},
                        "minimum_node_version": "22.18.0",
                    },
                )
                agent = Agent(Path.cwd(), data, None)
                agent._stopping = True
                process = _ExitedProcess()

                with (
                    patch.dict(
                        os.environ,
                        {"NVM_DIR": str(nvm_root), "PATH": str(old_bin)},
                    ),
                    patch(
                        "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                        new=AsyncMock(return_value=process),
                    ) as create_process,
                ):
                    await agent._run_agent()

                launch_path = create_process.await_args.kwargs["env"]["PATH"]
                self.assertEqual(launch_path.split(os.pathsep)[0], str(new_bin))

        asyncio.run(scenario())

    def test_agent_defaults_to_declared_startup_full_access(self) -> None:
        data = cast(
            AgentData,
            {
                "name": "Antigravity CLI",
                "identity": "antigravity.google.com",
                "run_command": {"*": "npx -y agy-acp"},
                "full_access_startup_argument": "--dangerously-skip-permissions",
            },
        )
        agent = Agent(Path.cwd(), data, None)

        self.assertTrue(agent.supports_startup_full_access)
        self.assertTrue(agent.startup_full_access)
        self.assertEqual(
            agent.command,
            "npx -y agy-acp --dangerously-skip-permissions",
        )

        agent._agent_data = cast(  # type: ignore[attr-defined]
            AgentData,
            {
                "name": "Antigravity CLI",
                "identity": "antigravity.google.com",
                "run_command": {"*": "npx -y agy-acp"},
            },
        )
        self.assertTrue(agent.supports_startup_full_access)

        agent.configure_startup_full_access(False)

        self.assertFalse(agent.startup_full_access)
        self.assertEqual(agent.command, "npx -y agy-acp")

    def test_load_session_accepts_a_null_result(self) -> None:
        """`session/load` may resolve to null; resuming must not fail."""

        class _NullResponse:
            async def wait(self, timeout: float | None = None) -> None:
                return None

        class _ModesResponse:
            async def wait(self, timeout: float | None = None) -> dict:
                return {
                    "modes": {
                        "currentModeId": "default",
                        "availableModes": [{"id": "default", "name": "Default"}],
                    }
                }

        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            for response, expected_modes in (
                (_NullResponse(), None),
                (_ModesResponse(), "default"),
            ):
                with self.subTest(response=type(response).__name__):
                    agent = Agent(Path.cwd(), data, "agent-session-1")
                    posted: list[object] = []
                    with patch.object(
                        agent, "post_message", side_effect=posted.append
                    ), patch(
                        "codeswarm.acp.agent.api.session_load",
                        return_value=response,
                    ):
                        await agent.acp_load_session()

                    modes = [
                        message
                        for message in posted
                        if isinstance(message, acp_messages.SetModes)
                    ]
                    if expected_modes is None:
                        self.assertEqual(modes, [])
                    else:
                        self.assertEqual(len(modes), 1)
                        self.assertEqual(modes[0].current_mode, expected_modes)

        asyncio.run(scenario())

    def test_agent_exit_persists_code_runtime_and_failure_details(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            posted: list[AgentFail] = []

            class Target:
                def post_message(self, message: AgentFail) -> bool:
                    posted.append(message)
                    return True

                def call_later(self, callback, *args) -> None:
                    pass

            agent._message_target = Target()  # type: ignore[assignment]
            agent.log = Mock()  # type: ignore[method-assign]
            with patch(
                "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=_ExitedProcess(17)),
            ):
                await agent._run_agent()

            process_logs = [
                call.args[0]
                for call in agent.log.call_args_list
                if call.args and call.args[0].startswith("[process]")
            ]
            self.assertEqual(len(process_logs), 1)
            self.assertIn("exit_code=17", process_logs[0])
            self.assertIn("intentional=false", process_logs[0])
            self.assertRegex(process_logs[0], r"runtime_seconds=\d+\.\d{3}")
            self.assertEqual(len(posted), 1)
            self.assertIn("Exit code: 17", posted[0].details)
            self.assertIn("Runtime:", posted[0].details)

        asyncio.run(scenario())

    def test_agent_exit_record_survives_normal_log_truncation(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            agent._stopping = True
            agent._log_truncated = True

            class Target:
                def call_later(self, callback, *args) -> None:
                    pass

            agent._message_target = Target()  # type: ignore[assignment]
            with patch(
                "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=_ExitedProcess()),
            ):
                await agent._run_agent()

            self.assertTrue(
                any(
                    line.startswith("[process]")
                    for line in agent._log_pending
                )
            )

        asyncio.run(scenario())

    def test_agent_log_stops_after_its_size_limit(self) -> None:
        data = cast(
            AgentData,
            {
                "name": "Test agent",
                "identity": "test.agent",
                "run_command": {"*": "test-agent"},
            },
        )
        agent = Agent(Path.cwd(), data, None)
        flushes: list[object] = []

        class Target:
            def call_later(self, callback, *args) -> None:
                flushes.append(callback)

        agent._message_target = Target()  # type: ignore[assignment]
        with patch("codeswarm.acp.agent.MAX_AGENT_LOG_BYTES", 5):
            agent.log("1234")
            agent.log("56")
            agent.log("ignored")

        # Lines are buffered and flushed together, so the pending buffer is
        # where the size limit is now observable.
        self.assertEqual(agent._log_pending, ["1234", LOG_TRUNCATED_MESSAGE])
        # A burst schedules one flush, not one callback per line.
        self.assertEqual(len(flushes), 1)

    def test_clean_agent_process_exit_is_reported_as_a_failure(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            posted: list[AgentFail] = []

            class Target:
                def post_message(self, message: AgentFail) -> bool:
                    posted.append(message)
                    return True

                def call_later(self, callback, *args) -> None:
                    pass

            agent._message_target = Target()  # type: ignore[assignment]
            with patch(
                "codeswarm.acp.agent.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=_ExitedProcess()),
            ):
                await agent._run_agent()

            self.assertEqual([message.message for message in posted], ["Agent exited unexpectedly"])
            self.assertIs(posted[0].agent, agent)

        asyncio.run(scenario())

    def test_agent_failure_is_attributed_to_its_adapter(self) -> None:
        data = cast(
            AgentData,
            {
                "name": "Test agent",
                "identity": "test.agent",
                "run_command": {"*": "test-agent"},
            },
        )
        agent = Agent(Path.cwd(), data, None)
        posted: list[AgentFail] = []

        class Target:
            def post_message(self, message: AgentFail) -> bool:
                posted.append(message)
                return True

        agent._message_target = Target()  # type: ignore[assignment]
        agent.post_message(AgentFail("Failed to start"))

        self.assertEqual(len(posted), 1)
        self.assertIs(posted[0].agent, agent)

    def test_initialization_failure_never_reports_agent_ready(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            agent.acp_initialize = AsyncMock(
                side_effect=jsonrpc.APIError(-1, "initialize failed", {})
            )
            post_message = Mock()
            agent.post_message = post_message  # type: ignore[method-assign]

            await agent.run()

            posted = [call.args[0] for call in post_message.call_args_list]
            self.assertTrue(any(isinstance(message, AgentFail) for message in posted))
            self.assertFalse(any(isinstance(message, AgentReady) for message in posted))

        asyncio.run(scenario())

    def test_initialization_timeout_reports_failure_without_ready(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            agent.acp_initialize = AsyncMock(side_effect=TimeoutError)
            post_message = Mock()
            agent.post_message = post_message  # type: ignore[method-assign]

            await agent.run()

            posted = [call.args[0] for call in post_message.call_args_list]
            self.assertEqual(posted[0].message, "Timed out initializing agent")
            self.assertFalse(any(isinstance(message, AgentReady) for message in posted))

        asyncio.run(scenario())

    def test_ctrl_c_exit_stops_the_active_conversation_session(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        conversation = pilot.app.screen.query_one(Conversation)
                        session = _StoppingSession()
                        conversation.session = session  # type: ignore[assignment]

                        await pilot.press("ctrl+c")
                        await pilot.pause(0.2)
                        self.assertTrue(session.stopped)

        asyncio.run(scenario())

    def test_ctrl_c_interrupts_agent_work_before_quitting(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        conversation = pilot.app.screen.query_one(Conversation)
                        session = _StoppingSession()
                        conversation.session = session  # type: ignore[assignment]
                        conversation.turn = "agent"

                        await pilot.press("ctrl+c")
                        await pilot.pause(0.1)
                        self.assertTrue(session.cancelled)
                        self.assertFalse(session.stopped)

                        # Completing that turn clears its quit-confirmation
                        # window. A new turn's first Ctrl+C must cancel again,
                        # even when it starts within three seconds.
                        pilot.app.settings.set("notifications.turn_over", False)
                        await conversation.agent_turn_over("end_turn")
                        conversation.turn = "agent"
                        session.cancelled = False
                        await pilot.press("ctrl+c")
                        await pilot.pause(0.1)
                        self.assertTrue(session.cancelled)
                        self.assertFalse(session.stopped)

                        await pilot.press("ctrl+c")
                        await pilot.pause(0.1)
                        self.assertTrue(session.stopped)

        asyncio.run(scenario())

    def test_ctrl_c_finishes_a_stuck_turn_and_dispatches_the_queued_prompt(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        conversation = pilot.app.screen.query_one(Conversation)
                        agent = Mock()
                        agent.get_info.return_value = "Claude"
                        conversation.agent = agent
                        conversation.agent_ready = True
                        pilot.app.settings.set("notifications.turn_over", False)

                        first_prompt_started = asyncio.Event()
                        prompts: list[str] = []

                        async def send_prompt(prompt: str) -> str:
                            prompts.append(prompt)
                            if len(prompts) == 1:
                                first_prompt_started.set()
                                await asyncio.Event().wait()
                            return "end_turn"

                        conversation.session.send_prompt = send_prompt  # type: ignore[method-assign]
                        conversation.session.cancel_active = AsyncMock(return_value=True)  # type: ignore[method-assign]

                        await conversation.on_user_input_submitted(
                            messages.UserInputSubmitted("stuck task")
                        )
                        await asyncio.wait_for(first_prompt_started.wait(), timeout=1)
                        await conversation.on_user_input_submitted(
                            messages.UserInputSubmitted("queued follow-up")
                        )

                        await pilot.press("ctrl+c")
                        await pilot.pause(0.2)

                        self.assertEqual(prompts, ["stuck task", "queued follow-up"])
                        self.assertEqual(conversation.turn, "client")
                        self.assertIsNone(conversation._agent_status_timer)
                        self.assertIsNone(conversation._agent_started_at)
                        self.assertEqual(conversation.queued_messages, ())

        asyncio.run(scenario())

    def test_ctrl_c_copies_selected_conversation_text(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        screen = pilot.app.screen
                        with (
                            patch.object(
                                screen,
                                "get_selected_text",
                                return_value="text from the conversation",
                            ),
                            patch.object(screen, "clear_selection") as clear_selection,
                            patch.object(pilot.app, "copy_to_clipboard") as copy,
                        ):
                            await pilot.press("ctrl+c")
                            await pilot.pause(0.1)

                        copy.assert_called_once_with("text from the conversation")
                        clear_selection.assert_called_once_with()
                        self.assertTrue(pilot.app.is_running)

        asyncio.run(scenario())

    def test_finishing_a_mouse_selection_copies_it(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        screen = pilot.app.screen
                        with (
                            patch.object(
                                screen,
                                "get_selected_text",
                                return_value="mouse-selected conversation text",
                            ),
                            patch.object(pilot.app, "copy_to_clipboard") as copy,
                        ):
                            screen.post_message(events.TextSelected())
                            await pilot.pause(0.1)

                        copy.assert_called_once_with(
                            "mouse-selected conversation text"
                        )

        asyncio.run(scenario())

    def test_ctrl_c_interrupts_a_direct_shell_command_before_quitting(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        conversation = pilot.app.screen.query_one(Conversation)
                        terminal = Mock()
                        terminal.kill.return_value = True
                        conversation._local_shells.add(terminal)

                        await pilot.press("ctrl+c")
                        await pilot.pause(0.1)

                        terminal.kill.assert_called_once_with()
                        self.assertIs(
                            pilot.app.screen.query_one(Conversation), conversation
                        )
                        conversation._local_shells.clear()

        asyncio.run(scenario())

    def test_closing_a_session_stops_it_before_returning_to_store(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": state_dir, "XDG_DATA_HOME": state_dir},
                ):
                    async with CodeSwarmApp(setup_prompt=False).run_test(
                        size=(120, 40)
                    ) as pilot:
                        await pilot.pause(0.2)
                        conversation = pilot.app.screen.query_one(Conversation)
                        session = _StoppingSession()
                        conversation.session = session  # type: ignore[assignment]

                        conversation.post_message(
                            messages.SessionClose(pilot.app.screen.id or "")
                        )
                        await pilot.pause(0.2)

                        self.assertTrue(session.stopped)
                        self.assertIsInstance(pilot.app.screen, StoreScreen)

        asyncio.run(scenario())

    def test_stop_terminates_process_group_and_cancels_protocol_tasks(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            agent = Agent(Path.cwd(), data, None)
            process = _FakeProcess()
            agent._process = process  # type: ignore[assignment]
            agent._task = asyncio.create_task(asyncio.sleep(60))
            agent._agent_task = asyncio.create_task(asyncio.sleep(60))

            if os.name == "posix":
                with patch("codeswarm.acp.agent.os.killpg") as killpg:
                    await agent.stop()
                killpg.assert_called_once_with(process.pid, signal.SIGTERM)
            else:
                await agent.stop()
                self.assertTrue(process.terminated)

            self.assertIsNone(agent._process)
            self.assertIsNone(agent._task)
            self.assertIsNone(agent._agent_task)

        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_stop_terminates_the_adapter_process_group(self) -> None:
        async def scenario() -> None:
            data = cast(
                AgentData,
                {
                    "name": "Test agent",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            )
            child_code = (
                "import subprocess, time; "
                "child=subprocess.Popen(['sleep', '60']); "
                "print(child.pid, flush=True); time.sleep(60)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdout is not None
            child_pid = int((await process.stdout.readline()).decode())
            agent = Agent(Path.cwd(), data, None)
            agent._process = process

            try:
                await agent.stop()
                await asyncio.sleep(0.1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
