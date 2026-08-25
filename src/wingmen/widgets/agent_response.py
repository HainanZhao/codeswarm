from datetime import datetime

from textual.app import ComposeResult
from textual import containers, events
from textual.binding import Binding
from textual.content import Content
from textual.reactive import var
from textual.widget import Widget
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from wingmen.conversation_markdown import ConversationMarkdown
from wingmen.widgets.non_selectable_label import NonSelectableLabel


SYSTEM = """\
If asked to output code add inline documentation in the google style format, and always use type hinting where appropriate.
Avoid using external libraries where possible, and favor code that writes output to the terminal.
When asked for a table do not wrap it in a code fence.
"""


def format_reply_timestamp(
    replied_at: datetime, *, now: datetime | None = None
) -> str:
    """Format a local reply time compactly, adding a date only when needed."""
    reference = now or datetime.now().astimezone()
    time_text = replied_at.strftime("%I:%M %p").lstrip("0")
    if replied_at.date() == reference.date():
        return time_text
    date_text = f"{replied_at.strftime('%b')} {replied_at.day}"
    if replied_at.year != reference.year:
        date_text = f"{date_text}, {replied_at.year}"
    return f"{date_text}, {time_text}"


class AgentResponse(ConversationMarkdown):
    block_cursor_offset = var(-1)

    def __init__(self, markdown: str | None = None) -> None:
        super().__init__(markdown)
        self._stream: MarkdownStream | None = None

    def block_cursor_clear(self) -> None:
        self.block_cursor_offset = -1

    def block_cursor_up(self) -> Widget | None:
        if self.block_cursor_offset == -1:
            if self.children:
                self.block_cursor_offset = len(self.children) - 1
            else:
                return None
        else:
            self.block_cursor_offset -= 1

        if self.block_cursor_offset == -1:
            return None
        try:
            return self.children[self.block_cursor_offset]
        except IndexError:
            self.block_cursor_offset = -1
            return None

    def block_cursor_down(self) -> Widget | None:
        if self.block_cursor_offset == -1:
            if self.children:
                self.block_cursor_offset = 0
            else:
                return None
        else:
            self.block_cursor_offset += 1
        if self.block_cursor_offset >= len(self.children):
            self.block_cursor_offset = -1
            return None
        try:
            return self.children[self.block_cursor_offset]
        except IndexError:
            self.block_cursor_offset = -1
            return None

    def get_cursor_block(self) -> Widget | None:
        if self.block_cursor_offset == -1:
            return None
        return self.children[self.block_cursor_offset]

    def block_select(self, widget: Widget) -> None:
        self.block_cursor_offset = self.children.index(widget)

    @property
    def stream(self) -> MarkdownStream:
        if self._stream is None:
            self._stream = self.get_stream(self)
        return self._stream

    async def append_fragment(self, fragment: str) -> None:
        self.loading = False
        await self.stream.write(fragment)


class AgentToolActivity(containers.VerticalGroup, can_focus=True):
    """Tool calls belonging to one attributed agent turn."""

    BINDINGS = [
        Binding("up", "previous_tool", "Previous tool", show=False),
        Binding("down", "next_tool", "Next tool", show=False),
        Binding("enter", "toggle_tool", "Tool details", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_index = -1
        self._tools: list[Widget] = []
        self._finalized = False
        self.summary = Static("", id="tool-activity-summary")
        self.summary.display = False

    def compose(self) -> ComposeResult:
        yield self.summary

    async def add_tool_call(self, tool_call: Widget) -> None:
        for previous_tool in self._tools:
            previous_tool.display = False
        self._finalized = False
        await self.mount(tool_call)
        self._tools.append(tool_call)
        self.selected_index = len(self._tools) - 1
        self.refresh_preview()
        if self.has_focus:
            self._show_selected_tool()
        else:
            self._show_summary()

    def refresh_preview(self) -> None:
        """Render a bounded one-line preview of the latest tool call."""
        selected = self.selected_tool
        tool_data = getattr(selected, "tool_call", None)
        raw_title = tool_data.get("title", "Tool call") if tool_data else "Tool call"
        title = " ".join(str(raw_title).split())[:160] or "Tool call"
        noun = "tool" if len(self._tools) == 1 else "tools"
        prefix = "SYS OK //" if self._finalized else "SYS //"
        self.summary.update(f"{prefix} {title} · {len(self._tools)} {noun}")

    @property
    def selected_tool(self) -> Widget | None:
        if self.selected_index == -1:
            return None
        try:
            return self._tools[self.selected_index]
        except IndexError:
            return None

    def _select(self, offset: int) -> None:
        if not self._tools:
            return
        selected = self.selected_tool
        if selected is not None:
            selected.display = False
        self.selected_index = (self.selected_index + offset) % len(self._tools)
        self._tools[self.selected_index].display = True

    def action_previous_tool(self) -> None:
        self._select(-1)

    def action_next_tool(self) -> None:
        self._select(1)

    def action_toggle_tool(self) -> None:
        from wingmen.widgets.tool_call import ToolCall

        selected = self.selected_tool
        if isinstance(selected, ToolCall) and selected.can_expand():
            selected.expanded = not selected.expanded

    def finalize(self, elapsed_seconds: int) -> None:
        """Collapse completed activity to a quiet count and duration."""
        if not self._tools:
            return
        minutes, seconds = divmod(max(0, elapsed_seconds), 60)
        duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        self._finalized = True
        self.refresh_preview()
        self.summary.update(f"{self.summary.render().plain} · {duration}")
        self._show_summary()

    def _show_summary(self) -> None:
        for tool in self._tools:
            tool.display = False
        self.summary.display = True

    def _show_selected_tool(self) -> None:
        self.summary.display = False
        selected = self.selected_tool
        if selected is not None:
            from wingmen.widgets.tool_call import ToolCall

            if isinstance(selected, ToolCall):
                selected.expanded = False
            selected.display = True

    def on_focus(self) -> None:
        self._show_selected_tool()

    def on_blur(self) -> None:
        self._show_summary()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()


class AgentMessage(containers.Vertical):
    """One agent reply with flight-dashboard attribution and response content."""

    DEFAULT_CLASSES = "block"

    def __init__(
        self,
        response: AgentResponse | None = None,
        *,
        source_agent: object,
        speaker: str,
        timestamp: str,
        tone_index: int,
    ) -> None:
        super().__init__()
        self.source_agent = source_agent
        self.response = response
        self.tool_activity = AgentToolActivity()
        self.tone_class = f"-agent-tone-{tone_index % 4}"
        self.add_class(self.tone_class)
        self.header = Content.assemble(
            (
                speaker,
                f"$agent-tone-{tone_index % 4} bold",
            ),
            (f" · {timestamp}", "dim"),
        )

    def compose(self) -> ComposeResult:
        yield NonSelectableLabel(
            self.header,
            id="agent-message-header",
            classes=self.tone_class,
        )
        if self.response is not None:
            yield self.response
        yield self.tool_activity

    async def add_response(self, response: AgentResponse) -> None:
        """Mount the response before the turn's trailing tool history."""
        self.response = response
        await self.mount(response, before=self.tool_activity)

    def finalize(self, elapsed_seconds: int) -> None:
        self.tool_activity.finalize(elapsed_seconds)

    def block_cursor_clear(self) -> None:
        if self.response is not None:
            self.response.block_cursor_clear()

    def block_cursor_up(self) -> Widget | None:
        return None if self.response is None else self.response.block_cursor_up()

    def block_cursor_down(self) -> Widget | None:
        return None if self.response is None else self.response.block_cursor_down()

    def get_cursor_block(self) -> Widget | None:
        return None if self.response is None else self.response.get_cursor_block()

    def block_select(self, widget: Widget) -> None:
        if self.response is not None:
            if widget is self.response:
                # A click on blank response space targets the Markdown
                # container, not one of its selectable rendered blocks.
                self.response.block_cursor_clear()
                return
            if self.response in widget.ancestors:
                self.response.block_select(widget)
            else:
                # Headers and tool history belong to the attributed turn,
                # but are not selectable response content.
                self.response.block_cursor_clear()

    def get_block_content(self, destination: str) -> str | None:
        if self.response is None:
            return None
        return self.response.get_block_content(destination)
