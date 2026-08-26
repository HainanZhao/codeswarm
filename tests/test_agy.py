import asyncio
import json
import unittest
from collections import deque
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from codeswarm import jsonrpc
from codeswarm.acp import messages
from codeswarm.agent import AgentFail, AgentReady
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.agy import AgyAgent


class _MessageTarget:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, message: Any) -> bool:
        self.messages.append(message)
        return True


class _Reader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = deque(lines)

    async def readline(self) -> bytes:
        return self._lines.popleft() if self._lines else b""

    async def read(self) -> bytes:
        content = b"".join(self._lines)
        self._lines.clear()
        return content


class _Process:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.stdout = _Reader(
            [(json.dumps(event) + "\n").encode() for event in events]
        )
        self.stderr = _Reader([])
        self.returncode: int | None = 0

    async def wait(self) -> int:
        return 0

    def send_signal(self, _signal: int) -> None:
        self.returncode = -2


class AgyAgentTests(unittest.TestCase):
    def make_agent(self) -> AgyAgent:
        return AgyAgent(
            Path.cwd(),
            cast(
                AgentData,
                {
                    "identity": "antigravity.google.com",
                    "name": "Antigravity CLI",
                    "short_name": "antigravity",
                    "run_command": {"*": "agy"},
                },
            ),
            None,
            None,
            persist=False,
        )

    def test_stream_result_finishes_turn_without_repeating_response(self) -> None:
        async def scenario() -> None:
            agent = self.make_agent()
            target = _MessageTarget()
            events: list[dict[str, object]] = [
                {
                    "event": "init",
                    "conversation_id": "conversation-1",
                    "init": {"permission_mode": "always-proceed"},
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "state": "ACTIVE",
                        "text_delta": "Native stream works",
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "state": "DONE",
                        "text_delta": "\n",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "conversation_id": "conversation-1",
                        "response": "Native stream works\n",
                    },
                },
            ]
            process = _Process(events)
            with patch(
                "codeswarm.agy.asyncio.create_subprocess_exec", return_value=process
            ) as create_process:
                await agent.start(target)  # type: ignore[arg-type]
                stop_reason = await agent.send_prompt("Check the stream")

            rendered = "".join(
                message.text
                for message in target.messages
                if isinstance(message, messages.Update)
            )
            self.assertEqual(stop_reason, "end_turn")
            self.assertEqual(agent.session_id, "conversation-1")
            self.assertEqual(agent.last_response, "Native stream works\n")
            self.assertEqual(rendered, "Native stream works\n")
            self.assertTrue(
                any(isinstance(message, AgentReady) for message in target.messages)
            )
            arguments = create_process.call_args.args
            self.assertIn("--output-format", arguments)
            self.assertIn("stream-json", arguments)
            self.assertIn("--print-timeout", arguments)
            self.assertIn("60m", arguments)
            self.assertIn("--dangerously-skip-permissions", arguments)

        asyncio.run(scenario())

    def test_ctrl_c_cancellation_does_not_report_antigravity_timeout(self) -> None:
        async def scenario() -> None:
            agent = self.make_agent()
            target = _MessageTarget()
            process = _Process([])

            def start_process(*_args: object, **_kwargs: object) -> _Process:
                process.returncode = -2
                agent._cancel_requested = True  # type: ignore[attr-defined]
                return process

            with patch(
                "codeswarm.agy.asyncio.create_subprocess_exec",
                side_effect=start_process,
            ):
                await agent.start(target)  # type: ignore[arg-type]
                stop_reason = await agent.send_prompt("Cancel this")

            self.assertEqual(stop_reason, "cancelled")
            self.assertFalse(
                any(isinstance(message, AgentFail) for message in target.messages)
            )

        asyncio.run(scenario())

    def test_malformed_stream_event_does_not_prevent_final_result(self) -> None:
        async def scenario() -> None:
            agent = self.make_agent()
            target = _MessageTarget()
            process = _Process(
                [
                    {"event": "step_update", "step_update": "malformed"},
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "conversation_id": "conversation-2",
                            "response": "Recovered.",
                        },
                    },
                ]
            )
            with patch(
                "codeswarm.agy.asyncio.create_subprocess_exec", return_value=process
            ):
                await agent.start(target)  # type: ignore[arg-type]
                stop_reason = await agent.send_prompt("Continue")

            rendered = "".join(
                message.text
                for message in target.messages
                if isinstance(message, messages.Update)
            )
            self.assertEqual(stop_reason, "end_turn")
            self.assertEqual(rendered, "Recovered.")

        asyncio.run(scenario())

    def test_stop_token_is_hidden_from_native_stream_output(self) -> None:
        async def scenario() -> None:
            agent = self.make_agent()
            target = _MessageTarget()
            process = _Process(
                [
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "agent_response",
                            "text_delta": "Done [CODESWARM:STOP]",
                        },
                    },
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "conversation_id": "conversation-stop",
                            "response": "Done [CODESWARM:STOP]",
                        },
                    },
                ]
            )
            with patch(
                "codeswarm.agy.asyncio.create_subprocess_exec", return_value=process
            ):
                await agent.start(target)  # type: ignore[arg-type]
                await agent.send_prompt("Finish")

            rendered = "".join(
                message.text
                for message in target.messages
                if isinstance(message, messages.Update)
            )
            self.assertEqual(rendered, "Done ")
            self.assertEqual(agent.last_response, "Done [CODESWARM:STOP]")

        asyncio.run(scenario())

    def test_tool_step_updates_existing_codeswarm_tool_card(self) -> None:
        async def scenario() -> None:
            agent = self.make_agent()
            target = _MessageTarget()
            process = _Process(
                [
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 2,
                            "step_type": "tool",
                            "state": "ACTIVE",
                            "tool_name": "run_command",
                            "tool_info": {
                                "parameters": {"CommandLine": "pwd"}
                            },
                        },
                    },
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 2,
                            "step_type": "tool",
                            "state": "DONE",
                            "tool_name": "run_command",
                            "tool_info": {
                                "parameters": {"CommandLine": "pwd"},
                                "output": "/workspace\n",
                            },
                        },
                    },
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "conversation_id": "conversation-3",
                            "response": "Done.",
                        },
                    },
                ]
            )
            with patch(
                "codeswarm.agy.asyncio.create_subprocess_exec", return_value=process
            ):
                await agent.start(target)  # type: ignore[arg-type]
                await agent.send_prompt("Run pwd")

            tool_call = next(
                message.tool_call
                for message in target.messages
                if isinstance(message, messages.ToolCall)
            )
            tool_update = next(
                message.tool_call
                for message in target.messages
                if isinstance(message, messages.ToolCallUpdate)
            )
            self.assertEqual(tool_call["kind"], "execute")
            self.assertEqual(tool_call["status"], "in_progress")
            self.assertEqual(tool_call["rawInput"], {"CommandLine": "pwd"})
            self.assertEqual(
                tool_call["content"][0]["content"]["text"],
                "CommandLine: pwd",
            )
            self.assertEqual(tool_update["status"], "completed")
            self.assertEqual(tool_update["rawOutput"], {"output": "/workspace\n"})
            self.assertIn("Output: /workspace", tool_update["content"][0]["content"]["text"])

        asyncio.run(scenario())
