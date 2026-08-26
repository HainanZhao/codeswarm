"""The small, in-app editor for CodeSwarm preferences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual import containers, getters, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, Switch

from codeswarm.app import CodeSwarmApp
from codeswarm.agent_schema import Agent
from codeswarm.agents import available_identities, read_agents
from codeswarm.settings import SchemaDict

if TYPE_CHECKING:
    from codeswarm.widgets.conversation import Conversation


CHOICES: dict[str, list[tuple[str, str]]] = {
    "ui.density": [("Comfortable", "comfortable"), ("Compact", "compact")],
    "ui.scrollbar": [("Normal", "normal"), ("Hidden", "hidden")],
    "notifications.system": [
        ("When CodeSwarm is unfocused", "blur"),
        ("Always", "always"),
        ("Never", "never"),
    ],
    "tools.expand": [
        ("On failure", "fail"),
        ("Always", "always"),
        ("Never", "never"),
    ],
    "diff.view": [("Automatic", "auto"), ("Unified", "unified"), ("Split", "split")],
    "diff.wrap": [("No wrap", "no-wrap"), ("Wrap", "wrap")],
}


@dataclass(frozen=True)
class SettingField:
    key: str
    title: str
    type: str
    choices: list[tuple[str, str]] | None = None

    @property
    def control_id(self) -> str:
        return f"config-{self.key.replace('.', '-') }"


class ConfigRow(containers.Horizontal):
    """One setting label and its editor."""

    def __init__(self, field: SettingField, value: object) -> None:
        super().__init__()
        self.field = field
        self.value = value

    def compose(self) -> ComposeResult:
        yield Label(self.field.title, classes="setting-label")
        if self.field.type == "boolean":
            yield Switch(bool(self.value), id=self.field.control_id)
        elif self.field.choices is not None:
            yield Select(
                self.field.choices,
                value=str(self.value),
                allow_blank=False,
                id=self.field.control_id,
            )
        else:
            input_type = {
                "integer": "integer",
                "number": "number",
            }.get(self.field.type, "text")
            yield Input(str(self.value), type=input_type, id=self.field.control_id)


class ConfigRosterRow(containers.Horizontal):
    """An agent checkbox with ordering controls that act on the same row."""

    def __init__(self, control_id: str, selected: bool) -> None:
        super().__init__(id=f"{control_id}-row", classes="config-roster-row")
        self.control_id = control_id
        self.selected = selected

    def compose(self) -> ComposeResult:
        yield Checkbox(
            "",
            self.selected,
            id=self.control_id,
            classes="config-roster-agent",
        )
        yield Button("↑", id=f"{self.control_id}-up", classes="config-roster-move")
        yield Button("↓", id=f"{self.control_id}-down", classes="config-roster-move")


class ConfigScreen(Screen[bool]):
    """Edit persistent CodeSwarm preferences without leaving a conversation."""

    CSS_PATH = "config.tcss"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("alt+up", "move_roster_up", "Move Agent Up", show=False),
        Binding("alt+down", "move_roster_down", "Move Agent Down", show=False),
    ]
    AUTO_FOCUS = "#config-ui-prompt_message"

    app = getters.app(CodeSwarmApp)

    def __init__(self, conversation: Conversation | None = None) -> None:
        super().__init__()
        self._conversation = conversation
        self._agents: dict[str, Agent] = {}
        self._roster_controls: dict[str, str] = {}
        self._roster_labels: dict[str, str] = {}
        self._installed: set[str] = set()

    def _fields(self) -> list[tuple[str, list[SettingField]]]:
        fields: list[tuple[str, list[SettingField]]] = []
        for group in self.app.settings_schema.schema:
            group_key = group["key"]
            # The saved roster is launch state, rather than a preference. It
            # is maintained by the agent store and never hand-edited here.
            if group_key == "launcher":
                continue
            group_fields: list[SettingField] = []
            for field in group.get("fields", []):
                if not field.get("editable", True):
                    continue
                key = f"{group_key}.{field['key']}"
                choices = CHOICES.get(key)
                if key == "ui.theme":
                    choices = [(name, name) for name in self.app.available_themes]
                group_fields.append(
                    SettingField(
                        key,
                        field.get("title", field["key"].replace("_", " ").title()),
                        field["type"],
                        choices,
                    )
                )
            if group_fields:
                fields.append((group_key.title(), group_fields))
        return fields

    def compose(self) -> ComposeResult:
        with containers.Vertical(id="config-dialog"):
            yield Static("CodeSwarm configuration", id="config-title")
            yield Static(
                "Preferences apply when you save. Session and chat commands stay in the conversation.",
                id="config-description",
            )
            with containers.VerticalScroll(id="config-settings"):
                yield Label("Agents", classes="config-group")
                yield Label(
                    "Roster for the next workspace (relay order)",
                    classes="setting-label",
                )
                yield containers.Vertical(id="config-roster-options")
                yield Static(
                    "Loading available agents…", id="config-roster-help"
                )
                for group_title, fields in self._fields():
                    yield Label(group_title, classes="config-group")
                    for field in fields:
                        value_type = {
                            "boolean": bool,
                            "integer": int,
                            "number": float,
                        }.get(field.type, str)
                        yield ConfigRow(field, self.app.settings.get(field.key, value_type))
            with containers.Horizontal(id="config-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    async def on_mount(self) -> None:
        """Load the bundled agent catalog and build the roster checkboxes."""
        try:
            self._agents = await read_agents()
        except Exception as error:
            self.query_one("#config-roster-help", Static).update(
                f"Unable to load agent catalog: {error}"
            )
            return
        self._installed = await available_identities(list(self._agents.values()))

        saved = [
            identity.strip()
            for identity in self.app.settings.get("launcher.roster", str).splitlines()
            if identity.strip()
        ]
        active = (
            [
                entry.data["identity"]
                for entry in self._conversation.session.roster
                if entry.active
            ]
            if self._conversation is not None
            else saved
        )
        owner_identity = (
            self._conversation.session.roster[0].data["identity"]
            if self._conversation is not None and self._conversation.session.roster
            else None
        )
        if owner_identity is not None and owner_identity not in active:
            active.append(owner_identity)
        coding_agents = {
            identity: agent
            for identity, agent in self._agents.items()
            if agent["type"] == "coding"
        }
        # A live peer the catalog does not know (an ad-hoc adapter) still needs
        # a row, or an untouched save would silently drop it from the session.
        for identity in active:
            if identity not in coding_agents:
                entry_data = next(
                    (
                        entry.data
                        for entry in self._conversation.session.roster
                        if self._conversation is not None
                        and entry.data["identity"] == identity
                    ),
                    None,
                )
                if entry_data is not None:
                    coding_agents[identity] = entry_data
        if self._conversation is not None:
            ordered_identities = [
                identity for identity in active if identity in coding_agents
            ]
            ordered_identities.extend(
                identity
                for identity in saved
                if identity in coding_agents and identity not in ordered_identities
            )
        else:
            ordered_identities = [
                identity for identity in saved if identity in coding_agents
            ]
        ordered_identities.extend(
            identity
            for identity in coding_agents
            if identity not in ordered_identities
        )

        rows: list[ConfigRosterRow] = []
        for index, identity in enumerate(ordered_identities):
            agent = coding_agents[identity]
            control_id = f"config-roster-agent-{index}"
            self._roster_controls[control_id] = identity
            status = "" if identity in self._installed else " — not detected"
            self._roster_labels[control_id] = (
                f"{agent['name']}  ({agent['short_name']}){status}"
            )
            rows.append(ConfigRosterRow(control_id, identity in active))
        await self.query_one("#config-roster-options", containers.Vertical).mount(
            *rows
        )
        self._refresh_roster_labels()
        if owner_identity is not None:
            owner_control_id = next(
                (
                    control_id
                    for control_id, identity in self._roster_controls.items()
                    if identity == owner_identity
                ),
                None,
            )
            if owner_control_id is not None:
                self.query_one(f"#{owner_control_id}", Checkbox).disabled = True
        self.query_one("#config-roster-help", Static).update(
            "Check agents to update this idle session. Use ↑/↓ (or Alt+↑/↓) "
            "to set the next workspace's relay order."
        )

    def _refresh_roster_labels(self) -> None:
        """Number controls so their top-to-bottom relay order is explicit."""
        selected_position = 0
        for control in self.query("#config-roster-options Checkbox"):
            if control.id is not None:
                if control.value:
                    selected_position += 1
                    prefix = f"{selected_position}. "
                else:
                    prefix = "   "
                control.label = f"{prefix}{self._roster_labels[control.id]}"
        self._refresh_roster_move_buttons()

    def _refresh_roster_move_buttons(self) -> None:
        """Enable only moves that can change the checked relay order."""
        selected = [
            control
            for control in self.query("#config-roster-options Checkbox")
            if control.value
        ]
        first = selected[0] if selected else None
        last = selected[-1] if selected else None
        for control in self.query("#config-roster-options Checkbox"):
            if control.id is None:
                continue
            self.query_one(f"#{control.id}-up", Button).disabled = (
                not control.value or control is first
            )
            self.query_one(f"#{control.id}-down", Button).disabled = (
                not control.value or control is last
            )

    @on(Checkbox.Changed, "#config-roster-options Checkbox")
    def on_roster_changed(self) -> None:
        self._refresh_roster_labels()

    def _focused_roster_control(self) -> Checkbox | None:
        focused = self.focused
        while focused is not None and not isinstance(focused, ConfigRosterRow):
            focused = focused.parent
        if not isinstance(focused, ConfigRosterRow):
            return None
        return focused.query_one(Checkbox)

    def _move_roster(self, direction: int, control: Checkbox | None = None) -> None:
        """Move one checked roster entry relative to the next checked entry."""
        control = control or self._focused_roster_control()
        selected = [
            option
            for option in self.query("#config-roster-options Checkbox")
            if option.value
        ]
        if control is None or control not in selected:
            return
        index = selected.index(control)
        target_index = index + direction
        if not 0 <= target_index < len(selected):
            return
        container = self.query_one("#config-roster-options", containers.Vertical)
        row = control.parent
        target = selected[target_index].parent
        if direction < 0:
            container.move_child(row, before=target)
        else:
            container.move_child(row, after=target)
        self._refresh_roster_labels()

    def action_move_roster_up(self) -> None:
        self._move_roster(-1)

    def action_move_roster_down(self) -> None:
        self._move_roster(1)

    def _read_roster(self) -> list[str]:
        """Return checked agents in their displayed relay order."""
        return [
            self._roster_controls[control.id]
            for control in self.query("#config-roster-options Checkbox")
            if control.value and control.id in self._roster_controls
        ]

    def _feedback(self, message: str, *, severity: str = "information") -> None:
        """Use the conversation ribbon once a prompt exists; otherwise toast."""
        if self._conversation is not None:
            style = "error" if severity == "error" else "warning"
            self._conversation.flash(message, style=style)
        else:
            self.notify(message, title="Roster", severity=severity)

    @on(Button.Pressed, "#cancel")
    def on_cancel_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#save")
    async def on_save_button(self) -> None:
        await self.action_save()

    @on(Button.Pressed, ".config-roster-move")
    def on_roster_move_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None:
            return
        suffix = "-up" if button_id.endswith("-up") else "-down"
        control = self.query_one(f"#{button_id.removesuffix(suffix)}", Checkbox)
        self._move_roster(-1 if suffix == "-up" else 1, control)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def action_save(self) -> None:
        if self._conversation is not None and self._conversation.turn == "agent":
            self._conversation.flash(
                "Wait for active agent work to finish before changing the roster",
                style="warning",
            )
            return
        roster = self._read_roster()
        if not roster:
            self._feedback(
                "Select at least one agent for the next workspace",
                severity="error",
            )
            first = self.query("#config-roster-options Checkbox").first(None)
            if first is not None:
                first.focus()
            return
        for _group_title, fields in self._fields():
            for field in fields:
                control = self.query_one(f"#{field.control_id}")
                try:
                    if isinstance(control, Switch):
                        value: object = control.value
                    elif isinstance(control, Select):
                        value = control.value
                    elif isinstance(control, Input):
                        raw_value = control.value
                        value = int(raw_value) if field.type == "integer" else (
                            float(raw_value) if field.type == "number" else raw_value
                        )
                    else:
                        raise TypeError(f"Unexpected config control for {field.key}")
                except ValueError:
                    self._feedback(
                        f"{field.title} needs a valid {field.type} value",
                        severity="error",
                    )
                    control.focus()
                    return
                self.app.settings.set(field.key, value)
        if self._conversation is not None and self._conversation.session.roster:
            failures = await self._conversation.reconcile_roster(roster, self._agents)
            self._conversation.flash(
                "Configuration saved"
                if not failures
                else "Configuration saved; some roster changes could not be applied",
                style="success" if not failures else "warning",
            )
        else:
            self.app.settings.set("launcher.roster", "\n".join(roster))
            await self.app.save_settings()
            if self._conversation is not None:
                self._conversation.flash("Configuration saved", style="success")
            else:
                self.app.notify(
                    "Configuration saved", title="CodeSwarm", severity="information"
                )
        self.dismiss(True)
