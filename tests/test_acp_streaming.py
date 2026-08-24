import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from wingmen.acp import messages
from wingmen.acp.agent import Agent
from wingmen.agent_schema import Agent as AgentData


class _MessageTarget:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, message: Any) -> bool:
        self.messages.append(message)
        return True


class ACPStreamingTests(unittest.TestCase):
    def make_agent(self) -> Agent:
        return Agent(
            Path.cwd(),
            cast(
                AgentData,
                {
                    "name": "Test",
                    "identity": "test.agent",
                    "run_command": {"*": "test-agent"},
                },
            ),
            None,
        )

    def test_stop_token_is_not_rendered_when_split_across_chunks(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        for text in ("Finished. [WING", "MEN:STOP]", ""):
            agent.rpc_session_update(
                "session-1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            )
        agent._flush_agent_response_display()

        rendered = "".join(
            message.text
            for message in target.messages
            if isinstance(message, messages.Update)
        )
        self.assertEqual(rendered, "Finished. ")
        self.assertNotIn("[WINGMEN:STOP]", rendered)
        self.assertEqual(agent.last_response, "Finished. [WINGMEN:STOP]")

    def test_gemini_mode_control_chunk_is_state_not_agent_output(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "[MODE_UPDATE] yolo"},
            },
        )

        self.assertEqual(agent.last_response, "")
        self.assertFalse(
            any(isinstance(message, messages.Update) for message in target.messages)
        )
        mode_updates = [
            message
            for message in target.messages
            if isinstance(message, messages.ModeUpdate)
        ]
        self.assertEqual(len(mode_updates), 1)
        self.assertEqual(mode_updates[0].current_mode, "yolo")

    def test_long_response_is_bounded_for_relay_and_ui(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        with patch("wingmen.acp.agent.MAX_RELAY_RESPONSE_CAPTURE_CHARS", 4), patch(
            "wingmen.acp.agent.MAX_AGENT_RESPONSE_CHARS", 5
        ):
            for text in ("hello", "!"):
                agent.rpc_session_update(
                    "session-1",
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                )

        rendered = "".join(
            message.text
            for message in target.messages
            if isinstance(message, messages.Update)
        )
        self.assertEqual(rendered.count("stopped rendering"), 1)
        self.assertTrue(rendered.startswith("hello"))
        self.assertIn("omitted the middle", agent.last_response)
        self.assertLessEqual(len(agent.last_response), 100)

    def test_long_thought_is_bounded_for_the_ui(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        with patch("wingmen.acp.agent.MAX_AGENT_THOUGHT_CHARS", 4):
            agent.rpc_session_update(
                "session-1",
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "abcde"},
                },
            )
            agent.rpc_session_update(
                "session-1",
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "ignored"},
                },
            )

        rendered = "".join(
            message.text
            for message in target.messages
            if isinstance(message, messages.Thinking)
        )
        self.assertEqual(rendered, "abcd\n\n[Wingmen stopped rendering the rest of this unusually long thought.]\n")

    def test_malformed_stream_updates_are_ignored(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": 1, "text": ["not text"]},
            },  # type: ignore[arg-type]
        )
        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "usage_update",
                "used": "many",
                "size": None,
            },  # type: ignore[arg-type]
        )

        self.assertEqual(target.messages, [])
        self.assertEqual(agent.last_response, "")

    def test_completed_tool_calls_are_not_retained(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "Read project",
                "status": "in_progress",
            },
        )
        self.assertIn("tool-1", agent.tool_calls)

        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool-1",
                "status": "completed",
            },
        )

        self.assertNotIn("tool-1", agent.tool_calls)
        self.assertIsInstance(target.messages[-1], messages.ToolCallUpdate)

    def test_tool_activity_messages_keep_their_source_agent(self) -> None:
        agent = self.make_agent()
        target = _MessageTarget()
        agent._message_target = target

        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "Read project",
                "status": "in_progress",
            },
        )
        agent.rpc_session_update(
            "session-1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool-1",
                "status": "completed",
            },
        )

        tool_messages = [
            message
            for message in target.messages
            if isinstance(message, (messages.ToolCall, messages.ToolCallUpdate))
        ]
        self.assertEqual(len(tool_messages), 2)
        self.assertTrue(
            all(getattr(message, "agent", None) is agent for message in tool_messages)
        )


if __name__ == "__main__":
    unittest.main()
