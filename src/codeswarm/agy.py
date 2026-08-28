"""Native Google Antigravity CLI integration using its stream-JSON interface."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from copy import deepcopy
from contextlib import suppress
from pathlib import Path
from typing import Any

from textual.content import Content
from textual.message import Message
from textual.message_pump import MessagePump

from codeswarm.acp import messages
from codeswarm.acp.agent import OPERATING_INSTRUCTIONS, Mode
from codeswarm.acp.relay import STOP_TOKEN
from codeswarm.agent import AgentBase, AgentFail, AgentReady
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.db import DB, decode_session_meta


_MODES = {
    "default": Mode("default", "Agent Default", "Antigravity's standard mode."),
    "accept-edits": Mode(
        "accept-edits", "Accept Edits", "Apply edits without review prompts."
    ),
    "plan": Mode("plan", "Plan", "Plan-oriented execution."),
}

_TOOL_DETAIL_LIMIT = 1200


def _compact_tool_value(value: object, *, limit: int = _TOOL_DETAIL_LIMIT) -> str:
    """Make provider tool data readable without flooding the transcript."""
    if isinstance(value, dict):
        text = " ".join(
            f"{key}={_compact_tool_value(item, limit=limit)}"
            for key, item in value.items()
        )
    elif isinstance(value, (list, tuple)):
        text = " ".join(_compact_tool_value(item, limit=limit) for item in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _tool_detail_content(
    parameters: dict[str, Any], output: str | None = None
) -> list[dict[str, Any]]:
    lines = [
        f"{key}: {_compact_tool_value(value)}" for key, value in parameters.items()
    ]
    if output:
        lines.append(f"Output: {_compact_tool_value(output)}")
    if not lines:
        return []
    return [
        {
            "type": "content",
            "content": {"type": "text", "text": "\n".join(lines)},
        }
    ]


class AgyAgent(AgentBase):
    """Run Antigravity directly, without a third-party ACP bridge."""

    def __init__(
        self,
        project_root: Path,
        agent: AgentData,
        session_id: str | None,
        session_pk: int | None,
        *,
        persist: bool = True,
    ) -> None:
        super().__init__(project_root)
        self._agent_data = agent
        self.session_id = session_id
        self.session_pk = session_pk
        self._persist = persist
        self._message_target: MessagePump | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._startup_full_access = True
        self._current_mode = "default"
        self._operating_instructions_sent = False
        self._roster_introduction = ""
        self._last_response_parts: list[str] = []
        self._response_display_tail = ""
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._cancel_requested = False

    @property
    def last_response(self) -> str:
        return "".join(self._last_response_parts)

    @property
    def supports_startup_full_access(self) -> bool:
        return True

    @property
    def startup_full_access(self) -> bool:
        return self._startup_full_access

    def configure_startup_full_access(self, enabled: bool) -> None:
        self._startup_full_access = enabled

    @property
    def command(self) -> str:
        return "agy"

    def get_info(self) -> Content:
        return Content(self._agent_data["name"])

    def set_roster_introduction(self, introduction: str) -> None:
        self._roster_introduction = introduction.strip()

    def post_message(self, message: Message) -> bool:
        if isinstance(
            message,
            (messages.Update, messages.Thinking, messages.ToolCall, messages.ToolCallUpdate),
        ) and message.agent is None:
            message.agent = self  # type: ignore[assignment]
        if isinstance(message, (messages.SetModes, messages.ModeUpdate)) and message.agent is None:
            message.agent = self  # type: ignore[assignment]
        if isinstance(message, AgentFail) and message.agent is None:
            message.agent = self
        return (
            self._message_target.post_message(message)
            if self._message_target is not None
            else False
        )

    async def start(self, message_target: MessagePump | None = None) -> None:
        self._message_target = message_target
        if self.session_pk is not None:
            await DB().session_update_last_used(self.session_pk)
        self.post_message(messages.SetModes(self._current_mode, _MODES))
        self.post_message(AgentReady(self))

    async def send_prompt(self, prompt: str) -> str | None:
        self._cancel_requested = False
        first_prompt = not self._operating_instructions_sent
        if first_prompt:
            roster_context = (
                f"\n\n{self._roster_introduction}" if self._roster_introduction else ""
            )
            prompt = f"{OPERATING_INSTRUCTIONS}{roster_context}\n\nUser request:\n{prompt}"

        self._last_response_parts = []
        command = [
            "agy",
            "--print",
            prompt,
            "--print-timeout",
            "60m",
            "--output-format",
            "stream-json",
        ]
        if self.session_id is not None:
            command.extend(["--conversation", self.session_id])
        if self._startup_full_access:
            command.append("--dangerously-skip-permissions")
        if self._current_mode != "default":
            command.extend(["--mode", self._current_mode])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.project_root_path,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            self.post_message(AgentFail("Failed to start Antigravity", str(error)))
            return "cancelled"

        self._process = process
        stderr_task = asyncio.create_task(self._drain_stderr(process))
        result: dict[str, Any] | None = None
        try:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    candidate = await self._handle_event(event)
                    if candidate is not None:
                        result = candidate
            exit_code = await process.wait()
            stderr = await stderr_task
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
            if self._process is process:
                self._process = None

        if self._cancel_requested or exit_code == -signal.SIGINT:
            return "cancelled"

        if result is None or result.get("status") != "SUCCESS":
            details = str(result.get("error") if result else stderr.strip())
            self.post_message(
                AgentFail(
                    "Antigravity did not complete the turn",
                    details,
                    help="crashed",
                )
            )
            return "cancelled"
        if exit_code != 0:
            self.post_message(
                AgentFail(
                    "Antigravity exited with an error",
                    stderr.strip(),
                    help="crashed",
                )
            )
            return "cancelled"

        response = result.get("response")
        if isinstance(response, str) and response != self.last_response:
            suffix = response.removeprefix(self.last_response)
            if suffix:
                self._emit_response(suffix)
            elif not self.last_response:
                self._emit_response(response)
        self._flush_response_display()
        if first_prompt:
            self._operating_instructions_sent = True
        return "end_turn"

    async def _handle_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("event") == "init":
            conversation_id = event.get("conversation_id")
            if isinstance(conversation_id, str) and conversation_id:
                self.session_id = conversation_id
                await self._persist_session()
            return None
        if event.get("event") == "step_update":
            update = event.get("step_update")
            if not isinstance(update, dict):
                return None
            if update.get("step_type") == "agent_response":
                text = update.get("text_delta")
                if isinstance(text, str) and text:
                    self._emit_response(text)
            elif update.get("step_type") == "tool":
                self._handle_tool_update(update)
            return None
        if event.get("event") == "result":
            result = event.get("result")
            return result if isinstance(result, dict) else None
        return None

    def _emit_response(self, text: str) -> None:
        self._last_response_parts.append(text)
        combined = self._response_display_tail + text
        self._response_display_tail = ""
        if STOP_TOKEN in combined:
            visible, self._response_display_tail = combined.split(STOP_TOKEN, 1)
            if visible:
                self.post_message(messages.Update("text", visible))
            return
        keep = len(STOP_TOKEN) - 1
        if len(combined) <= keep:
            self._response_display_tail = combined
            return
        self.post_message(messages.Update("text", combined[:-keep]))
        self._response_display_tail = combined[-keep:]

    def _flush_response_display(self) -> None:
        if self._response_display_tail:
            self.post_message(messages.Update("text", self._response_display_tail))
            self._response_display_tail = ""

    def _handle_tool_update(self, update: dict[str, Any]) -> None:
        """Translate an official stream-JSON tool lifecycle into a tool card."""
        step_index = update.get("step_index")
        if not isinstance(step_index, int):
            return
        tool_id = f"agy-tool-{step_index}"
        name = update.get("tool_name")
        tool_name = name if isinstance(name, str) else "tool"
        tool_info = update.get("tool_info")
        info = tool_info if isinstance(tool_info, dict) else {}
        parameters = info.get("parameters")
        raw_input = parameters if isinstance(parameters, dict) else {}
        output = info.get("output")
        raw_output = {"output": output} if isinstance(output, str) else {}
        output_text = output if isinstance(output, str) else None
        state = update.get("state")
        if state == "ACTIVE":
            kind = "execute" if tool_name == "run_command" else "other"
            tool_call: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": tool_name.replace("_", " ").title(),
                "kind": kind,
                "status": "in_progress",
                "rawInput": raw_input,
                "content": _tool_detail_content(raw_input),
            }
            self._tool_calls[tool_id] = tool_call
            self.post_message(messages.ToolCall(deepcopy(tool_call)))
            return

        current = self._tool_calls.get(tool_id)
        if current is None:
            return
        if state == "DONE":
            current["status"] = "completed"
            if raw_output:
                current["rawOutput"] = raw_output
            current["content"] = _tool_detail_content(raw_input, output_text)
            tool_update: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "completed",
            }
            if raw_output:
                tool_update["rawOutput"] = raw_output
            self.post_message(messages.ToolCallUpdate(deepcopy(current), tool_update))
            self._tool_calls.pop(tool_id, None)

    async def _persist_session(self) -> None:
        if not self._persist or self.session_id is None:
            return
        meta: dict[str, object] = {
            "cwd": str(self.project_root_path),
            "agent_data": self._agent_data,
        }
        if self.session_pk is None:
            self.session_pk = await DB().session_new(
                "New Session",
                self._agent_data["name"],
                self._agent_data["identity"],
                self.session_id,
                protocol="agy",
                meta=meta,
            )
        else:
            db = DB()
            if session := await db.session_get(self.session_pk):
                previous_meta = decode_session_meta(session["meta_json"])
                previous_meta.update(meta)
                meta = previous_meta
            await db.session_update_owner(
                self.session_pk,
                agent=self._agent_data["name"],
                agent_identity=self._agent_data["identity"],
                agent_session_id=self.session_id,
                protocol="native",
                meta=meta,
            )

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        return (await process.stderr.read()).decode(errors="replace")

    async def set_mode(self, mode_id: str) -> str | None:
        if mode_id not in _MODES:
            return "Unsupported Antigravity mode"
        self._current_mode = mode_id
        self.post_message(messages.ModeUpdate(mode_id))
        return None

    async def cancel(self) -> bool:
        if self._process is None or self._process.returncode is not None:
            return False
        self._cancel_requested = True
        self._process.send_signal(signal.SIGINT)
        return True

    async def stop(self) -> None:
        await self.cancel()
