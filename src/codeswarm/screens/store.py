from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from textual.binding import Binding
from textual.screen import Screen
from textual import events
from textual import work
from textual import getters
from textual import on
from textual.app import ComposeResult
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual import containers
from textual import widgets

import codeswarm
from codeswarm.app import CodeSwarmApp
from codeswarm.format_path import format_path
from codeswarm.pill import pill
from codeswarm import messages
from codeswarm.widgets.directory_input import DirectoryInput
from codeswarm.widgets.condensed_path import CondensedPath
from codeswarm.widgets.grid_select import GridSelect
from codeswarm.agent_schema import Agent
from codeswarm.agents import read_agents, detect_preferred_agents, available_identities


CODESWARM_FORMATION = "      ✈\n   ✈     ✈\n✈           ✈"


@dataclass
class ChangeDirectory(Message):
    path: str


class DirectoryDisplay(containers.HorizontalGroup):

    BINDINGS = [("escape", "dismiss", "Dismiss")]

    DEFAULT_CSS = """
    DirectoryDisplay {
        CondensedPath { display: block; }
        DirectoryInput { display: none; }
        &.-edit {
            CondensedPath { display: none}
            DirectoryInput { display: block; }
        }
    }
    """

    project_dir: reactive[Path] = reactive(Path)
    path = reactive("")
    edit = reactive(False, toggle_class="-edit")

    directory_input = getters.query_one(DirectoryInput)
    condensed_path = getters.query_one(CondensedPath)

    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.path = format_path(project_dir, directory=True)

    def watch_project_dir(self, path: Path) -> None:
        self.path = format_path(path, directory=True)

    def focus(self, scroll_visible=True) -> Self:
        self.edit = True
        self.directory_input.focus(scroll_visible=scroll_visible)
        return self

    @on(events.Click, "CondensedPath")
    def on_click(self) -> None:
        self.edit = True
        self.directory_input.focus()

    @on(events.DescendantBlur)
    def on_blur(self):
        self.action_dismiss()

    @on(widgets.Input.Submitted)
    def on_input_submitted(self, event: widgets.Input.Submitted) -> None:
        path = Path(event.value).expanduser().resolve()
        self.edit = False
        if not path.is_dir():
            self.notify(
                f"Unable to change directory to {str(path)!r}",
                title="Change directory",
                severity="error",
            )
            return
        self.condensed_path.path = format_path(path, directory=True)
        self.post_message(ChangeDirectory(str(path)))

    def action_dismiss(self) -> None:
        self.edit = False
        self.directory_input.value = self.path

    def watch_edit(self, edit: bool) -> None:
        if not edit and self.directory_input.has_focus:
            self.directory_input.blur()

    def compose(self) -> ComposeResult:
        yield widgets.Label("📁 ")
        yield CondensedPath(self.path, directory=True).data_bind(
            path=DirectoryDisplay.path
        ).with_tooltip("Project directory for new agent sessions (click to edit)")
        yield DirectoryInput(self.path, select_on_focus=True, compact=True).data_bind(
            value=DirectoryDisplay.path
        )


class AgentItem(containers.VerticalGroup):
    """An entry in the Agent grid select."""

    selected: reactive[bool] = reactive(False, toggle_class="-selected")

    def __init__(
        self, agent: Agent, *, selected: bool = False, available: bool = False
    ) -> None:
        self._agent = agent
        self._available = available
        super().__init__()
        self.set_reactive(AgentItem.selected, selected)
        # set_reactive skips the reactive's toggle_class side effect, and the
        # pre-set value also short-circuits it at mount; apply it by hand.
        self.set_class(selected, "-selected")

    @property
    def agent(self) -> Agent:
        return self._agent

    def compose(self) -> ComposeResult:
        agent = self._agent
        yield widgets.Label(agent["name"], id="name")
        yield widgets.Label(agent["author_name"], id="author")
        yield widgets.Static(agent["description"], id="description")
        yield widgets.Label(
            pill(
                "Ready" if self._available else "Not detected",
                "$success-muted 50%" if self._available else "$warning-muted 50%",
                "$text-primary",
            ),
            id="availability",
        )


class AgentGridSelect(GridSelect):
    HELP = """\
## Agent select

- **cursor keys** Navigate agents
- **tab / shift+tab** Move to next / previous section
- **click / space** Add or remove from the roster
- **enter** Launch the roster, or the highlighted agent if none is selected
"""
    BINDINGS = [
        Binding("enter", "launch", "Launch", tooltip="Launch the roster"),
        Binding(
            "space",
            "toggle_roster",
            "Add/remove roster",
            tooltip="Add or remove from the roster",
        ),
    ]
    BINDING_GROUP_TITLE = "Agent Select"

    @dataclass
    class ToggleRoster(Message):
        """Sent when the user adds or removes the highlighted agent."""

        identity: str

    @dataclass
    class LaunchRoster(Message):
        """Sent when the user asks to launch. `identity` is the highlighted
        agent, used only as a fallback when nothing is in the roster."""

        identity: str | None

    def _clear_mouse_highlight(self) -> None:
        """Keep mouse selection visually separate from keyboard navigation."""
        self.highlighted = None
        self.query(".-highlight").remove_class("-highlight")

    def on_click(self, event: events.Click) -> None:
        """Make a single card click toggle roster membership."""
        if event.widget is None:
            return
        for widget in event.widget.ancestors_with_self:
            if widget in self.children and isinstance(widget, AgentItem):
                # Mouse selection is already explicit. Do not leave the card
                # with the separate keyboard-highlight treatment, which made
                # an unselected card still look selected after a second click.
                self.post_message(self.ToggleRoster(widget.agent["identity"]))
                # Textual may restore click focus after this handler returns,
                # so clear it after the event's refresh cycle.
                self.call_after_refresh(self._clear_mouse_highlight)
                event.stop()
                return
        super().on_click(event)

    def action_toggle_roster(self) -> None:
        if self.highlighted is None:
            return
        child = self.children[self.highlighted]
        if not isinstance(child, AgentItem):
            return
        self.post_message(self.ToggleRoster(child.agent["identity"]))

    def action_launch(self) -> None:
        identity: str | None = None
        if self.highlighted is not None:
            child = self.children[self.highlighted]
            if isinstance(child, AgentItem):
                identity = child.agent["identity"]
        self.post_message(self.LaunchRoster(identity))


class Container(containers.VerticalScroll):
    BINDING_GROUP_TITLE = "View"

    def allow_focus(self) -> bool:
        """Only allow focus when we can scroll."""
        return super().allow_focus() and self.show_vertical_scrollbar


class StoreScreen(Screen):
    BINDING_GROUP_TITLE = "Screen"
    CSS_PATH = "store.tcss"
    FOCUS_GROUP = Binding.Group("Focus")
    BINDINGS = [
        Binding(
            "tab",
            "app.focus_next",
            "Focus Next",
            group=FOCUS_GROUP,
        ),
        Binding(
            "shift+tab",
            "app.focus_previous",
            "Focus Previous",
            group=FOCUS_GROUP,
        ),
        Binding("enter", "launch_roster", "Launch roster"),
        Binding(
            "ctrl+d",
            "directory",
            "Directory",
            tooltip="Change project directory",
        ),
    ]

    agents_view = getters.query_one("#agents-view", AgentGridSelect)
    container = getters.query_one("#container", Container)

    project_dir: reactive[Path] = reactive(Path)

    app = getters.app(CodeSwarmApp)

    def __init__(
        self, name: str | None = None, id: str | None = None, classes: str | None = None
    ):
        self._agents: dict[str, Agent] = {}
        self._detected: list[Agent] = []
        self._installed: set[str] = set()
        self._roster_selection: dict[str, Agent] = {}
        super().__init__(name=name, id=id, classes=classes)
        self.project_dir = self.app.project_dir

    @property
    def agents(self) -> dict[str, Agent]:
        return self._agents

    def compose(self) -> ComposeResult:
        with containers.VerticalGroup(id="title-container"):
            with containers.Grid(id="title-grid"):
                yield widgets.Label(self.get_info(), id="info")
                yield widgets.Static(
                    CODESWARM_FORMATION,
                    id="codeswarm-formation",
                    markup=False,
                )
        yield DirectoryDisplay(self.project_dir).data_bind(
            project_dir=StoreScreen.project_dir
        )
        yield widgets.Static(id="roster-strip", classes="-empty")
        yield Container(id="container", can_focus=False)

    def get_info(self) -> Content:
        return Content.assemble(
            Content.from_markup("CodeSwarm"),
            pill(f"v{codeswarm.get_version()}", "$primary-muted", "$text-primary"),
            "\nChoose one or more coding agents for this workspace.",
            "\nClick or Space selects agents; Enter starts the roster.",
        )

    def _agent_item(self, agent: Agent) -> AgentItem:
        return AgentItem(
            agent,
            selected=agent["identity"] in self._roster_selection,
            available=agent["identity"] in self._installed,
        )

    def compose_agents(self) -> ComposeResult:
        agents = self._agents

        ordered_agents = sorted(
            (
                agent
                for agent in agents.values()
                if agent["type"] == "coding"
            ),
            key=lambda agent: agent["name"].casefold(),
        )

        ready_agents = [
            agent
            for agent in self._detected
            if agent["type"] == "coding"
        ]
        if ready_agents:
            yield widgets.Static(
                "[$text-warning u]Detected coding agents[/]",
                classes="heading",
            )
            with containers.VerticalGroup():
                with AgentGridSelect(classes="agents-picker", min_column_width=40):
                    for agent in ready_agents:
                        yield self._agent_item(agent)

        detected_ids = {agent["identity"] for agent in ready_agents}
        other_agents = [
            agent for agent in ordered_agents if agent["identity"] not in detected_ids
        ]
        if other_agents:
            yield widgets.Static(
                "[$text-warning u]Other coding agents[/]",
                classes="heading",
            )
            with containers.VerticalGroup():
                with AgentGridSelect(classes="agents-picker", min_column_width=40):
                    for agent in other_agents:
                        yield self._agent_item(agent)

        if not ready_agents and not ordered_agents:
            yield widgets.Label(
                "No agents match your filter.", classes="instruction-text"
            )

    def move_focus(self, direction: Literal[-1] | Literal[+1]) -> None:
        if isinstance(self.focused, GridSelect):
            focus_chain = list(self.query(GridSelect))
            if self.focused in focus_chain:
                index = focus_chain.index(self.focused)
                new_focus = focus_chain[(index + direction) % len(focus_chain)]
                if direction == -1:
                    new_focus.highlight_last()
                else:
                    new_focus.highlight_first()
                new_focus.focus(scroll_visible=False)

    @on(GridSelect.LeaveUp)
    def on_grid_select_leave_up(self, event: GridSelect.LeaveUp):
        event.stop()
        self.move_focus(-1)

    @on(GridSelect.LeaveDown)
    def on_grid_select_leave_down(self, event: GridSelect.LeaveUp):
        event.stop()
        self.move_focus(+1)

    @on(AgentGridSelect.ToggleRoster)
    def on_agent_grid_select_toggle_roster(
        self, message: AgentGridSelect.ToggleRoster
    ) -> None:
        identity = message.identity
        if identity in self._roster_selection:
            del self._roster_selection[identity]
        elif (agent := self._agents.get(identity)) is not None:
            self._roster_selection[identity] = agent
        for item in self.query(AgentItem):
            if item.agent["identity"] == identity:
                item.selected = identity in self._roster_selection
        self._update_roster_strip()

    @on(AgentGridSelect.LaunchRoster)
    def on_agent_grid_select_launch_roster(
        self, message: AgentGridSelect.LaunchRoster
    ) -> None:
        if self._roster_selection:
            identities = list(self._roster_selection.keys())
            unavailable = [
                agent["name"]
                for agent in self._roster_selection.values()
                if agent["identity"] not in self._installed
            ]
            if unavailable:
                self.notify(
                    "Not detected: "
                    f"{', '.join(unavailable)}. Install the CLI and reopen CodeSwarm.",
                    title="Agent unavailable",
                    severity="warning",
                )
                return
            self.post_message(
                messages.LaunchAgent(identities[0], peers=tuple(identities[1:]))
            )
            self._roster_selection.clear()
            for item in self.query(AgentItem):
                item.selected = False
            self._update_roster_strip()
            return
        if message.identity is not None and message.identity in self._installed:
            self.post_message(messages.LaunchAgent(message.identity))
        elif message.identity is not None:
            agent = self._agents.get(message.identity)
            name = agent["name"] if agent is not None else "Selected agent"
            self.notify(
                f"{name} is not detected. Install the CLI and reopen CodeSwarm.",
                title="Agent unavailable",
                severity="warning",
            )

    def _update_roster_strip(self) -> None:
        try:
            strip = self.query_one("#roster-strip", widgets.Static)
        except NoMatches:
            return
        names = [agent["name"] for agent in self._roster_selection.values()]
        strip.set_class(not names, "-empty")
        if names:
            strip.update(
                f"Roster ({len(names)}): {', '.join(names)} — [b]enter[/b] to launch"
            )

    @on(ChangeDirectory)
    def on_change_directory(self, event: ChangeDirectory) -> None:
        self.project_dir = Path(event.path)
        self.app.project_dir = self.project_dir

    @work
    async def on_mount(self) -> None:
        try:
            self._agents = await read_agents()
        except Exception as error:
            self.notify(
                f"Failed to read agents data ({error})",
                title="Agents data",
                severity="error",
            )
            return

        self._installed = await available_identities(list(self._agents.values()))
        self._detected = await detect_preferred_agents(
            self._agents, self._installed
        )
        # Pre-select the detected roster so launching is a single keypress;
        # the user can space-toggle to trim it before hitting enter.
        self._roster_selection = {
            agent["identity"]: agent for agent in self._detected
        }
        # The catalog probes are asynchronous. A quick quit may unmount the
        # Store before they complete, in which case there is nothing left to
        # render into.
        if not self.is_attached:
            return
        container = self.query_one_optional("#container", Container)
        if container is None:
            return
        await container.mount_compose(self.compose_agents())
        self._update_roster_strip()
        with suppress(NoMatches):
            first_grid = container.query(GridSelect).first()
            first_grid.focus(scroll_visible=False)

    async def action_directory(self) -> None:
        if (directory_display := self.query_one_optional(DirectoryDisplay)) is not None:
            directory_display.focus()

    def action_launch_roster(self) -> None:
        """Launch the selected roster even when a mouse click cleared grid focus."""
        identity: str | None = None
        if not self._roster_selection:
            for grid in self.query(AgentGridSelect):
                if grid.highlighted is None:
                    continue
                child = grid.children[grid.highlighted]
                if isinstance(child, AgentItem):
                    identity = child.agent["identity"]
                    break
        self.post_message(AgentGridSelect.LaunchRoster(identity))


if __name__ == "__main__":
    from codeswarm.app import CodeSwarmApp

    app = CodeSwarmApp(mode="store")

    app.run()
