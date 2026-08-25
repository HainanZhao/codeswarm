"""The small, in-app editor for Wingmen preferences."""

from __future__ import annotations

from dataclasses import dataclass

from textual import containers, events, getters, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, Switch

from wingmen.app import WingmenApp
from wingmen.agent_schema import Agent
from wingmen.agents import available_identities, read_agents
from wingmen.settings import SchemaDict


CHOICES: dict[str, list[tuple[str, str]]] = {
    "ui.density": [("Comfortable", "comfortable"), ("Compact", "compact")],
    "ui.scrollbar": [("Normal", "normal"), ("Thin", "thin"), ("Hidden", "hidden")],
    "notifications.system": [
        ("When Wingmen is unfocused", "blur"),
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
            input_type = "integer" if self.field.type in {"integer", "number"} else "text"
            yield Input(str(self.value), type=input_type, id=self.field.control_id)


class ConfigScreen(Screen[bool]):
    """Edit persistent Wingmen preferences without leaving a conversation."""

    CSS_PATH = "config.tcss"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("alt+up", "move_roster_up", "Move Agent Up", show=False),
        Binding("alt+down", "move_roster_down", "Move Agent Down", show=False),
    ]
    AUTO_FOCUS = "#config-ui-prompt_message"

    app = getters.app(WingmenApp)

    def __init__(self) -> None:
        super().__init__()
        self._agents: dict[str, Agent] = {}
        self._roster_controls: dict[str, str] = {}
        self._roster_labels: dict[str, str] = {}
        self._installed: set[str] = set()
        self._last_roster_control_id: str | None = None

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
            yield Static("Wingmen configuration", id="config-title")
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
                with containers.Horizontal(id="config-roster-order-actions"):
                    yield Button("Move Up", id="config-roster-up")
                    yield Button("Move Down", id="config-roster-down")
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
        coding_agents = {
            identity: agent
            for identity, agent in self._agents.items()
            if agent["type"] == "coding"
        }
        ordered_identities = [
            identity for identity in saved if identity in coding_agents
        ]
        ordered_identities.extend(
            identity
            for identity in coding_agents
            if identity not in ordered_identities
        )

        controls: list[Checkbox] = []
        for index, identity in enumerate(ordered_identities):
            agent = coding_agents[identity]
            control_id = f"config-roster-agent-{index}"
            self._roster_controls[control_id] = identity
            status = "" if identity in self._installed else " — not detected"
            self._roster_labels[control_id] = (
                f"{agent['name']}  ({agent['short_name']}){status}"
            )
            controls.append(
                Checkbox(
                    "",
                    identity in saved,
                    id=control_id,
                    classes="config-roster-agent",
                )
            )
        await self.query_one("#config-roster-options", containers.Vertical).mount(
            *controls
        )
        self._refresh_roster_labels()
        self.query_one("#config-roster-help", Static).update(
            "Check agents, then use Move Up/Down (or Alt+↑/↓) to set relay "
            "order. Changes apply to the next workspace."
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

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, Checkbox) and event.widget.id in self._roster_controls:
            self._last_roster_control_id = event.widget.id

    @on(Checkbox.Changed, "#config-roster-options Checkbox")
    def on_roster_changed(self) -> None:
        self._refresh_roster_labels()

    def _move_roster(self, direction: int) -> None:
        """Move the focused roster entry one position without changing selection."""
        options = list(self.query("#config-roster-options Checkbox"))
        focused = self.focused
        if not isinstance(focused, Checkbox) or focused not in options:
            focused = next(
                (
                    option
                    for option in options
                    if option.id == self._last_roster_control_id
                ),
                None,
            )
        if focused is None:
            self.notify("Focus an agent checkbox first", title="Roster")
            return
        index = options.index(focused)
        target_index = index + direction
        if not 0 <= target_index < len(options):
            return
        container = self.query_one("#config-roster-options", containers.Vertical)
        target = options[target_index]
        if direction < 0:
            container.move_child(focused, before=target)
        else:
            container.move_child(focused, after=target)
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

    @on(Button.Pressed, "#cancel")
    def on_cancel_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#save")
    async def on_save_button(self) -> None:
        await self.action_save()

    @on(Button.Pressed, "#config-roster-up")
    def on_roster_up_button(self) -> None:
        self.action_move_roster_up()

    @on(Button.Pressed, "#config-roster-down")
    def on_roster_down_button(self) -> None:
        self.action_move_roster_down()

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def action_save(self) -> None:
        roster = self._read_roster()
        if not roster:
            self.notify(
                "Select at least one agent for the next workspace",
                title="Roster",
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
                    self.notify(f"{field.title} needs a valid {field.type} value", severity="error")
                    control.focus()
                    return
                self.app.settings.set(field.key, value)
        self.app.settings.set("launcher.roster", "\n".join(roster))
        await self.app.save_settings()
        self.app.notify("Configuration saved", title="Wingmen", severity="information")
        self.dismiss(True)
