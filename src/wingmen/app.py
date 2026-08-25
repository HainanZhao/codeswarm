import asyncio
from importlib.resources import files
from functools import cached_property
import os
from pathlib import Path
import json
from time import monotonic
from typing import Callable, ClassVar, Sequence, TYPE_CHECKING

from rich import terminal_theme

from textual import events, on, work
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.reactive import var, reactive
from textual.app import App
from textual.signal import Signal
from textual.timer import Timer
from textual.notifications import Notify
from textual.screen import Screen
from textual.theme import Theme

import wingmen
from wingmen.db import DB, decode_session_meta
from wingmen.settings import Schema, Settings
from wingmen.agent_schema import Agent as AgentData
from wingmen import messages
from wingmen.settings_schema import SCHEMA
from wingmen import paths
from wingmen import atomic
from wingmen.session_tracker import SessionTracker

if TYPE_CHECKING:
    from wingmen.screens.main import MainScreen
    from wingmen.screens.store import StoreScreen
    from wingmen.widgets.conversation import Conversation
    from wingmen.db import DB


# A pure-black terminal palette with Wingmen's cool avionics accents.
WINGMEN_TERMINAL_THEME = terminal_theme.TerminalTheme(
    background=(0, 0, 0),  # #000000
    foreground=(216, 222, 233),  # #D8DEE9
    normal=[
        (59, 66, 82),  # black - #3B4252
        (251, 113, 133),  # red - coral #FB7185
        (52, 211, 153),  # green - emerald #34D399
        (167, 139, 250),  # yellow slot - violet #A78BFA
        (56, 189, 248),  # blue - sky #38BDF8
        (167, 139, 250),  # magenta - violet #A78BFA
        (45, 212, 191),  # cyan - teal #2DD4BF
        (229, 233, 240),  # white - #E5E9F0
    ],
    bright=[
        (76, 86, 106),  # bright black - #4C566A
        (253, 164, 175),  # bright red - coral #FDA4AF
        (110, 231, 183),  # bright green - emerald #6EE7B7
        (196, 181, 253),  # bright yellow slot - violet #C4B5FD
        (125, 211, 252),  # bright blue - sky #7DD3FC
        (196, 181, 253),  # bright magenta - violet #C4B5FD
        (94, 234, 212),  # bright cyan - teal #5EEAD4
        (236, 239, 244),  # bright white - #ECEFF4
    ],
)

WINGMEN_BLACK_THEME = Theme(
    name="wingmen-black",
    primary="#2DD4BF",
    secondary="#14B8A6",
    warning="#A78BFA",
    error="#FB7185",
    success="#34D399",
    accent="#67E8F9",
    foreground="#D8DEE9",
    background="#000000",
    surface="#0A0D12",
    panel="#10151D",
    dark=True,
    variables={
        "agent-tone-0": "#2DD4BF",
        "agent-tone-1": "#38BDF8",
        "agent-tone-2": "#A78BFA",
        "agent-tone-3": "#FB7185",
        "block-cursor-background": "#2DD4BF",
        "block-cursor-foreground": "#000000",
        "input-selection-background": "#14B8A6 35%",
        "button-color-foreground": "#000000",
    },
)


def get_store_screen() -> StoreScreen:
    """Get the store screen (lazily loaded)."""
    from wingmen.screens.store import StoreScreen

    return StoreScreen()


def is_agent_snapshot(value: object) -> bool:
    """Whether persisted session metadata contains a usable agent record."""
    if not isinstance(value, dict):
        return False
    string_fields = {
        "identity",
        "name",
        "short_name",
        "url",
        "protocol",
        "type",
        "author_name",
        "author_url",
        "publisher_name",
        "publisher_url",
        "description",
        "help",
    }
    return (
        all(isinstance(value.get(field), str) for field in string_fields)
        and isinstance(value.get("tags"), list)
        and isinstance(value.get("run_command"), dict)
        and isinstance(value.get("actions"), dict)
    )


class WingmenApp(App, inherit_bindings=False):
    """The top level app."""

    CSS_PATH = "wingmen.tcss"
    # Slash commands are the single command surface for a conversation. The
    # framework command palette duplicates that surface and clutters the footer.
    ENABLE_COMMAND_PALETTE = False
    MODES = {"store": get_store_screen}
    BINDING_GROUP_TITLE = "System"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(
            "ctrl+c",
            "interrupt_or_quit",
            "Interrupt / quit",
            tooltip="Cancel active agent work; press again to quit.",
            system=True,
            priority=True,
        ),
    ]
    ALLOW_IN_MAXIMIZED_VIEW = ""

    _settings = var(dict)
    scrollbar: reactive[str] = reactive("normal")
    terminal_title: var[str] = var("Wingmen")
    terminal_title_icon: var[str] = var("✈")
    terminal_title_flash = var(0)
    terminal_title_blink = var(False)
    project_dir = var(Path)

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]

    PAUSE_GC_ON_SCROLL = True

    def __init__(
        self,
        agent_data: AgentData | None = None,
        project_dir: str | None = None,
        mode: str | None = None,
        peers: Sequence[AgentData] = (),
        first_agent: int = 0,
        setup_prompt: bool = False,
        max_rounds: int = 100,
    ) -> None:
        """Wingmen app.

        Args:
            agent_data: Agent data to run.
            project_dir: Project directory.
            mode: Initial mode.
            agent: Agent identity or short name.
            peers: Additional agents forming a relay roster alongside
                `agent_data`. Empty for a solo session.
        """
        self.settings_changed_signal: Signal[tuple[str, object]] = Signal(
            self, "settings_changed"
        )
        self.agent_data = agent_data
        self.peers = list(peers)
        self.first_agent = first_agent
        self.setup_prompt = setup_prompt
        self.max_rounds = max_rounds

        self._initial_mode = mode
        self._conversations: set["Conversation"] = set()
        self._quitting = False
        self._launch_lock = asyncio.Lock()
        self._supports_pyperclip: bool | None = None
        self._terminal_title_flash_timer: Timer | None = None
        self._interrupt_requested_at: float | None = None

        self._session_tracker = SessionTracker()

        super().__init__()
        self.project_dir = Path(project_dir or "./").expanduser().resolve()

    @property
    def settings_path(self) -> Path:
        return paths.get_config() / "wingmen.json"

    async def get_db(self) -> DB:
        """Get an instance of the database."""
        db = DB()
        return db

    @cached_property
    def settings_schema(self) -> Schema:
        return Schema(SCHEMA)

    @cached_property
    def settings(self) -> Settings:
        """App settings"""
        return Settings(
            self.settings_schema, self._settings, on_set_callback=self.setting_updated
        )

    @property
    def session_tracker(self) -> SessionTracker:
        return self._session_tracker

    def copy_to_clipboard(self, text: str) -> None:
        """Override copy to clipboard to use pyperclip first, then OSC 52.

        Args:
            text: Text to copy.
        """
        if self._supports_pyperclip is None:
            try:
                import pyperclip
            except ImportError:
                self._supports_pyperclip = False
            else:
                self._supports_pyperclip = True

        if self._supports_pyperclip:
            import pyperclip

            try:
                pyperclip.copy(text)
            except Exception:
                pass
        super().copy_to_clipboard(text)

    @on(events.TextSelected)
    def copy_finished_selection(self) -> None:
        """Copy mouse-selected conversation text without requiring a shortcut.

        Terminal applications receive mouse selection events, but macOS terminals
        commonly consume Command+C themselves. Copying when the drag finishes keeps
        selection useful regardless of which terminal hosts Wingmen.
        """
        if selected_text := self.screen.get_selected_text():
            self.copy_to_clipboard(selected_text)

    def update_terminal_title(self) -> None:
        """Update the terminal title."""
        screen_title = self.screen.title

        title = (
            f"{self.terminal_title} — {screen_title}"
            if screen_title
            else self.terminal_title
        )
        icon = self.terminal_title_icon
        blink = self.terminal_title_blink

        if self.terminal_title_flash:
            if blink:
                terminal_title = f"{icon} {title}"
            else:
                terminal_title = f"👉 {title}" if title else icon
        else:
            terminal_title = f"{icon} {title}"

        if driver := self._driver:
            driver.write(f"\033]0;{terminal_title}\007")

    def watch_terminal_title_blink(self) -> None:
        self.update_terminal_title()

    def watch_terminal_title_flash(self, terminal_title_flash: int) -> None:

        if not self.settings.get("notifications.blink_title", bool):
            # Ignore if blink title is disabled
            return

        def toggle_blink() -> None:
            self.terminal_title_blink = not self.terminal_title_blink

        if terminal_title_flash:
            if self._terminal_title_flash_timer is None:
                self._terminal_title_flash_timer = self.set_interval(0.5, toggle_blink)
        else:
            if self._terminal_title_flash_timer is not None:
                self._terminal_title_flash_timer.stop()
                self.terminal_title_blink = False
                self._terminal_title_flash_timer = None
        self.update_terminal_title()

    def watch_terminal_title(self, title: str) -> None:
        self.update_terminal_title()

    def terminal_alert(self, flash: bool = True) -> None:
        if flash:
            self.terminal_title_flash += 1
        else:
            self.terminal_title_flash = max(0, self.terminal_title_flash - 1)

    @work(thread=True, exit_on_error=False)
    def system_notify(
        self, message: str, *, title: str = "", sound: str | None = None
    ) -> None:
        """Use OS level notifications.

        Args:
            message: Message to display.
            title: Title of the notificaiton.
            sound: filename (minus .wav) of a sound effect in the sounds/ directory.
        """
        system_notifications = self.settings.get("notifications.system", str)
        if not (
            system_notifications == "always"
            or (system_notifications == "blur" and not self.app_focus)
        ):
            return

        from notifypy import Notify

        notification = Notify()
        notification.message = message
        notification.title = title
        notification.application_name = "Wingmen"
        if sound and self.settings.get("notifications.enable_sounds", bool):
            sound_path = str(files("wingmen.data").joinpath(f"sounds/{sound}.wav"))
            notification.audio = sound_path

        notification.send()

    def _on_notify(self, event: Notify) -> None:
        """Use the conversation ribbon once its prompt is available."""
        self._forward_system_notification(event)

        from wingmen.widgets.conversation import Conversation

        conversation = self.screen.query_one_optional(Conversation)
        if conversation is None:
            return

        notification = event.notification
        content = Content(notification.message)
        if notification.title:
            content = Content.assemble(
                (f"{notification.title}: ", "bold"),
                content,
            )
        style = {
            "information": "default",
            "warning": "warning",
            "error": "error",
        }[notification.severity]
        conversation.flash(
            content,
            duration=notification.timeout,
            style=style,
        )
        event.prevent_default()

    def _forward_system_notification(self, event: Notify) -> None:
        """Forward eligible notifications to the operating system."""
        system_notifications = self.settings.get("notifications.system", str)
        if system_notifications == "always" or (
            system_notifications == "blur" and not self.app_focus
        ):
            hide_low_severity = self.settings.get(
                "notifications.hide_low_severity", bool
            )
            if event.notification.markup:
                # Strip content markup
                message = Content.from_markup(event.notification.message).plain
            else:
                message = event.notification.message
            if not (hide_low_severity and event.notification.severity == "information"):
                self.system_notify(message, title=event.notification.title)

    async def save_settings(self, force: bool = False) -> None:
        """Save settings in a thread.

        Args:
            force: Force saving, even when no change detected.

        """
        await asyncio.to_thread(self._save_settings, force=force)

    def _save_settings(self, force: bool = False) -> None:
        """Save the settings if they have changed."""
        if force or self.settings.changed:
            path = str(self.settings_path)
            try:
                atomic.write(path, self.settings.json)
            except Exception as error:
                self.notify(str(error), title="Settings", severity="error")
            else:
                self.settings.up_to_date()

    def setting_updated(self, key: str, value: object) -> None:
        if key == "ui.theme":
            self.theme = WINGMEN_BLACK_THEME.name
        elif key == "ui.scrollbar":
            if isinstance(value, str):
                self.scrollbar = value
        elif key == "ui.density":
            compact = value == "compact"
            self.set_class(compact, "-compact-input")
            self.set_class(compact, "-hide-status-line")
            self.set_class(compact, "-hide-agent-title")
            self.set_class(compact, "-hide-info-bar")
        elif key == "agent.thoughts":
            self.set_class(not bool(value), "-hide-thoughts")
        self.settings_changed_signal.publish((key, value))

    async def on_load(self) -> None:
        self.register_theme(WINGMEN_BLACK_THEME)
        db = await self.get_db()
        await db.create()
        settings_path = self.settings_path
        if settings_path.exists():
            try:
                loaded_settings = json.loads(settings_path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                settings = {}
                self.notify(
                    "Settings could not be read; using defaults. "
                    "The existing file was left unchanged.",
                    title="Settings",
                    severity="warning",
                )
            else:
                if isinstance(loaded_settings, dict):
                    settings = loaded_settings
                else:
                    settings = {}
                    self.notify(
                        "Settings must be a JSON object; using defaults. "
                        "The existing file was left unchanged.",
                        title="Settings",
                        severity="warning",
                    )
        else:
            settings = {}
            settings_path.write_text(
                json.dumps(settings, indent=4, separators=(", ", ": ")), "utf-8"
            )
            self.notify(f"Wrote default settings to {settings_path}", title="Settings")
        theme_migrated = False
        ui_settings = settings.get("ui")
        if (
            isinstance(ui_settings, dict)
            and ui_settings.get("theme") != WINGMEN_BLACK_THEME.name
        ):
            ui_settings["theme"] = WINGMEN_BLACK_THEME.name
            theme_migrated = True
        self.ansi_theme_dark = WINGMEN_TERMINAL_THEME
        self._settings = settings
        self.settings.set_all()
        if theme_migrated:
            await self.save_settings(force=True)

    async def new_session_screen(
        self, get_screen: Callable[[], Screen]
    ) -> str:
        mode_name = self._session_tracker.new_session()

        def make_screen() -> Screen:
            screen = get_screen()
            screen.id = mode_name
            return screen

        self.add_mode(mode_name, make_screen)
        await self.switch_mode(mode_name)
        return mode_name

    async def replace_live_conversations(self) -> None:
        """Stop and discard all active conversation screens.

        Wingmen presents one active workspace. Returning to the agent store is
        a way to start or resume a replacement workspace, not a way to leave
        an invisible agent process behind. Stop conversations before removing
        their modes so each ACP subprocess receives its normal shutdown path.
        """
        mode_names = tuple(self._session_tracker.sessions)
        if not mode_names:
            return

        await asyncio.gather(
            *(conversation.shutdown() for conversation in tuple(self._conversations)),
            return_exceptions=True,
        )
        for mode_name in mode_names:
            self._session_tracker.close_session(mode_name)
            self.remove_mode(mode_name)

    async def on_mount(self) -> None:
        if mode := self._initial_mode:
            self.switch_mode(mode)
        else:
            await self.new_session_screen(self.get_main_screen)

        if self.setup_prompt:
            self.notify(
                "No saved agent roster yet. Choose one or more agents in "
                "the store to get started.",
                title="Wingmen setup",
                severity="warning",
                timeout=10,
            )

        self.update_terminal_title()
        self.set_process_title()

    @work(thread=True, exit_on_error=False)
    def set_process_title(self) -> None:
        try:
            import setproctitle

            setproctitle.setproctitle("wingmen")
        except Exception:
            pass

    def get_main_screen(self) -> MainScreen:
        """Make the default screen.

        Returns:
            Instance of `MainScreen`
        """
        # Lazy import
        from wingmen.screens.main import MainScreen

        project_path = Path(self.project_dir or "./").resolve().absolute()
        return MainScreen(
            project_path,
            self.agent_data,
            peers=self.peers,
            first_agent=self.first_agent,
            max_rounds=self.max_rounds,
        ).data_bind(scrollbar=WingmenApp.scrollbar)

    async def action_interrupt_or_quit(self) -> None:
        """Follow terminal convention: interrupt work before exiting Wingmen."""
        selected_text = self.screen.get_selected_text()
        if selected_text:
            self.copy_to_clipboard(selected_text)
            self.screen.clear_selection()
            return

        active_conversations = [
            conversation
            for conversation in self._conversations
            if conversation.has_interruptible_work
        ]
        now = monotonic()
        if active_conversations:
            if (
                self._interrupt_requested_at is not None
                and now - self._interrupt_requested_at < 3
            ):
                await self.action_quit()
                return

            self._interrupt_requested_at = now
            results = await asyncio.gather(
                *(
                    conversation.cancel_active_work()
                    for conversation in active_conversations
                ),
                return_exceptions=True,
            )
            for conversation, result in zip(active_conversations, results):
                if result is True:
                    conversation.flash(
                        "Cancellation requested — Ctrl+C again to quit",
                        style="warning",
                    )
                else:
                    conversation.flash(
                        "Stopping work — Ctrl+C again to quit", style="warning"
                    )
            return

        await self.action_quit()

    def clear_interrupt_request(self) -> None:
        """Forget a prior turn's Ctrl+C confirmation window."""
        self._interrupt_requested_at = None

    async def action_quit(self) -> None:
        """An [action](/guide/actions) to quit the app as soon as possible."""
        if self._quitting:
            return
        self._quitting = True

        self.screen.set_focus(None)

        await asyncio.gather(
            *(conversation.shutdown() for conversation in tuple(self._conversations)),
            return_exceptions=True,
        )
        try:
            await self.save_settings()
        finally:
            self.exit()

    def register_conversation(self, conversation: "Conversation") -> None:
        """Track live conversations so application shutdown stops every agent."""
        self._conversations.add(conversation)

    def unregister_conversation(self, conversation: "Conversation") -> None:
        self._conversations.discard(conversation)

    @on(messages.LaunchAgent)
    def on_launch_agent(self, message: messages.LaunchAgent) -> None:
        self.launch_agent(
            message.identity,
            agent_session_id=message.session_id,
            session_pk=message.pk,
            initial_prompt=message.prompt,
            peer_identities=message.peers,
        )

    @work
    async def launch_agent(
        self,
        agent_identity: str,
        *,
        agent_session_id: str | None = None,
        session_pk: int | None = None,
        project_path: Path | None = None,
        initial_prompt: str | None = None,
        peer_identities: Sequence[str] = (),
        first_agent: int = 0,
    ) -> None:
        async with self._launch_lock:
            await self._launch_agent(
                agent_identity,
                agent_session_id=agent_session_id,
                session_pk=session_pk,
                project_path=project_path,
                initial_prompt=initial_prompt,
                peer_identities=peer_identities,
                first_agent=first_agent,
            )

    async def _launch_agent(
        self,
        agent_identity: str,
        *,
        agent_session_id: str | None = None,
        session_pk: int | None = None,
        project_path: Path | None = None,
        initial_prompt: str | None = None,
        peer_identities: Sequence[str] = (),
        first_agent: int = 0,
    ) -> None:
        """Resolve and start one workspace while the launch lock is held."""
        from wingmen.screens.main import MainScreen
        from wingmen.agent_schema import Agent
        from wingmen.agents import AgentReadError, read_agents

        agent: Agent | None = None
        agents: dict[str, Agent] | None = None
        if session_pk is not None:
            db = DB()
            session = await db.session_get(session_pk)
            if session is not None:
                agent_data = decode_session_meta(session["meta_json"]).get(
                    "agent_data"
                )
                if is_agent_snapshot(agent_data):
                    agent = agent_data  # type: ignore[assignment]

        if agent is None:
            try:
                agents = await read_agents()
            except AgentReadError as error:
                self.notify(
                    f"Unable to read the agent catalog: {error}",
                    title="Launch agent",
                    severity="error",
                )
                return
            try:
                agent = agents[agent_identity]
            except KeyError:
                self.notify("Agent not found", title="Launch agent", severity="error")
                return
        peers: list[Agent] = []
        if peer_identities:
            if agents is None:
                try:
                    agents = await read_agents()
                except AgentReadError as error:
                    self.notify(
                        f"Unable to read the agent catalog: {error}",
                        title="Launch agent",
                        severity="error",
                    )
                    return
            for identity in peer_identities:
                peer = agents.get(identity)
                if peer is None:
                    self.notify(
                        f"Agent not found: {identity}",
                        title="Launch agent",
                        severity="error",
                    )
                    return
                peers.append(peer)
        if project_path is None:
            project_path = Path(self.project_dir or os.getcwd())

        def get_screen():
            screen = MainScreen(
                project_path,
                agent,
                agent_session_id,
                session_pk=session_pk,
                initial_prompt=initial_prompt,
                peers=peers,
                first_agent=first_agent,
            )

            return screen

        await self.replace_live_conversations()
        await self.new_session_screen(get_screen)
