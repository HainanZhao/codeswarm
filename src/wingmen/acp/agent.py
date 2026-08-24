import asyncio

from collections import deque
from contextlib import suppress
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
from time import monotonic
from typing import Any, cast, NamedTuple
from copy import deepcopy
from math import floor
import rich.repr

from textual.content import Content
from textual.message import Message
from textual.message_pump import MessagePump


from wingmen import jsonrpc
import wingmen
from wingmen.agent_schema import Agent as AgentData
from wingmen.agent import AgentBase, AgentReady, AgentFail
from wingmen.acp import protocol
from wingmen.acp import api
from wingmen.acp.api import API
from wingmen.acp import messages
from wingmen.acp.prompt import build as build_prompt
from wingmen.db import DB, decode_session_meta
from wingmen import paths
from wingmen import constants
from wingmen.answer import Answer
from wingmen.acp.relay import STOP_TOKEN

PROTOCOL_VERSION = 1
MAX_AGENT_STDERR_CHARS = 32_000
MAX_AGENT_LOG_BYTES = 1 * 1024 * 1024
MAX_AGENT_RESPONSE_CHARS = 256 * 1024
MAX_RELAY_RESPONSE_CAPTURE_CHARS = 12_000
MAX_AGENT_THOUGHT_CHARS = 128 * 1024
MAX_FILE_READ_BYTES = 4 * 1024 * 1024
MAX_INFLIGHT_AGENT_REQUESTS = 64
LOG_TRUNCATED_MESSAGE = "[wingmen] log truncated after 1 MiB"
RESPONSE_TRUNCATED_MESSAGE = (
    "\n\n[Wingmen stopped rendering the rest of this unusually long response.]\n"
)
THOUGHT_TRUNCATED_MESSAGE = (
    "\n\n[Wingmen stopped rendering the rest of this unusually long thought.]\n"
)
OPERATING_INSTRUCTIONS = """\
## Wingmen operating instructions

- Do not speculate. If a request or its relevant context is unclear, inspect
  the available context or ask the user for clarification; do not guess.
- Treat a question as a request for an answer, not authorization to begin
  work. Only act on explicit user instructions.
- When work is explicitly requested, follow the stated scope and constraints
  precisely: do not omit requirements or add unrequested work.
"""
_BRACKETED_MODE_UPDATE = re.compile(
    r"^\s*\[\s*mode[\s_-]+(?:update|updated|changed)\s*\]\s+(\S+)\s*$",
    re.IGNORECASE,
)
_ENCLOSED_MODE_UPDATE = re.compile(
    r"^\s*\[\s*mode[\s_-]+(?:update|updated|changed)"
    r"\s*[:;]\s*([^\]\s]+)\s*\]\s*$",
    re.IGNORECASE,
)
_PLAIN_MODE_UPDATE = re.compile(
    r"^\s*mode[\s_-]+(?:update|updated|changed)"
    r"(?:\s*[:;]\s*|\s+)([^\s\]]+)\s*$",
    re.IGNORECASE,
)


def parse_mode_update_notice(text: str) -> str | None:
    """Return the mode ID from an adapter-generated control message."""
    for pattern in (
        _BRACKETED_MODE_UPDATE,
        _ENCLOSED_MODE_UPDATE,
        _PLAIN_MODE_UPDATE,
    ):
        if match := pattern.fullmatch(text):
            return match.group(1)
    return None


class Mode(NamedTuple):
    """An agent mode."""

    id: str
    name: str
    description: str | None


class ContextUsage(NamedTuple):
    """Context window usage."""

    used: int
    size: int
    cost: Cost | None = None

    @property
    def percentage_used(self) -> float:
        try:
            return (self.used / self.size) * 100.0
        except ZeroDivisionError:
            # Sanity check. If size is 0, then 100% is always used?
            return 100.0

    @property
    def percentage_display(self) -> str:
        return f"{floor(self.percentage_used * 10) / 10:.1f}%"


class Cost(NamedTuple):
    """A cost with associated currency."""

    amount: float
    currency: str

    def __str__(self) -> str:
        return f"{self:}"

    def __format__(self, _specifier: str) -> str:
        amount, currency = self
        return f"{amount:.2f} {currency}"


class TokenUsage(NamedTuple):
    """Tokens used for a single prompt (per-turn)."""

    total_tokens: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int | None
    cached_read_tokens: int | None
    cached_write_tokens: int | None


def generate_datetime_filename(
    prefix: str, suffix: str, datetime_format: str | None = None
) -> str:
    """Generate a filename which includes the current date and time.

    Useful for ensuring a degree of uniqueness when saving files.

    Args:
        prefix: Prefix to attach to the start of the filename, before the timestamp string.
        suffix: Suffix to attach to the end of the filename, after the timestamp string.
            This should include the file extension.
        datetime_format: The format of the datetime to include in the filename.
            If None, the ISO format will be used.
    """
    if datetime_format is None:
        dt = datetime.now().isoformat()
    else:
        dt = datetime.now().strftime(datetime_format)

    file_name_stem = f"{prefix} {dt}"
    for reserved in ' <>:"/\\|?*.':
        file_name_stem = file_name_stem.replace(reserved, "_")
    return file_name_stem + suffix


@rich.repr.auto
class Agent(AgentBase):
    """An agent that speaks the ACP protocol."""

    def __init__(
        self,
        project_root: Path,
        agent: AgentData,
        session_id: str | None,
        session_pk: int | None = None,
        persist: bool = True,
    ) -> None:
        """

        Args:
            project_root: Project root path.
            command: Command to launch agent.
            persist: Whether this agent should write its own row to the
                sessions table. Roster peers in a relay share the session
                owned by the first agent and must not create their own rows.
        """
        super().__init__(project_root)

        self._agent_data = agent
        self.session_id = session_id
        self._persist = persist

        self.server = jsonrpc.Server()
        self.server.expose_instance(self)

        self._agent_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task[str] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._failure_reported = False
        self.done_event = asyncio.Event()

        self.agent_capabilities: protocol.AgentCapabilities = {
            "loadSession": False,
            "promptCapabilities": {
                "audio": False,
                "embeddedContent": False,
                "image": False,
            },
        }
        self.auth_methods: list[protocol.AuthMethod] = []
        self.session_pk: int | None = session_pk
        self.tool_calls: dict[str, protocol.ToolCall] = {}
        self._message_target: MessagePump | None = None

        self._terminal_count: int = 0

        log_filename: str = generate_datetime_filename(f"{agent['name']}", ".txt")
        if log_path := os.environ.get("WINGMEN_LOG"):
            self._log_file_path = Path(log_path).resolve().absolute()
            with suppress(OSError):
                self._log_file_path.unlink(missing_ok=True)
        else:
            self._log_file_path = paths.get_log() / log_filename
        self._log_bytes = 0
        self._log_truncated = False

        self._token_usage: TokenUsage | None = None
        self._context_usage: ContextUsage | None = None
        self._last_response_parts: list[str] = []
        self._last_response_head = ""
        self._last_response_tail = ""
        self._last_response_truncated = False
        self._last_response_chars = 0
        self._response_displayed_chars = 0
        self._response_display_truncated = False
        self._thought_displayed_chars = 0
        self._thought_display_truncated = False
        self._operating_instructions_sent = False
        # Keep a short suffix while streaming so a stop token split across
        # ACP chunks never reaches the conversation UI.
        self._response_display_tail = ""

    @property
    def last_response(self) -> str:
        """Text emitted by the most recent ACP prompt."""
        if self._last_response_truncated:
            return (
                self._last_response_head
                + "\n\n[Wingmen omitted the middle of this response to protect context.]\n\n"
                + self._last_response_tail
            )
        return "".join(self._last_response_parts)

    def _remember_response(self, text: str) -> None:
        """Keep enough of an answer for relay context without retaining it all."""
        if not self._last_response_truncated:
            new_size = self._last_response_chars + len(text)
            if new_size <= MAX_RELAY_RESPONSE_CAPTURE_CHARS:
                self._last_response_parts.append(text)
                self._last_response_chars = new_size
                return
            head_size = MAX_RELAY_RESPONSE_CAPTURE_CHARS // 2
            previous = "".join(self._last_response_parts)
            self._last_response_head = (previous + text[:head_size])[:head_size]
            if len(text) >= head_size:
                self._last_response_tail = text[-head_size:]
            else:
                self._last_response_tail = (previous + text)[-head_size:]
            self._last_response_parts = []
            self._last_response_truncated = True
            return

        tail_size = MAX_RELAY_RESPONSE_CAPTURE_CHARS // 2
        self._last_response_tail = (self._last_response_tail + text)[-tail_size:]

    @property
    def command(self) -> str | None:
        """The command used to launch the agent, or `None` if there isn't one."""
        acp_command = wingmen.get_os_matrix(self._agent_data["run_command"])
        return acp_command

    @property
    def supports_load_session(self) -> bool:
        """Does the agent support loading sessions?"""
        return self.agent_capabilities.get("loadSession", False)

    def __rich_repr__(self) -> rich.repr.Result:
        yield self.project_root_path
        yield self.command

    def log(self, line: str, *, force: bool = False) -> None:
        """Write text to the agent log file.

        Args:
            line: Text to be logged.

        """
        if self._message_target is None:
            return
        if force:
            # Lifecycle records explain why a capped/noisy adapter stopped;
            # they must remain available even after ordinary logging ends.
            self._message_target.call_later(self._log, line)
            return
        if self._log_truncated:
            return

        line_bytes = len(line.rstrip().encode("utf-8", "replace")) + 1
        if self._log_bytes + line_bytes > MAX_AGENT_LOG_BYTES:
            self._log_truncated = True
            self._message_target.call_later(self._log, LOG_TRUNCATED_MESSAGE)
            return

        self._log_bytes += line_bytes
        self._message_target.call_later(self._log, line)

    async def _log(self, line: str) -> None:
        """Write text to the agent log file.

        Intended to be called from `log`

        Args:
            line: Text to be logged.
        """

        if self._message_target is None:
            return

        def write_log(log_file_path: Path, line: str):
            """Write log in a thread."""
            try:
                with log_file_path.open("at") as log_file:
                    log_file.write(f"{line.rstrip()}\n")
            except OSError:
                pass

        await asyncio.to_thread(write_log, self._log_file_path, line)

    def get_info(self) -> Content:
        agent_name = self._agent_data["name"]
        return Content(agent_name)

    async def start(self, message_target: MessagePump | None = None) -> None:
        """Start the agent."""
        self._message_target = message_target
        self._stopping = False
        self._failure_reported = False
        try:
            await asyncio.to_thread(
                self._log_file_path.parent.mkdir, parents=True, exist_ok=True
            )
        except OSError:
            pass
        self._agent_task = asyncio.create_task(self._run_agent())

    def send(self, request: jsonrpc.Request) -> None:
        """Send a request to the agent.

        This is called automatically, if you go through `self.request`.

        Args:
            request: JSONRPC request object.

        """
        if self._process is None:
            self.log("[error] Agent process isn't running")
            return

        self.log(f"[client] {request.body}")
        if (stdin := self._process.stdin) is not None:
            stdin.write(b"%s\n" % request.body_json)

    def request(self) -> jsonrpc.Request:
        """Create a request object."""
        return API.request(self.send)

    def post_message(self, message: Message) -> bool:
        """Post a message to the message target (the Conversation).

        Args:
            message: Message object.

        Returns:
            `True` if the message was posted successfully, or `False` if it wasn't.
        """
        if isinstance(message, AgentFail) and message.agent is None:
            # The conversation needs to know which roster member failed in
            # order to keep healthy peers usable. Attach it here so every
            # failure path (process launch, initialization, and later exit)
            # carries the same identity.
            message.agent = self
        if isinstance(message, (messages.SetModes, messages.ModeUpdate)) and message.agent is None:
            # Mode IDs belong to a particular ACP session. The conversation
            # may host several adapters with overlapping or incompatible mode
            # names, so never lose the source agent on the way to the UI.
            message.agent = self
        if isinstance(
            message,
            (messages.Update, messages.Thinking, messages.ToolCall, messages.ToolCallUpdate),
        ) and message.agent is None:
            # Conversation rendering must start a new stream when the relay
            # advances. Without the source, a peer's output or tool activity
            # can be appended to the previous agent's attributed turn.
            message.agent = self
        if isinstance(message, messages.AvailableCommandsUpdate) and message.agent is None:
            # Command catalogs belong to one ACP session. Preserve ownership
            # so relay commands can be routed back to the advertising agent.
            message.agent = self
        if isinstance(message, AgentFail):
            self._failure_reported = True
        if (message_target := self._message_target) is None:
            return False
        return message_target.post_message(message)

    def _post_agent_response_chunk(self, content_type: str, text: str) -> None:
        """Forward streamed text while keeping the relay sentinel internal."""
        combined = self._response_display_tail + text
        prefix_length = min(len(STOP_TOKEN) - 1, len(combined))
        tail_length = 0
        for length in range(prefix_length, 0, -1):
            if combined.endswith(STOP_TOKEN[:length]):
                tail_length = length
                break

        if tail_length:
            visible, self._response_display_tail = (
                combined[:-tail_length],
                combined[-tail_length:],
            )
        else:
            visible, self._response_display_tail = combined, ""

        self._post_visible_response(content_type, visible.replace(STOP_TOKEN, ""))

    def _post_visible_response(self, content_type: str, visible: str) -> None:
        """Post a response fragment after applying the per-turn display cap."""
        if not visible or self._response_display_truncated:
            return
        remaining = MAX_AGENT_RESPONSE_CHARS - self._response_displayed_chars
        if remaining <= 0:
            self._response_display_truncated = True
            self.post_message(messages.Update("text", RESPONSE_TRUNCATED_MESSAGE))
            return
        displayed = visible[:remaining]
        self._response_displayed_chars += len(displayed)
        self.post_message(messages.Update(content_type, displayed))
        if len(displayed) < len(visible):
            self._response_display_truncated = True
            self.post_message(messages.Update("text", RESPONSE_TRUNCATED_MESSAGE))

    def _flush_agent_response_display(self) -> None:
        """Render any non-sentinel suffix withheld by the stream filter."""
        visible = self._response_display_tail.replace(STOP_TOKEN, "")
        self._response_display_tail = ""
        if visible:
            self._post_visible_response("text", visible)

    def _post_agent_thought_chunk(self, content_type: str, text: str) -> None:
        """Forward a bounded amount of streamed reasoning to the UI."""
        if self._thought_display_truncated:
            return
        remaining = MAX_AGENT_THOUGHT_CHARS - self._thought_displayed_chars
        if remaining <= 0:
            self._thought_display_truncated = True
            self.post_message(messages.Thinking("text", THOUGHT_TRUNCATED_MESSAGE))
            return
        visible = text[:remaining]
        self._thought_displayed_chars += len(visible)
        self.post_message(messages.Thinking(content_type, visible))
        if len(visible) < len(text):
            self._thought_display_truncated = True
            self.post_message(messages.Thinking("text", THOUGHT_TRUNCATED_MESSAGE))

    @jsonrpc.expose("session/update")
    def rpc_session_update(
        self,
        sessionId: str,
        update: protocol.SessionUpdate,
        _meta: dict[str, Any] | None = None,
    ):
        """Agent requests an update.

        https://agentclientprotocol.com/protocol/schema
        """

        match update:
            case {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": type, "text": text},
            } if isinstance(type, str) and isinstance(text, str):
                if text:
                    self.post_message(messages.UserMessage(type, text))

            case {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": type, "text": text},
            } if isinstance(type, str) and isinstance(text, str):
                if mode_id := parse_mode_update_notice(text):
                    self.post_message(messages.ModeUpdate(mode_id))
                    return
                self._remember_response(text)
                self._post_agent_response_chunk(type, text)

            case {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": type, "text": text},
            } if isinstance(type, str) and isinstance(text, str):
                self._post_agent_thought_chunk(type, text)

            case {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
            } if isinstance(tool_call_id, str):
                self.tool_calls[tool_call_id] = update
                self.post_message(messages.ToolCall(update))
                self._forget_completed_tool_call(tool_call_id)

            case {"sessionUpdate": "plan", "entries": entries}:
                # Plans belonged to the removed planning UI. Keep accepting the
                # protocol update, but do not send an event with no consumer.
                pass

            case {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
            } if isinstance(tool_call_id, str):
                if tool_call_id in self.tool_calls:
                    current_tool_call = self.tool_calls[tool_call_id]
                    for key, value in update.items():
                        if value is not None:
                            current_tool_call[key] = value

                    self.post_message(
                        messages.ToolCallUpdate(deepcopy(current_tool_call), update)
                    )
                    self._forget_completed_tool_call(tool_call_id)
                else:
                    # The agent can send a tool call update, without previously sending the tool call *rolls eyes*
                    current_tool_call: protocol.ToolCall = {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tool_call_id,
                        "title": "Tool call",
                    }
                    for key, value in update.items():
                        if value is not None:
                            current_tool_call[key] = value

                    self.tool_calls[tool_call_id] = current_tool_call
                    self.post_message(messages.ToolCall(current_tool_call))
                    self._forget_completed_tool_call(tool_call_id)

            case {
                "sessionUpdate": "available_commands_update",
                "availableCommands": available_commands,
            } if isinstance(available_commands, list):
                self.post_message(messages.AvailableCommandsUpdate(available_commands))

            case {
                "sessionUpdate": "current_mode_update",
                "currentModeId": mode_id,
            } if isinstance(mode_id, str):
                self.post_message(messages.ModeUpdate(mode_id))

            case {
                "sessionUpdate": "usage_update",
                "used": used,
                "size": size,
            } if (
                isinstance(used, int)
                and not isinstance(used, bool)
                and isinstance(size, int)
                and not isinstance(size, bool)
            ):
                match update.get("cost"):
                    case {
                        "amount": amount,
                        "currency": currency,
                    } if isinstance(amount, (int, float)) and isinstance(currency, str):
                        self._context_usage = ContextUsage(
                            used, size, Cost(amount, currency)
                        )
                    case _:
                        self._context_usage = ContextUsage(used, size)
                self.update_status_line()

    def _forget_completed_tool_call(self, tool_call_id: str) -> None:
        """Discard tool payloads once no later ACP operation needs them."""
        tool_call = self.tool_calls.get(tool_call_id)
        if tool_call is not None and tool_call.get("status") in {
            "completed",
            "failed",
        }:
            self.tool_calls.pop(tool_call_id, None)

    def update_status_line(self) -> None:
        """Update the current status line."""

        if (usage := self._context_usage) is not None:
            status: list[Content] = []
            status.append(
                Content.assemble(
                    f"{usage.used / 1000:.1f}K",
                    " (",
                    (f"{usage.percentage_display}", "bold"),
                    ")",
                )
            )
            if (cost := usage.cost) is not None:
                status.append(Content.assemble((f"{cost}", "bold")))

            status_line = Content(" • ").join(status)
            self.post_message(messages.UpdateStatusLine(status_line))

    @jsonrpc.expose("session/request_permission")
    async def rpc_request_permission(
        self,
        sessionId: str,
        options: list[protocol.PermissionOption],
        toolCall: protocol.ToolCallUpdatePermissionRequest,
        _meta: dict | None = None,
    ) -> protocol.RequestPermissionResponse:
        """Agent requests permission to make a tool call.

        Args:
            sessionId: The session ID.
            options: A list of permission options (potential replies).
            toolCall: The tool or tools the agent is requesting permission to call.
            _meta: Optional meta information.

        Returns:
            The response to the permission request.
        """
        if not options:
            raise jsonrpc.InvalidParams("Permission request requires at least one option")
        result_future: asyncio.Future[Answer] = asyncio.Future()
        tool_call_id = toolCall["toolCallId"]

        permission_tool_call = toolCall.copy()
        permission_tool_call.pop("sessionUpdate", None)
        tool_call = cast(protocol.ToolCall, permission_tool_call)
        if tool_call_id in self.tool_calls:
            self.tool_calls[tool_call_id] |= tool_call
        else:
            self.tool_calls[tool_call_id] = deepcopy(tool_call)

        tool_call = deepcopy(self.tool_calls[tool_call_id])

        message = messages.RequestPermission(options, tool_call, result_future)
        self.post_message(message)
        await result_future
        ask_result = result_future.result()

        request_permission_outcome: protocol.OutcomeSelected = {
            "optionId": ask_result.id,
            "outcome": "selected",
        }
        result: protocol.RequestPermissionResponse = {
            "outcome": request_permission_outcome
        }
        return result

    def _project_file_path(self, path: str) -> Path:
        """Resolve an ACP filesystem path without allowing a workspace escape."""
        project_root = self.project_root_path.resolve()
        candidate = (project_root / path).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as error:
            raise jsonrpc.InvalidParams("File path is outside the project") from error
        return candidate

    @jsonrpc.expose("fs/read_text_file")
    def rpc_read_text_file(
        self,
        sessionId: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, str]:
        """Read a file in the project."""
        # https://agentclientprotocol.com/protocol/file-system#reading-files
        read_path = self._project_file_path(path)
        if line is not None and line < 1:
            raise jsonrpc.InvalidParams("line must be positive")
        if limit is not None and limit < 0:
            raise jsonrpc.InvalidParams("limit must not be negative")
        first_line = (line or 1) - 1
        selected = bytearray()
        try:
            with read_path.open("rb") as source:
                skipped_lines = 0
                while skipped_lines < first_line:
                    chunk = source.readline(MAX_FILE_READ_BYTES + 1)
                    if not chunk:
                        break
                    if chunk.endswith(b"\n"):
                        skipped_lines += 1

                selected_lines = 0
                while len(selected) < MAX_FILE_READ_BYTES:
                    if limit is not None and selected_lines >= limit:
                        break
                    remaining = MAX_FILE_READ_BYTES - len(selected)
                    chunk = source.readline(remaining + 1)
                    if not chunk:
                        break
                    selected.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
                    if chunk.endswith(b"\n"):
                        selected_lines += 1
        except IOError:
            return {"content": ""}
        text = selected.decode("utf-8", "ignore")
        if line is not None:
            text = text.rstrip("\n")
        return {"content": text}

    @jsonrpc.expose("fs/write_text_file")
    def rpc_write_text_file(self, sessionId: str, path: str, content: str) -> None:
        # https://agentclientprotocol.com/protocol/file-system#writing-files

        write_path = self._project_file_path(path)
        write_path.write_text(content, encoding="utf-8", errors="ignore")

    # https://agentclientprotocol.com/protocol/schema#createterminalrequest
    @jsonrpc.expose("terminal/create")
    async def rpc_terminal_create(
        self,
        command: str,
        _meta: dict | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[protocol.EnvVariable] | None = None,
        outputByteLimit: int | None = None,
        sessionId: str | None = None,
    ) -> protocol.CreateTerminalResponse:
        terminal_cwd = str(self._project_file_path(cwd or "."))
        # Assign a terminal id
        self._terminal_count = self._terminal_count + 1
        terminal_id = f"terminal-{self._terminal_count}"

        terminal_env = (
            {variable["name"]: variable["value"] for variable in env} if env else {}
        )
        result_future: asyncio.Future[bool] = asyncio.Future()
        self.post_message(
            messages.CreateTerminal(
                terminal_id,
                command=command,
                args=args,
                cwd=terminal_cwd,
                env=terminal_env,
                output_byte_limit=outputByteLimit,
                result_future=result_future,
            )
        )
        await result_future
        if not result_future.result():
            raise jsonrpc.JSONRPCError("Failed to create a terminal.")
        return {"terminalId": terminal_id}

    # https://agentclientprotocol.com/protocol/schema#killterminalcommandrequest
    @jsonrpc.expose("terminal/kill")
    def rpc_terminal_kill(
        self, sessionID: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.KillTerminalCommandResponse:
        self.post_message(messages.KillTerminal(terminalId))
        return {}

    # https://agentclientprotocol.com/protocol/schema#terminal%2Foutput
    @jsonrpc.expose("terminal/output")
    async def rpc_terminal_output(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.TerminalOutputResponse:
        from wingmen.widgets.terminal_tool import ToolState

        result_future: asyncio.Future[ToolState] = asyncio.Future()

        if not self.post_message(messages.GetTerminalState(terminalId, result_future)):
            raise RuntimeError("Unable to get terminal output")

        await result_future
        terminal_state = result_future.result()

        result: protocol.TerminalOutputResponse = {
            "output": terminal_state.output,
            "truncated": terminal_state.truncated,
        }
        if (return_code := terminal_state.return_code) is not None:
            result["exitStatus"] = {"exitCode": return_code}
        return result

    # https://agentclientprotocol.com/protocol/schema#terminal%2Frelease
    @jsonrpc.expose("terminal/release")
    def rpc_terminal_release(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.ReleaseTerminalResponse:
        self.post_message(messages.ReleaseTerminal(terminalId))
        return {}

    # https://agentclientprotocol.com/protocol/schema#terminal%2Fwait-for-exit
    @jsonrpc.expose("terminal/wait_for_exit")
    async def rpc_terminal_wait_for_exit(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.WaitForTerminalExitResponse:
        result_future: asyncio.Future[tuple[int, str | None]] = asyncio.Future()
        if not self.post_message(
            messages.WaitForTerminalExit(terminalId, result_future)
        ):
            raise RuntimeError("Unable to wait for terminal exit; no terminal found")

        await result_future
        return_code, signal = result_future.result()
        return {"exitCode": return_code, "signal": signal}

    async def _run_agent(self) -> None:
        """Task to communicate with the agent subprocess."""

        PIPE = asyncio.subprocess.PIPE
        env = os.environ.copy()
        env["WINGMEN_CWD"] = str(self.project_root_path)
        if self._agent_data["identity"] == "geminicli.com":
            # Gemini's in-process GCP telemetry exporter has produced noisy
            # shutdown and out-of-order metric failures in long ACP sessions.
            # Keep Wingmen's adapter process isolated from that optional path.
            env["GEMINI_TELEMETRY_ENABLED"] = "false"
        process_started_at = monotonic()

        if (command := self.command) is None:
            self.post_message(
                AgentFail("Failed to start agent; no run command for this OS")
            )
            return
        try:
            process = self._process = await asyncio.create_subprocess_shell(
                command,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                env=env,
                cwd=str(self.project_root_path),
                limit=10 * 1024 * 1024,
                start_new_session=os.name == "posix",
            )
        except Exception as error:
            self.post_message(AgentFail("Failed to start agent", details=str(error)))
            return

        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stderr is not None

        async def drain_stderr() -> str:
            """Keep the adapter's stderr pipe draining without retaining it all."""
            chunks: deque[str] = deque()
            size = 0
            while chunk := await process.stderr.read(4096):
                text = chunk.decode("utf-8", "replace")
                self.log(f"[stderr] {text.rstrip()}")
                chunks.append(text)
                size += len(text)
                while chunks and size > MAX_AGENT_STDERR_CHARS:
                    size -= len(chunks.popleft())
            return "".join(chunks)

        self._stderr_task = asyncio.create_task(drain_stderr())
        self._task = asyncio.create_task(self.run())

        tasks: set[asyncio.Task] = set()
        request_slots = asyncio.Semaphore(MAX_INFLIGHT_AGENT_REQUESTS)

        async def call_jsonrpc(request: jsonrpc.JSONObject | jsonrpc.JSONList) -> None:
            try:
                if (result := await self.server.call(request)) is not None:
                    result_json = json.dumps(result).encode("utf-8")
                    if process.stdin is not None:
                        process.stdin.write(b"%s\n" % result_json)
            finally:
                request_slots.release()
                if (task := asyncio.current_task()) is not None:
                    tasks.discard(task)

        try:
            while line := await process.stdout.readline():
                # This line should contain JSON, which may be:
                #   A) a JSONRPC request
                #   B) a JSONRPC response to a previous request
                if not line.strip():
                    continue

                try:
                    line_str = line.decode("utf-8")
                except Exception as error:
                    self.log(f"[error] Unable to decode utf-8 from agent: {error}")
                    continue

                self.log(f"[agent] {line_str}")
                try:
                    agent_data: jsonrpc.JSONType = json.loads(line_str)
                except Exception as error:
                    self.log(f"[error] failed to decode JSON from agent: {error}")
                    continue

                if isinstance(agent_data, dict):
                    if "result" in agent_data or "error" in agent_data:
                        API.process_response(agent_data)
                        continue

                elif isinstance(agent_data, list):
                    if not all(isinstance(datum, dict) for datum in agent_data):
                        self.log(f"[error] Agent sent invalid data: {agent_data!r}")
                        continue
                    if all(
                        isinstance(datum, dict) and ("result" in datum or "error" in datum)
                        for datum in agent_data
                    ):
                        API.process_response(agent_data)
                        continue

                if not isinstance(agent_data, dict):
                    self.log("[error] Invalid JSON from agent {agent_data!r}")
                    continue

                # Some adapters stream token-sized notifications. Limit concurrent
                # request handlers so a misbehaving peer receives normal pipe
                # backpressure instead of making an unbounded number of tasks.
                await request_slots.acquire()
                tasks.add(asyncio.create_task(call_jsonrpc(agent_data)))
                await asyncio.sleep(0)
        except (ValueError, asyncio.LimitOverrunError) as error:
            self._failure_reported = True
            self.post_message(
                AgentFail(
                    "Agent sent an oversized protocol message",
                    details=str(error),
                )
            )
            if process.returncode is None:
                try:
                    process.terminate()
                except OSError:
                    pass

        # Cancel all remaining tasks and wait for them to finish
        for task in tasks:
            task.cancel()

        # Wait for all tasks to complete cancellation
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # The protocol task may still be waiting for a response when the
        # subprocess exits.  Do not leave it behind as an orphaned task.
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

        # EOF on stdout means this adapter can no longer service requests,
        # including the deceptively common "clean" (zero exit status) case.
        # Wait to obtain a definitive return code before reporting it.
        return_code = await process.wait()
        runtime_seconds = monotonic() - process_started_at
        signal_number = -return_code if return_code < 0 else None
        self.log(
            "[process] "
            f"exit_code={return_code} "
            f"signal={signal_number if signal_number is not None else 'none'} "
            f"intentional={str(self._stopping).lower()} "
            f"runtime_seconds={runtime_seconds:.3f}",
            force=True,
        )
        stderr_task = self._stderr_task
        fail_details = await stderr_task if stderr_task is not None else ""
        if self._stderr_task is stderr_task:
            self._stderr_task = None
        if not self._stopping and not self._failure_reported:
            summary = (
                "Agent exited unexpectedly"
                if return_code == 0
                else f"Agent returned a failure code: [b]{return_code}"
            )
            exit_details = (
                f"Exit code: {return_code}\n"
                f"Signal: {signal_number if signal_number is not None else 'none'}\n"
                f"Runtime: {runtime_seconds:.3f} seconds"
            )
            if fail_details.strip():
                exit_details = f"{exit_details}\n\n{fail_details.strip()}"
            self.post_message(
                AgentFail(
                    summary,
                    details=exit_details,
                )
            )

        self._process = None

    async def stop(self) -> None:
        """Gracefully stop the process."""
        self._stopping = True
        if self.session_pk is not None:
            db = DB()
            await db.session_update_last_used(self.session_pk)

        process = self._process
        if process is not None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except OSError:
                pass

            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except OSError:
                    pass
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=1.0)

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in (self._task, self._agent_task, self._stderr_task)
            if task is not None and task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._agent_task = None
        self._stderr_task = None
        self._process = None

    async def run(self) -> None:
        """The main logic of the Agent."""
        if constants.ACP_INITIALIZE:
            try:
                async with asyncio.timeout(constants.ACP_INITIALIZE_TIMEOUT):
                    # Boilerplate to initialize comms
                    await self.acp_initialize()

                    if self.session_id is None:
                        # Create a new session
                        await self.acp_new_session()
                    else:
                        # Load existing session
                        if not self.agent_capabilities.get("loadSession", False):
                            self.post_message(
                                AgentFail(
                                    "Resume not supported",
                                    f"{self._agent_data['name']} does not currently support resuming sessions.",
                                    help="no_resume",
                                )
                            )
                            return
                        await self.acp_load_session()
                        if self.session_pk is not None:
                            db = DB()
                            await db.session_update_last_used(self.session_pk)
            except TimeoutError:
                self.post_message(
                    AgentFail(
                        "Timed out initializing agent",
                        f"No ACP response within {constants.ACP_INITIALIZE_TIMEOUT} seconds.",
                    )
                )
                return
            except jsonrpc.APIError as error:
                if isinstance(error.data, dict):
                    reason = str(
                        error.data.get("reason") or "Failed to initialize agent"
                    )
                    details = str(
                        error.data.get("details") or error.data.get("error") or ""
                    )
                else:
                    reason = "Failed to initialize agent"
                    details = ""
                self.post_message(AgentFail(reason, details))
                return
            except Exception as error:
                self.post_message(
                    AgentFail("Failed to initialize agent", details=str(error))
                )
                return

        self.post_message(AgentReady(self))

    async def send_prompt(self, prompt: str) -> str | None:
        """Send a prompt to the agent.

        !!! note
            This method blocks as it may defer to a thread to read resources.

        Args:
            prompt: Prompt text.
        """
        if not self._operating_instructions_sent:
            prompt = f"{OPERATING_INSTRUCTIONS}\n\nUser request:\n{prompt}"
            self._operating_instructions_sent = True
        prompt_content_blocks = await asyncio.to_thread(
            build_prompt, self.project_root_path, prompt
        )
        return await self.acp_session_prompt(prompt_content_blocks)

    async def acp_initialize(self):
        """Initialize agent."""
        with self.request():
            initialize_response = api.initialize(
                PROTOCOL_VERSION,
                {
                    "fs": {
                        "readTextFile": True,
                        "writeTextFile": True,
                    },
                    "terminal": True,
                },
                {
                    "name": wingmen.NAME,
                    "title": wingmen.TITLE,
                    "version": wingmen.get_version(),
                },
            )

        response = await initialize_response.wait()
        assert response is not None

        # Store agents capabilities
        if agent_capabilities := response.get("agentCapabilities"):
            self.agent_capabilities = agent_capabilities
        if auth_methods := response.get("authMethods"):
            self.auth_methods = auth_methods

    async def acp_new_session(self) -> None:
        """Create a new session."""
        with self.request():
            session_new_response = api.session_new(
                str(self.project_root_path),
                [],
            )
        response = await session_new_response.wait()
        assert response is not None
        self.session_id = response["sessionId"]

        if self.supports_load_session and self._persist:
            db = DB()
            session_name = "New Session"
            self.session_pk = await db.session_new(
                session_name,
                self._agent_data["name"],
                self._agent_data["identity"],
                self.session_id,
                protocol="acp",
                meta={
                    "cwd": str(self.project_root_path),
                    "agent_data": self._agent_data,
                },
            )

        if (modes := response.get("modes", None)) is not None:
            current_mode = modes["currentModeId"]
            available_modes = modes["availableModes"]
            modes_update = {
                mode["id"]: Mode(
                    mode["id"], mode["name"], mode.get("description", None)
                )
                for mode in available_modes
            }
            self.post_message(messages.SetModes(current_mode, modes_update))

    async def acp_load_session(self) -> None:
        assert self.session_id is not None, "Session id must be set"
        cwd = str(self.project_root_path)
        if self.session_pk is not None:
            db = DB()
            if (session := await db.session_get(self.session_pk)) is not None:
                meta = decode_session_meta(session["meta_json"])
                session_cwd = meta.get("cwd")
                if isinstance(session_cwd, str):
                    cwd = session_cwd
                agent_data = meta.get("agent_data")
                if isinstance(agent_data, dict):
                    self._agent_data = agent_data  # type: ignore[assignment]

        with self.request():
            session_load_response = api.session_load(cwd, [], self.session_id)
        response = await session_load_response.wait()

        if (modes := response.get("modes", None)) is not None:
            current_mode = modes["currentModeId"]
            available_modes = modes["availableModes"]
            modes_update = {
                mode["id"]: Mode(
                    mode["id"], mode["name"], mode.get("description", None)
                )
                for mode in available_modes
            }
            self.post_message(messages.SetModes(current_mode, modes_update))

    async def acp_session_prompt(
        self, prompt: list[protocol.ContentBlock]
    ) -> str | None:
        """Send the prompt to the agent.

        Returns:
            The stop reason.

        """
        self._last_response_parts = []
        self._last_response_head = ""
        self._last_response_tail = ""
        self._last_response_truncated = False
        self._last_response_chars = 0
        self._response_display_tail = ""
        self._response_displayed_chars = 0
        self._response_display_truncated = False
        self._thought_displayed_chars = 0
        self._thought_display_truncated = False
        with self.request():
            session_prompt = api.session_prompt(prompt, self.session_id)
        try:
            result = await session_prompt.wait()
        except jsonrpc.APIError as error:
            details = ""
            match error.data:
                case {"details": details}:
                    pass

            self.post_message(
                AgentFail(
                    "Failed to send prompt" or error.message,
                    (
                        str(details)
                        if details
                        else f"{self._agent_data['name']} returned an error"
                    ),
                )
            )
            return None
        except jsonrpc.JSONRPCError as error:
            self.post_message(
                AgentFail(
                    "Failed to send prompt" or error.message,
                    (error.message or f"{self._agent_data['name']} returned an error"),
                )
            )
            return None
        finally:
            self._flush_agent_response_display()

        assert result is not None
        # TODO: Where to display this?
        token_usage = result.get("usage")

        return result.get("stopReason")

    async def acp_session_set_mode(self, mode_id: str) -> str | None:
        """Update the current mode with the agent."""
        with self.request():
            response = api.session_set_mode(self.session_id, mode_id)
        try:
            await response.wait()
        except jsonrpc.APIError as error:
            match error.data:
                case {"details": details}:
                    return details if isinstance(details, str) else "Failed to set mode"
            return "Failed to set mode"
        else:
            return None

    async def set_mode(self, mode_id: str) -> str | None:
        return await self.acp_session_set_mode(mode_id)

    async def acp_session_cancel(self) -> bool:
        with self.request():
            response = api.session_cancel(self.session_id, {})
        try:
            await response.wait()
        except jsonrpc.APIError:
            # No-op if there is nothing to cancel
            return False
        return True

    async def cancel(self) -> bool:
        return await self.acp_session_cancel()
