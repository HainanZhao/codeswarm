"""ACP message handlers used by the conversation surface."""

from __future__ import annotations

from asyncio import Future
from functools import partial
from typing import Any, Callable

from textual import log, on, work
from textual.content import Content
from textual.css.query import NoMatches
from textual.widget import Widget

from codeswarm import messages
from codeswarm.acp import messages as acp_messages
from codeswarm.acp import protocol as acp_protocol
from codeswarm.acp.agent import parse_mode_update_notice
from codeswarm.answer import Answer
from codeswarm.slash_command import SlashCommand
from codeswarm.widgets.user_input import UserInput


def is_mode_update_notice(text: str) -> bool:
    """Identify adapter-generated mode notices that don't belong in the chat."""
    return parse_mode_update_notice(text) is not None


class ConversationACPHandlers(Widget):
    """Translate ACP events into conversation widgets and responses.

    Textual walks the full message-pump MRO when finding ``@on`` handlers, so
    this mixin keeps ACP dispatch attached to the conversation without
    enlarging its UI implementation.
    """

    @on(acp_messages.UpdateStatusLine)
    async def on_update_status_line(self, message: acp_messages.UpdateStatusLine):
        self.status = message.status_line

    @on(acp_messages.Update)
    async def on_acp_agent_message(self, message: acp_messages.Update):
        message.stop()
        await self.queue_agent_stream_fragment("response", message.text, message.agent)

    @on(acp_messages.UserMessage)
    async def on_acp_user_message(self, message: acp_messages.UserMessage):
        # Some adapters encode mode acknowledgements as synthetic user
        # messages instead of current_mode_update events. They are state, not
        # conversation content, and the selected mode is already shown below.
        if is_mode_update_notice(message.text):
            message.stop()
            return
        await self.flush_agent_stream()
        self._agent_thought = None
        self._agent_response = None
        message.stop()
        await self.post(UserInput(message.text))

    @on(acp_messages.Thinking)
    async def on_acp_agent_thinking(self, message: acp_messages.Thinking):
        message.stop()
        await self.queue_agent_stream_fragment("thought", message.text, message.agent)

    @on(acp_messages.RequestPermission)
    async def on_acp_request_permission(self, message: acp_messages.RequestPermission):
        message.stop()
        await self.flush_agent_stream()
        options = [
            Answer(option["name"], option["optionId"], option["kind"])
            for option in message.options
        ]
        self.request_permissions(message.result_future, options, message.tool_call)
        self._agent_response = None
        self._agent_thought = None

    @on(acp_messages.ToolCallUpdate)
    @on(acp_messages.ToolCall)
    async def on_acp_tool_call_update(
        self, message: acp_messages.ToolCall | acp_messages.ToolCallUpdate
    ):
        await self.flush_agent_stream()
        from codeswarm.widgets.agent_response import AgentToolActivity
        from codeswarm.widgets.tool_call import ToolCall

        follow_output = self.window.follow_output
        tool_call = message.tool_call
        source_agent = (
            getattr(message, "agent", None) or self._active_relay_agent or self.agent
        )
        self.clear_agent_thinking()
        self.begin_agent_output(source_agent)
        if tool_call.get("status", None) in (None, "completed"):
            self._agent_thought = None

        try:
            existing_tool_call = self.contents.query_one(f"#{message.tool_id}", ToolCall)
        except NoMatches:
            if source_agent is None:
                await self.post(ToolCall(tool_call, id=message.tool_id), new_block=True)
            else:
                agent_message = await self.ensure_agent_message(source_agent)
                await agent_message.tool_activity.add_tool_call(
                    ToolCall(tool_call, id=message.tool_id)
                )
        else:
            if existing_tool_call is not None:
                await existing_tool_call.update_tool_call(tool_call)
                try:
                    activity = existing_tool_call.query_ancestor(AgentToolActivity)
                except NoMatches:
                    pass
                else:
                    activity.refresh_preview()
        self._scroll_output_if_following(follow_output)

    @on(acp_messages.AvailableCommandsUpdate)
    async def on_acp_available_commands_update(
        self, message: acp_messages.AvailableCommandsUpdate
    ):
        slash_commands: list[SlashCommand] = []
        for available_command in message.commands:
            if not isinstance(available_command, dict):
                continue
            name = available_command.get("name")
            description = available_command.get("description")
            if not isinstance(name, str) or not isinstance(description, str):
                continue
            input = available_command.get("input", {}) or {}
            if not isinstance(input, dict):
                input = {}
            hint = input.get("hint")
            slash_commands.append(
                SlashCommand(
                    f"/{name}",
                    description,
                    hint=hint if isinstance(hint, str) else None,
                )
            )
        if message.agent is not None:
            self._agent_slash_commands[id(message.agent)] = slash_commands
            if self._mode_agent is message.agent or self._mode_agent is None:
                self.agent_slash_commands = slash_commands
                self.update_slash_commands()
        else:
            # Compatibility for adapters and tests predating source attribution.
            self.agent_slash_commands = slash_commands
            self.update_slash_commands()

    def get_terminal(self, terminal_id: str):
        """Get a live terminal widget by ACP id."""
        from codeswarm.widgets.terminal_tool import TerminalTool

        try:
            terminal = self.contents.query_one(f"#{terminal_id}", TerminalTool)
        except NoMatches:
            return None
        if terminal.released:
            return None
        return terminal

    @work
    @on(acp_messages.CreateTerminal)
    async def on_acp_create_terminal(self, message: acp_messages.CreateTerminal):
        if getattr(self, "discussion_mode", False):
            message.result_future.set_result(False)
            self.post_message(
                messages.Flash(
                    "Chat mode blocks agent terminal access",
                    style="warning",
                )
            )
            return
        from codeswarm.widgets.terminal_tool import Command, TerminalTool

        command = Command(
            message.command,
            message.args or [],
            message.env or {},
            message.cwd or str(self.project_path),
        )
        width = self.window.size.width - 5 - self.window.styles.scrollbar_size_vertical
        height = self.window.scrollable_content_region.height - 2
        terminal = TerminalTool(
            command,
            output_byte_limit=message.output_byte_limit,
            id=message.terminal_id,
            minimum_terminal_width=width,
        )
        self.terminals[message.terminal_id] = terminal
        terminal.display = False

        try:
            await terminal.start(width, height)
        except Exception as error:
            log(str(error))
            self.terminals.pop(message.terminal_id, None)
            message.result_future.set_result(False)
            return

        try:
            await self.post(terminal)
        except Exception:
            terminal.kill()
            terminal.release()
            self.terminals.pop(message.terminal_id, None)
            message.result_future.set_result(False)
        else:
            message.result_future.set_result(True)

    @on(acp_messages.KillTerminal)
    async def on_acp_kill_terminal(self, message: acp_messages.KillTerminal):
        if (terminal := self.get_terminal(message.terminal_id)) is not None:
            terminal.kill()

    @on(acp_messages.GetTerminalState)
    def on_acp_get_terminal_state(self, message: acp_messages.GetTerminalState):
        if (terminal := self.get_terminal(message.terminal_id)) is None:
            message.result_future.set_exception(
                KeyError(f"No terminal with id {message.terminal_id!r}")
            )
        else:
            message.result_future.set_result(terminal.tool_state)

    @on(acp_messages.ReleaseTerminal)
    def on_acp_terminal_release(self, message: acp_messages.ReleaseTerminal):
        # `get_terminal` looks the widget up in the transcript, and the
        # transcript is pruned, so a released terminal often has no widget
        # left to find. Fall back to the registry: its process still has to
        # be killed.
        terminal = self.get_terminal(message.terminal_id)
        if terminal is None:
            terminal = self.terminals.get(message.terminal_id)
        if terminal is not None:
            terminal.kill()
            terminal.release()
        # Dropped unconditionally. `terminals` is the ACP handle registry, not
        # the display owner, and the id is dead once released; when the widget
        # has already been pruned this registry is the last owner of the whole
        # parsed scrollback. Without this every command an agent ever ran was
        # retained for the life of the session, which is what made a long
        # session slow and then unresponsive.
        self.terminals.pop(message.terminal_id, None)

    @work
    @on(acp_messages.WaitForTerminalExit)
    async def on_acp_wait_for_terminal_exit(
        self, message: acp_messages.WaitForTerminalExit
    ):
        if (terminal := self.get_terminal(message.terminal_id)) is None:
            message.result_future.set_exception(
                KeyError(f"No terminal with id {message.terminal_id!r}")
            )
        else:
            return_code, signal = await terminal.wait_for_exit()
            message.result_future.set_result((return_code or 0, signal))

    async def set_mode(self, mode_id: str | None) -> None:
        """Set the current ACP agent mode."""
        if mode_id is not None and mode_id.startswith("codeswarm:mode:"):
            await self.set_shared_mode(mode_id)
            return
        agent = self._mode_agent or self.agent
        if agent is None:
            return
        if mode_id is None:
            self.current_mode = None
        elif mode_id not in self.modes:
            self.notify(
                "That mode is not available for the selected agent",
                title="Set Mode",
                severity="error",
            )
        elif (error := await agent.set_mode(mode_id)) is not None:
            self.notify(error, title="Set Mode", severity="error")
        elif (new_mode := self.modes.get(mode_id)) is not None:
            self.current_mode = new_mode

    @on(acp_messages.SetModes)
    async def on_acp_set_modes(self, message: acp_messages.SetModes):
        self.set_agent_modes(message.modes, message.current_mode, message.agent)
        await self._sync_desired_mode()

    @work
    async def request_permissions(
        self,
        result_future: Future[Answer],
        options: list[Answer],
        tool_call_update: acp_protocol.ToolCallUpdatePermissionRequest,
    ) -> None:
        kind = tool_call_update.get("kind", None)
        title = tool_call_update.get("title", "") or ""
        contents = tool_call_update.get("content", []) or []
        for content in contents:
            if content.get("type") != "diff":
                break
        else:
            kind = "edit"

        if kind == "edit":
            diffs: list[tuple[str, str, str | None, str]] = []
            for content in contents:
                match content:
                    case {
                        "type": "diff",
                        "oldText": old_text,
                        "newText": new_text,
                        "path": path,
                    }:
                        diffs.append((path, path, old_text, new_text))

            if diffs:
                from codeswarm.screens.permissions import PermissionsScreen

                self.app.terminal_alert()
                self.app.system_notify(
                    f"{self.agent_title} would like to write files",
                    title="Permissions request",
                    sound="question",
                )
                permissions_screen = PermissionsScreen(
                    options, diffs, agent_name=self.agent_title or "The Agent"
                )
                result = await self.app.push_screen_wait(
                    permissions_screen, mode=self.screen.id
                )
                self.app.terminal_alert(False)
                result_future.set_result(result)
                return

        from codeswarm.widgets.acp_content import ACPToolCallContent

        def answer_callback(answer: Answer) -> None:
            try:
                result_future.set_result(answer)
            except Exception:
                # Shutdown can race with an answer callback.
                pass

        tool_call_content = tool_call_update.get("content", None) or []
        self.ask(
            options,
            title or "",
            partial(ACPToolCallContent, tool_call_content)
            if tool_call_content
            else None,
            answer_callback,
        )

    def ask(
        self,
        options: list[Answer],
        title: str = "",
        get_content: Callable[[], Widget] | None = None,
        callback: Callable[[Answer], Any] | None = None,
    ) -> None:
        """Show a question from an ACP agent."""
        from codeswarm.widgets.question import Ask

        notify_title = f"[{self.agent_title}] {title}" if self.agent_title else title
        notify_message = "\n".join(f" • {option.text}" for option in options)
        self.app.system_notify(notify_message, title=notify_title, sound="question")
        self.prompt.ask(Ask(title, options, get_content, callback))
