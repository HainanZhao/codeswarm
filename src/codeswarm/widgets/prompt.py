from __future__ import annotations

from pathlib import Path
import shlex
from typing import TYPE_CHECKING, Callable, Self

from textual import on
from textual.reactive import var, Initialize
from textual.app import ComposeResult

from textual.actions import SkipAction
from textual.binding import Binding

from textual.content import Content
from textual import getters
from textual.message import Message
from textual.widgets import Button, OptionList, TextArea, Label
from textual import containers
from textual.widget import Widget
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection
from textual import events

from codeswarm.app import CodeSwarmApp
from codeswarm import messages
from codeswarm.widgets.highlighted_textarea import HighlightedTextArea
from codeswarm.widgets.condensed_path import CondensedPath
from codeswarm.widgets.path_search import PathSearch
from codeswarm.widgets.question import Ask, Question
from codeswarm.widgets.slash_complete import SlashComplete
from codeswarm.messages import UserInputSubmitted
from codeswarm.slash_command import SlashCommand
from codeswarm.prompt.extract import extract_paths_from_prompt
from codeswarm.mode_policy import MODE_ORDER

if TYPE_CHECKING:
    from codeswarm.acp.agent import Mode


class PromptPicker(OptionList):
    """Shared dismissal behaviour for the prompt's overlay pickers.

    Both pickers hide via a `:blur` rule, so dismissing one means removing
    focus from it. Doing that alone leaves the screen with no focused widget
    and the composer unable to accept typing, so focus is handed back to the
    prompt.
    """

    def dismiss_picker(self) -> None:
        """Hide this picker and return focus to the composer."""
        self.blur()
        # Resolved by selector rather than by class: PromptTextArea is defined
        # further down this module.
        text_area = self.screen.query_one_optional("PromptTextArea", Widget)
        if text_area is not None:
            text_area.focus()


class ModeSwitcher(PromptPicker):
    BINDING_GROUP_TITLE = "Mode switcher"
    BINDINGS = [Binding("escape", "dismiss", "Dismiss mode switcher")]

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected):
        self.post_message(messages.ChangeMode(event.option_id))
        self.dismiss_picker()

    def action_dismiss(self):
        self.dismiss_picker()


class CollaborationSwitcher(PromptPicker):
    """Compact picker for CodeSwarm's collaboration routing strategy."""

    BINDING_GROUP_TITLE = "Collaboration switcher"
    BINDINGS = [Binding("escape", "dismiss", "Dismiss collaboration switcher")]

    def __init__(self) -> None:
        super().__init__(
            Option(
                Content.assemble(("Roster", "bold"), (" · Sequential relay", "dim")),
                id="roster",
            ),
            Option(
                Content.assemble(("Manual", "bold"), (" · Pinned agent", "dim")),
                id="manual",
            ),
            Option(
                Content.assemble(("Pair", "bold"), (" · Doer → verifier", "dim")),
                id="pair",
            ),
            id="collaboration-switcher",
            compact=True,
        )

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id in {"roster", "manual", "pair"}:
            self.post_message(messages.ChangeCollaborationMode(event.option_id))
        self.dismiss_picker()

    def action_dismiss(self) -> None:
        self.dismiss_picker()


class InvokeFileSearch(Message):
    pass


class InvokeSlashComplete(Message):
    pass


class AgentInfo(Label):
    pass


class ModeInfo(Label):
    pass


class CollaborationInfo(Label):
    pass


class StatusLine(Label):
    status: var[str | Content] = var("")

    def watch_status(self, status: str) -> None:
        self.set_class(not bool(status), "-hidden")
        self.update(status)
        self.tooltip = status


class PromptContainer(containers.HorizontalGroup):
    def on_mouse_down(self, event: events.MouseUp) -> None:
        for child in self.query("*"):
            if child.has_focus:
                return
        prompt_text_area = self.query_one(PromptTextArea)
        if not prompt_text_area.has_focus:
            prompt_text_area.focus()


class QueuedMessages(containers.Vertical):
    """A compact holding area for prompts waiting behind an active turn."""

    messages: var[tuple[str, ...]] = var(())

    def watch_messages(self, messages: tuple[str, ...]) -> None:
        self.display = bool(messages)
        async def rebuild() -> None:
            await self._rebuild_rows(messages)

        self.run_worker(rebuild, exclusive=True)

    async def _rebuild_rows(self, messages: tuple[str, ...]) -> None:
        await self.remove_children()
        for index, message in enumerate(messages):
            row = containers.Horizontal(classes="queued-message-row")
            await self.mount(row)
            await row.mount(Label(message, markup=False))
            await row.mount(
                Button(
                    "×",
                    id=f"queued-cancel-{index}",
                    classes="queued-message-cancel",
                    compact=True,
                    flat=True,
                    tooltip="Cancel queued message",
                )
            )

    class CancelRequested(Message):
        """Request cancellation of the queued preview at a visible index."""

        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    @on(Button.Pressed)
    def on_cancel_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        prefix = "queued-cancel-"
        if not button_id.startswith(prefix):
            return
        try:
            index = int(button_id[len(prefix) :])
        except ValueError:
            return
        event.stop()
        self.post_message(self.CancelRequested(index))


class PromptTextArea(HighlightedTextArea):
    HELP = """\
## Prompt

Talk to your agent in natural language.
See on-screen instructions for details.

- Be simple
- Be direct
- Nothing fancy
"""

    BINDING_GROUP_TITLE = "Prompt"

    BINDINGS = [
        Binding(
            "enter",
            "submit",
            "Send",
            key_display="⏎",
            priority=True,
            tooltip="Send the prompt to the agent",
        ),
        Binding(
            "ctrl+j,shift+enter",
            "newline",
            "New Line",
            key_display="⇧+⏎",
            tooltip="Insert a new line",
        ),
        Binding(
            "tab",
            "tab_complete",
            "Complete",
            tooltip="Complete path (if possible)",
            priority=True,
            show=False,
        ),
    ]

    app = getters.app(CodeSwarmApp)

    auto_completes: var[list[Option]] = var(list)
    multi_line = var(False, bindings=True)
    agent_ready: var[bool] = var(False)
    suggestions: var[list[str] | None] = var(None)
    suggestions_index: var[int] = var(0)

    project_path = var(Path())
    working_directory = var("")

    slash_commands: var[list[SlashCommand]] = var([])
    slash_command_prefixes: var[tuple[str, ...]] = var(())

    class Submitted(Message):
        def __init__(self, markdown: str) -> None:
            self.markdown = markdown
            super().__init__()

    def watch_slash_commands(self, slash_commands: list[SlashCommand]) -> None:
        """A tuple of slash commands for performance reasons (used with `str.startswith`)."""
        self.slash_command_prefixes = tuple(
            [slash_command.command for slash_command in slash_commands]
        )

    def highlight_slash_command(self, text: str) -> Content:
        """Override slash command highlighting."""

        if text.startswith(self.slash_command_prefixes):
            content = Content(text)
            for slash_command in self.slash_commands:
                if text.startswith(slash_command.command + " "):
                    content = content.stylize(
                        "$text-success", 0, len(slash_command.command)
                    )
                    if (
                        slash_command.hint
                        and len(text) - (len(slash_command.command) + 1) == 0
                    ):
                        content += Content.styled(
                            slash_command.hint, "$text-secondary 70%"
                        )
                    break
            return content
        return Content(text)

    def on_mount(self) -> None:
        self.highlight_cursor_line = False
        self.hide_suggestion_on_blur = False

    def on_key(self, event: events.Key) -> None:
        if event.key != "escape":
            self.suggestions = None
            self.suggestion = ""

    def update_suggestion(self) -> None:
        prompt = self.query_ancestor(Prompt)

        if self.selection.start == self.selection.end and self.text.startswith("/"):
            return


    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return True

    def action_newline(self) -> None:
        self.insert("\n")

    def action_submit(self) -> None:
        if not self.agent_ready and not self.text.strip().startswith(("/", "!")):
            self.app.bell()
            self.post_message(
                messages.Flash(
                    "Agent is not ready. Please wait while the agent connects…",
                    "error",
                )
            )
            return
        if self.suggestion:
            if " " not in self.text:
                self.insert(self.suggestion + " ")
            else:
                prompt = self.query_ancestor(Prompt)
                last_token = shlex.split(self.text + self.suggestion)[-1]
                last_token_path = Path(prompt.working_directory) / last_token
                if last_token_path.is_dir():
                    self.insert(self.suggestion)
                else:
                    self.insert(self.suggestion + " ")
                self.suggestion = ""
            return
        self.post_message(UserInputSubmitted(self.text))
        self.clear()

    def action_cursor_up(self, select: bool = False):
        if self.selection.is_empty and not select:
            row, _column = self.selection[0]
            if row == 0:
                self.post_message(messages.HistoryMove(-1))
                return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False):
        if self.selection.is_empty and not select:
            row, _column = self.selection[0]
            if row == (self.wrapped_document.height - 1):
                self.post_message(messages.HistoryMove(+1))
                return
        super().action_cursor_down(select)

    def action_cursor_line_end(self, select: bool = False) -> None:
        """Move the cursor to the end of the line."""
        if not self._has_cursor:
            self.scroll_end()
            return
        location = self.get_cursor_line_end_location()
        if location == self.cursor_location:
            # If the cursor is already at the end, then we assume the user wants to
            # scroll the conversation to the end
            from codeswarm.widgets.conversation import Conversation

            self.query_ancestor(Conversation).window.anchor()
        else:
            self.move_cursor(location, select=select)

    async def watch_selection(
        self, previous_selection: Selection, selection: Selection
    ) -> None:
        if previous_selection == selection:
            return
        if selection.start == selection.end:
            previous_y, previous_x = previous_selection.end
            y, x = selection.end
            if y == previous_y:
                direction = -1 if x < previous_x else +1
            else:
                direction = 0
            line = self.document.get_line(y)

            if (
                y == 0
                and x == 1
                and direction == +1
                and line
                and line[0] == "/"
            ):
                self.post_message(InvokeSlashComplete())
                return

            if y == 0 and line and line[0] == "/" and direction == -1:
                if line in self.slash_command_prefixes:
                    self.selection = Selection((0, 0), (0, len(line)))
                    return

            for _path, start, end in extract_paths_from_prompt(line):
                if x > start and x < end:
                    self.selection = Selection((y, start), (y, end))
                    break
                if direction == -1 and x == end:
                    self.selection = Selection((y, start), (y, end))
                    break

            # ``@`` starts file references.
            if x > 1 and x <= len(line) and line[x - 1] == "@":
                remaining_line = line[x + 1 :]
                if not remaining_line or remaining_line[0].isspace():
                    self.post_message(InvokeFileSearch())


class Prompt(containers.VerticalGroup):

    BINDINGS = [
        Binding("escape", "dismiss", "Dismiss", show=False),
    ]

    PROMPT_NULL = " "
    PROMPT_AI = Content.styled("❯", "$text-secondary")
    PROMPT_MULTILINE = Content.styled("☰", "$text-secondary")

    prompt_container = getters.query_one("#prompt-container", Widget)
    prompt_text_area = getters.query_one(PromptTextArea)
    prompt_label = getters.query_one("#prompt", Label)
    current_directory = getters.query_one(CondensedPath)
    path_search = getters.query_one(PathSearch)
    slash_complete = getters.query_one(SlashComplete)
    question = getters.query_one(Question)
    mode_switcher = getters.query_one(ModeSwitcher)
    collaboration_switcher = getters.query_one(CollaborationSwitcher)

    slash_commands: var[list[SlashCommand]] = var(list)
    multi_line = var(False)
    show_path_search = var(False, toggle_class="-show-path-search", bindings=True)
    show_slash_complete = var(False, toggle_class="-show-slash-complete", bindings=True)
    project_path = var(Path, init=False)
    working_directory = var("")
    agent_info = var(Content(""))
    _ask: var[Ask | None] = var(None)
    agent_ready: var[bool] = var(False)
    current_mode: var[Mode | None] = var(None)
    collaboration_mode = var("Roster")
    mode_owner: var[str] = var("")
    modes: var[dict[str, Mode] | None] = var(None)
    status: var[str | Content] = var("")
    queued_messages: var[tuple[str, ...]] = var(())

    app = getters.app(CodeSwarmApp)

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ):
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.ask_queue: list[Ask] = []

    @property
    def text(self) -> str:
        return self.prompt_text_area.text

    @text.setter
    def text(self, text: str) -> None:
        self.prompt_text_area.text = text
        self.prompt_text_area.selection = Selection.cursor(
            self.prompt_text_area.get_cursor_line_end_location()
        )

    def watch_current_mode(self, mode: Mode | None) -> None:
        self.set_class(mode is not None, "-has-mode")
        if mode is not None:
            tooltip = Content.from_markup(
                "[b]$description[/]\n\n[dim](click to open mode switcher)",
                description=mode.description,
            )
            label = (
                mode.name
                if mode.id.startswith("codeswarm:") or not self.mode_owner
                else f"{self.mode_owner}: {mode.name}"
            )
            self.query_one(ModeInfo).with_tooltip(tooltip).update(label)
        self.watch_modes(self.modes)

    def watch_mode_owner(self) -> None:
        self.watch_current_mode(self.current_mode)

    def watch_collaboration_mode(self, mode: str) -> None:
        info = self.query_one_optional(CollaborationInfo)
        if info is not None:
            info.with_tooltip("Click to choose collaboration routing").update(mode)
        switcher = self.query_one_optional(CollaborationSwitcher)
        if switcher is not None:
            switcher.highlighted = switcher.get_option_index(mode.lower())

    async def watch_project_path(self, old_path: Path, new_path: Path) -> None:
        """Refresh an already-opened file index after a project switch."""
        if old_path != new_path and self.path_search.loaded:
            self.call_later(self.path_search.refresh_paths)

    def ask(self, ask: Ask) -> None:
        """Temporarily replace the prompt with an agent question.

        Args:
            ask: An `Ask` instance which contains a question and responses.
        """
        self.ask_queue.append(ask)
        self.app.terminal_alert()
        if self._ask is None:
            self._ask = self.ask_queue.pop(0)

    @staticmethod
    def _toggle_picker(picker: PromptPicker) -> None:
        """Open a picker, or close it if the same control is clicked again."""
        if picker.display:
            picker.dismiss_picker()
        else:
            picker.focus()

    # Both openers stop the event: it must not reach the screen-level handler
    # that dismisses pickers on an outside click, or the click that opens a
    # picker would immediately close it again.
    @on(events.Click, "ModeInfo")
    def on_click_mode_info(self, event: events.Click) -> None:
        self._toggle_picker(self.mode_switcher)
        event.stop()

    @on(events.Click, "CollaborationInfo")
    def on_click_collaboration_info(self, event: events.Click) -> None:
        self._toggle_picker(self.collaboration_switcher)
        event.stop()

    @on(events.Click, "AgentInfo")
    def on_click_agent_info(self, event: events.Click) -> None:
        """Select the clicked relay recipient without leaving the prompt."""
        from codeswarm.widgets.conversation import Conversation

        self.query_ancestor(Conversation).select_routing_agent_at(event.x)
        event.stop()

    def watch_modes(self, modes: dict[str, Mode] | None) -> None:
        from codeswarm.visuals.columns import Columns

        columns = Columns("auto", "auto", "flex")
        if modes is not None:
            mode_list = sorted(
                modes.values(),
                key=lambda mode: (
                    MODE_ORDER.get(mode.id, len(MODE_ORDER) + 1),
                    mode.name.lower(),
                ),
            )
            for mode in mode_list:
                columns.add_row(
                    (
                        Content.styled("✔", "$text-success")
                        if self.current_mode and mode.id == self.current_mode.id
                        else ""
                    ),
                    Content.from_markup("[bold]$mode[/]", mode=mode.name),
                    Content.styled(mode.description or "", "dim"),
                )
        else:
            mode_list = []

        self.mode_switcher.set_options(
            [Option(row, id=mode.id) for row, mode in zip(columns, mode_list)]
        )
        if (
            self.current_mode is not None
            and modes is not None
            and self.current_mode.id in modes
        ):
            self.mode_switcher.highlighted = self.mode_switcher.get_option_index(
                self.current_mode.id
            )
        else:
            self.mode_switcher.highlighted = None

    def watch_agent_ready(self, ready: bool) -> None:
        self.set_class(not ready, "-not-ready")
        if ready:
            self.query_one(AgentInfo).update(self.agent_info)

    def watch_agent_info(self, agent_info: Content) -> None:
        if self.agent_ready:
            self.query_one(AgentInfo).update(agent_info)
        else:
            self.query_one(AgentInfo).update("Initializing…")

    def watch_multiline(self) -> None:
        self.update_prompt()

    def watch_working_directory(self, working_directory: str) -> None:
        if not working_directory:
            return
        out_of_bounds = not Path(working_directory).is_relative_to(self.project_path)
        if out_of_bounds and not self.has_class("-working-directory-out-of-bounds"):
            self.post_message(
                messages.Flash(
                    "You have navigated away from the project directory",
                    style="error",
                    duration=5,
                )
            )
        self.set_class(
            out_of_bounds,
            "-working-directory-out-of-bounds",
        )

    def watch__ask(self, ask: Ask | None) -> None:
        self.set_class(ask is not None, "-mode-ask")
        if ask is None:
            self.prompt_text_area.focus()
        else:
            self.question.update(ask)
            self.question.focus()

    def update_prompt(self):
        """Update the prompt according to the current mode."""
        self.prompt_label.update(
            self.PROMPT_MULTILINE if self.multi_line else self.PROMPT_AI,
            layout=False,
        )
        prompt_message = self.app.settings.get("ui.prompt_message", str)
        self.prompt_text_area.placeholder = Content.assemble(
            f"{prompt_message}\t".expandtabs(8),
            ("▌/▐", "r"),
            " commands ",
            ("▌@▐", "r"),
            " files",
        )
        self.prompt_text_area.highlight_language = "markdown"

    def focus(self, scroll_visible: bool = True) -> Self:
        if self._ask is not None:
            self.question.focus()
        else:
            self.query(HighlightedTextArea).focus()
        return self

    def append(self, text: str) -> None:
        self.query_one(HighlightedTextArea).insert(
            text, maintain_selection_offset=False
        )

    def watch_show_path_search(self, show: bool) -> None:
        self.prompt_text_area.suggestion = ""

    def watch_show_slash_complete(self, show: bool) -> None:
        if show:
            self.slash_complete.focus()

    def project_directory_updated(self) -> None:
        """Refresh file search only after the user has opened it at least once."""
        if self.path_search.loaded:
            self.path_search.refresh_paths()

    @on(TextArea.Changed)
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text

        self.multi_line = "\n" in text or "```" in text

        self.update_prompt()

    @on(InvokeFileSearch)
    def on_invoke_file_search(self, event: InvokeFileSearch) -> None:
        event.stop()
        # Scanning a large repository is not startup work. It starts only
        # when the user asks to attach a file, and later refreshes stay warm
        # while that feature has been used in this conversation.
        self.path_search.refresh_paths()
        self.show_path_search = True
        self.path_search.reset()

    @on(InvokeSlashComplete)
    def on_invoke_slash_complete(self, event: InvokeSlashComplete) -> None:
        event.stop()
        self.show_slash_complete = True

    @on(messages.PromptSuggestion)
    def on_prompt_suggestion(self, event: messages.PromptSuggestion) -> None:
        event.stop()
        self.prompt_text_area.suggestion = event.suggestion

    @on(SlashComplete.Completed)
    def on_slash_complete_completed(self, event: SlashComplete.Completed) -> None:
        event.stop()
        self.show_slash_complete = False
        self.prompt_text_area.clear()
        self.prompt_text_area.insert(event.command)
        self.prompt_text_area.suggestion = ""
        if event.submit:
            self.post_message(UserInputSubmitted(event.command))
            self.prompt_text_area.clear()
        else:
            self.prompt_text_area.insert(" ")
            self.focus()

    @on(SlashComplete.Previewed)
    def on_slash_complete_previewed(self, event: SlashComplete.Previewed) -> None:
        event.stop()
        self.text = event.command

    @on(messages.Dismiss)
    def on_dismiss(self, event: messages.Dismiss) -> None:
        event.stop()
        if event.widget is self.slash_complete and self.show_slash_complete:
            self.show_slash_complete = False
            self.prompt_text_area.suggestion = ""
            self.focus()
        elif event.widget is self.path_search and self.show_path_search:
            self.show_path_search = False
            self.focus()

    @on(messages.InsertPath)
    def on_insert_path(self, event: messages.InsertPath) -> None:
        event.stop()
        if " " in event.path:
            path = f'"{event.path}"'
        else:
            path = event.path
            if (
                self.prompt_text_area.get_text_range(*self.prompt_text_area.selection)
                != " "
            ):
                path += " "
        self.prompt_text_area.insert(path)

    @on(Question.Answer)
    def on_question_answer(self, event: Question.Answer) -> None:
        """Question has been answered."""
        event.stop()

        def remove_question() -> None:
            """Remove the question and restore the text prompt."""
            if self.ask_queue:
                self._ask = self.ask_queue.pop(0)
            else:
                self._ask = None
            self.app.terminal_alert(False)

        if self._ask is not None and (callback := self._ask.callback) is not None:
            callback(event.answer)

        self.set_timer(0.3, remove_question)

    def suggest(self, suggestion: str) -> None:
        if suggestion.startswith(self.text) and self.text != suggestion:
            self.prompt_text_area.suggestion = suggestion[len(self.text) :]

    def compose(self) -> ComposeResult:
        yield PathSearch(self.project_path).data_bind(root=Prompt.project_path)
        yield SlashComplete().data_bind(slash_commands=Prompt.slash_commands)
        yield QueuedMessages(markup=False).data_bind(messages=Prompt.queued_messages)
        with PromptContainer(id="prompt-container"):
            yield Question()
            with containers.HorizontalGroup(id="text-prompt"):
                yield Label(self.PROMPT_AI, id="prompt", markup=False)
                yield PromptTextArea().data_bind(
                    multi_line=Prompt.multi_line,
                    agent_ready=Prompt.agent_ready,
                    project_path=Prompt.project_path,
                    working_directory=Prompt.working_directory,
                    slash_commands=Prompt.slash_commands,
                )
        with containers.HorizontalGroup(id="info-container"):
            yield AgentInfo()
            yield CondensedPath().data_bind(path=Prompt.working_directory)
            yield StatusLine(markup=False).data_bind(status=Prompt.status)
            yield ModeSwitcher()
            yield CollaborationSwitcher()
            yield CollaborationInfo("Roster")
            yield ModeInfo("mode")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return True

    def action_dismiss(self) -> None:
        """Escape means one of four things, in priority order:

        1. A ghost completion suggestion is showing: clear it.
        2. The slash-command popup is open: close it.
        3. None of the above: let the key bubble (`SkipAction`) so an
           ancestor (e.g. cancelling the agent's turn) can handle it.
        """
        if self.prompt_text_area.suggestion:
            self.prompt_text_area.suggestion = ""
            return
        if self.show_slash_complete:
            self.show_slash_complete = False
        else:
            raise SkipAction()
