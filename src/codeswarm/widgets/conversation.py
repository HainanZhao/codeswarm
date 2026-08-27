from __future__ import annotations

import asyncio
from datetime import datetime

from collections import deque
from contextlib import suppress
from operator import attrgetter
from typing import TYPE_CHECKING, Literal
from pathlib import Path
from time import monotonic

from typing import Sequence

from textual import on, work
from textual.app import ComposeResult
from textual import containers
from textual import getters
from textual import events
from textual.actions import SkipAction
from textual.binding import Binding
from textual.content import Content
from textual.geometry import clamp
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets.markdown import MarkdownBlock, MarkdownFence
from textual.geometry import Offset, Spacing
from textual.reactive import var
from textual.layouts.grid import GridLayout
from textual.layout import WidgetPlacement
from textual.timer import Timer
from textual.worker import WorkerCancelled, WorkerError


from codeswarm import jsonrpc, messages
from codeswarm import paths
from codeswarm.agent_schema import Agent as AgentData
from codeswarm.acp import messages as acp_messages
from codeswarm.acp.agent import Mode
from codeswarm.acp.relay import DEFAULT_STOP_ACKNOWLEDGMENT, STOP_TOKEN, RelayResult
from codeswarm.app import CodeSwarmApp
from codeswarm.agent import AgentBase, AgentReady, AgentFail
from codeswarm.session import SessionCoordinator
from codeswarm.widgets.conversation_acp import ConversationACPHandlers
from codeswarm.format_path import format_path
from codeswarm.history import History
from codeswarm.mode_policy import (
    DEFAULT_MODE_POLICY_ID,
    POLICIES_BY_ID,
    STARTUP_FULL_ACCESS_MODE,
    shared_current_mode,
    shared_modes,
)
from codeswarm.widgets.flash import Flash
from codeswarm.widgets.note import Note
from codeswarm.widgets.prompt import Prompt, QueuedMessages
from codeswarm.widgets.user_input import UserInput
from codeswarm.widgets.agent_response import format_reply_timestamp
from codeswarm.acp.relay import MAX_QUEUED_PROMPTS
from codeswarm.slash_command import SlashCommand
from codeswarm.protocol import BlockProtocol, BlockContentProtocol, ExpandProtocol

if TYPE_CHECKING:
    from codeswarm.widgets.agent_response import AgentMessage, AgentResponse
    from codeswarm.widgets.agent_thought import AgentThought
    from codeswarm.widgets.terminal_tool import TerminalTool


AGENT_FAIL_HELP = {
    "fail": """\
## Agent failed to run

**The agent failed to start.**

Check that the agent is installed and up-to-date.

Some agents require an ACP adapter. Install or update the agent according to
its upstream documentation, then restart CodeSwarm.
""",
    "no_resume": """\
## Agent does not support resume

The agent or ACP adapter does not support resuming sessions.

Update the agent and ACP adapter according to their upstream documentation,
then start a new workspace.

- Use `/close` to return to the launcher, or exit with
  `Ctrl+C`.
- Select the agent and press `Enter` to start a fresh workspace.
""",
}

MAX_AGENT_ERROR_DISPLAY_CHARS = 4_000
AGENT_TURN_WORKER_GROUP = "agent-turn"

STOP_REASON_MAX_TOKENS = """\
## Maximum tokens reached

$AGENT reported that your account is out of tokens.

- You may need to purchase additional tokens, or fund your account.
- If your account has tokens, try running any login or auth process again.
"""

STOP_REASON_MAX_TURN_REQUESTS = """\
## Maximum model requests reached

$AGENT has exceeded the maximum number of model requests in a single turn.
"""

STOP_REASON_REFUSAL = """\
## Agent refusal

$AGENT has refused to continue.
"""

DISCUSSION_INSTRUCTIONS = """\
Discuss the question at a high level. Do not inspect files, search the shared
workspace, run terminal commands, call tools, or make edits. Answer from the
user's prompt and general knowledge only. State uncertainty rather than
checking the codebase.
"""

DISCUSSION_MODE = Mode(
    "codeswarm:discuss",
    "Chat",
    "Chat without inspecting the workspace or using tools",
)
NATIVE_MODE = Mode(
    "codeswarm:native",
    "Agent Default",
    "The agent is using a native mode without a CodeSwarm equivalent",
)


class Contents(containers.VerticalGroup, can_focus=False):
    BLANK = True

    def process_layout(
        self, placements: list[WidgetPlacement]
    ) -> list[WidgetPlacement]:
        if placements:
            last_placement = placements[-1]
            top, right, _bottom, left = last_placement.margin
            placements[-1] = last_placement._replace(
                margin=Spacing(top, right, 0, left)
            )
        return placements


class ContentsGrid(containers.Grid):
    BLANK = True

    def pre_layout(self, layout) -> None:
        assert isinstance(layout, GridLayout)
        layout.stretch_height = True


class Window(containers.VerticalScroll):
    HELP = """\
## Conversation

This is a view of your conversation with the agent.

- **cursor keys** Scroll
- **alt+up / alt+down** Navigate content
- **c** Copy the highlighted block to the clipboard
- **p** Copy the highlighted block into the prompt
- **start typing** Focus the prompt
"""
    BINDING_GROUP_TITLE = "View"
    BINDINGS = [Binding("end", "screen.focus_prompt", "Prompt")]

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.follow_output = True
        self._programmatic_scroll = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._programmatic_scroll or round(old_value) == round(new_value):
            return
        # A user scroll changes this state. Content growth may leave scroll_y
        # unchanged while the maximum grows, so it does not accidentally turn
        # following off between streamed fragments.
        self.follow_output = self.is_vertical_scroll_end

    def scroll_end_for_output(self) -> None:
        """Follow streamed output without treating the move as user scrolling."""
        self._programmatic_scroll = True
        try:
            self.scroll_end(animate=False, immediate=True)
        finally:
            self._programmatic_scroll = False

    def update_node_styles(self, animate: bool = True) -> None:
        pass


class Conversation(ConversationACPHandlers, containers.Vertical):
    """Holds the agent conversation (input, output, and various controls / information)."""

    BLANK = True
    BINDING_GROUP_TITLE = "Conversation"
    CURSOR_BINDING_GROUP = Binding.Group(description="Cursor")
    BINDINGS = [
        Binding(
            "alt+up",
            "cursor_up",
            "Block cursor up",
            priority=True,
            group=CURSOR_BINDING_GROUP,
            show=False,
        ),
        Binding(
            "alt+down",
            "cursor_down",
            "Block cursor down",
            group=CURSOR_BINDING_GROUP,
            show=False,
        ),
        Binding(
            "space",
            "expand_block",
            "Expand",
            key_display="␣",
            tooltip="Expand cursor block",
            show=False,
        ),
        Binding(
            "space",
            "collapse_block",
            "Collapse",
            key_display="␣",
            tooltip="Collapse cursor block",
            show=False,
        ),
        Binding(
            "c",
            "copy_to_clipboard",
            "Copy",
            tooltip="Copy the highlighted block to the clipboard",
            show=False,
        ),
        Binding(
            "p",
            "copy_to_prompt",
            "Copy to prompt",
            tooltip="Copy the highlighted block into the prompt",
            show=False,
        ),
        Binding(
            "ctrl+shift+p",
            "toggle_pause",
            "Pause/Resume",
            key_display="⌃⇧P",
            tooltip="Pause or resume all agents",
        ),
        Binding(
            "ctrl+o",
            "mode_switcher",
            "Modes",
            tooltip="Open the mode switcher",
            show=False,
        ),
        Binding(
            # Not ctrl+w: TextArea binds that to delete-word-left, and the
            # prompt holds focus almost all the time, so this action would
            # be advertised in the footer but never fire.
            "f4",
            "close_session",
            "Close session",
            tooltip="Close the current session (f4)",
            show=False,
        ),
    ]

    busy_count = var(0)
    cursor_offset = var(-1, init=False)
    project_path = var("")
    working_directory: var[str] = var("")
    _blocks: var[list[MarkdownBlock] | None] = var(None)

    contents = getters.query_one(Contents)
    window = getters.query_one(Window)
    prompt = getters.query_one(Prompt)
    app = getters.app(CodeSwarmApp)

    prompt_history_index: var[int] = var(0, init=False)

    agent: var[AgentBase | None] = var(None, bindings=True)
    agent_info: var[Content] = var(Content())
    agent_ready: var[bool] = var(False)
    modes: var[dict[str, Mode]] = var({}, bindings=True)
    current_mode: var[Mode | None] = var(None)
    turn: var[Literal["agent", "client"] | None] = var(None, bindings=True)
    relay_paused: var[bool] = var(False, toggle_class="-relay-paused")
    discussion_mode: var[bool] = var(False, toggle_class="-discussion-mode")
    collaboration_mode = var("Roster")
    status: var[str | Content] = var("")
    queued_messages: var[tuple[str, ...]] = var(())

    title = var("")

    def __init__(
        self,
        project_path: Path,
        agent: AgentData | None = None,
        agent_session_id: str | None = None,
        session_pk: int | None = None,
        initial_prompt: str | None = None,
        peers: Sequence[AgentData] = (),
        first_agent: int = 0,
        max_rounds: int = 100,
    ) -> None:
        super().__init__()

        project_path = project_path.resolve().absolute()

        self.set_reactive(Conversation.project_path, project_path)
        self.set_reactive(Conversation.working_directory, str(project_path))
        self.agent_slash_commands: list[SlashCommand] = []
        self._agent_slash_commands: dict[int, list[SlashCommand]] = {}
        self.terminals: dict[str, TerminalTool] = {}
        self._local_shells: set[TerminalTool] = set()
        self._working_agent: AgentBase | None = None
        self._agent_started_at: float | None = None
        self._agent_elapsed: dict[int, int] = {}
        self._agent_status_timer: Timer | None = None
        self._collaboration_complete = False
        self._agent_response: AgentResponse | None = None
        self._agent_message: AgentMessage | None = None
        self._agent_thought: AgentThought | None = None
        self._response_agent: AgentBase | None = None
        self.session = SessionCoordinator(
            project_path,
            agent,
            session_id=agent_session_id,
            session_pk=session_pk,
            peers=peers,
            first_agent=first_agent,
            max_rounds=max_rounds,
        )
        self.set_reactive(
            Conversation.collaboration_mode,
            self.session.collaboration_mode.title(),
        )
        self._pending_collaboration_mode: str | None = None
        self._mouse_down_offset: Offset | None = None

        self.project_data_path = paths.get_project_data(project_path)
        self.prompt_history = History(self.project_data_path / "prompt_history.jsonl")
        self._require_check_prune = False

        self._shutdown = False

        self._initial_prompt = initial_prompt
        self._ready_agents: set[int] = set()
        self._active_relay_agent: AgentBase | None = None
        self._mode_agent: AgentBase | None = None
        self._agent_modes: dict[int, tuple[dict[str, Mode], str | None]] = {}
        self._unattributed_modes: tuple[dict[str, Mode], str | None] = ({}, None)
        self._desired_mode_policy_id = DEFAULT_MODE_POLICY_ID
        self._mode_sync_lock = asyncio.Lock()
        self._mode_sync_failure: str | None = None

        self._post_lock = asyncio.Lock()
        self._pending_solo_prompts: deque[str] = deque()
        self._queued_prompt_previews: deque[tuple[str, bool, str]] = deque()

    def update_title(self) -> None:
        """Update the screen title."""

        if agent_title := self.agent_title:
            project_path = format_path(self.project_path)
            self.screen.title = f"{agent_title} {project_path}"
        else:
            self.screen.title = ""

    @property
    def _relay_active(self) -> bool:
        return self.session.relay_active

    @property
    def agent_title(self) -> str | None:
        if self.agent is not None:
            info = self.agent.get_info()
            return info.plain if isinstance(info, Content) else str(info)
        if self.session.owner_data is not None:
            return self.session.owner_data["name"]
        return None

    def validate_prompt_history_index(self, index: int) -> int:
        return clamp(index, -self.prompt_history.size, 0)

    def insert_path_into_prompt(self, path: Path) -> None:
        try:
            insert_path_text = str(path.relative_to(self.project_path))
        except Exception:
            self.app.bell()
            return

        insert_text = (
            f'@"{insert_path_text}"'
            if " " in insert_path_text
            else f"@{insert_path_text}"
        )
        self.prompt.prompt_text_area.insert(insert_text)
        self.prompt.prompt_text_area.insert(" ")

    def set_agent_modes(
        self,
        modes: dict[str, Mode],
        current_mode_id: str | None,
        agent: AgentBase | None = None,
    ) -> None:
        """Apply an agent's complete mode state while preserving the UI invariant.

        ACP agents may replace their available modes at any time. A mode selected
        before that update is not necessarily present afterwards, so consumers
        must only ever see a current mode that belongs to ``modes``.
        """
        mode_agent = agent or self.agent
        if mode_agent is not None:
            modes, current_mode_id = self._effective_agent_mode_state(
                mode_agent, modes, current_mode_id
            )
            self._agent_modes[id(mode_agent)] = (modes, current_mode_id)
        else:
            self._unattributed_modes = (modes, current_mode_id)
        if self._mode_agent is None:
            self._mode_agent = mode_agent
        self._refresh_displayed_modes()

    @staticmethod
    def _effective_agent_mode_state(
        agent: AgentBase,
        modes: dict[str, Mode],
        current_mode_id: str | None,
    ) -> tuple[dict[str, Mode], str | None]:
        """Add process-backed full access to an adapter's ACP mode state."""
        if not getattr(agent, "supports_startup_full_access", False):
            return modes, current_mode_id
        effective_modes = {
            **modes,
            STARTUP_FULL_ACCESS_MODE.id: STARTUP_FULL_ACCESS_MODE,
        }
        effective_current = (
            STARTUP_FULL_ACCESS_MODE.id
            if getattr(agent, "startup_full_access", False)
            else current_mode_id
        )
        return effective_modes, effective_current

    def _adopt_mode_replacement(
        self, previous: AgentBase, replacement: AgentBase
    ) -> None:
        """Move conversation-owned state to a process-mode replacement."""
        self._ready_agents.discard(id(previous))
        self._agent_modes.pop(id(previous), None)
        self._agent_elapsed.pop(id(previous), None)
        self._agent_slash_commands.pop(id(previous), None)
        if self._working_agent is previous:
            self._working_agent = None
            self._agent_started_at = None
        if self._active_relay_agent is previous:
            self._active_relay_agent = replacement
        if self._mode_agent is previous:
            self._mode_agent = replacement
        self.agent = self.session.primary_agent
        active_agents = self.session.active_agents
        self.agent_ready = bool(active_agents) and all(
            id(agent) in self._ready_agents for agent in active_agents
        )
        self._refresh_roster_info()
        self._refresh_displayed_modes()
        self.update_slash_commands()

    def _refresh_displayed_modes(self) -> None:
        """Show one semantic mode surface for the complete active roster."""
        active_agents = self.session.active_agents
        states = [self._agent_modes.get(id(agent)) for agent in active_agents]
        if active_agents and not all(state is not None for state in states):
            self.modes = {DISCUSSION_MODE.id: DISCUSSION_MODE}
            self.current_mode = DISCUSSION_MODE if self.discussion_mode else None
            return
        if active_agents:
            complete_states = [state for state in states if state is not None]
            roster_modes = shared_modes(modes for modes, _current in complete_states)
            self.modes = {
                DISCUSSION_MODE.id: DISCUSSION_MODE,
                **roster_modes,
            }
            common_mode = shared_current_mode(complete_states)
            self.current_mode = DISCUSSION_MODE if self.discussion_mode else (
                common_mode
                or roster_modes.get(self._desired_mode_policy_id)
                or NATIVE_MODE
            )
            prompt = self.query_one_optional(Prompt)
            if prompt is not None:
                prompt.mode_owner = ""
            return

        if self._mode_agent is None:
            agent_modes, current_mode_id = self._unattributed_modes
            self.modes = {
                **agent_modes,
                DISCUSSION_MODE.id: DISCUSSION_MODE,
            }
            self.current_mode = (
                DISCUSSION_MODE
                if self.discussion_mode
                else agent_modes.get(current_mode_id) if current_mode_id else None
            )
            return
        agent_modes, current_mode_id = self._agent_modes.get(
            id(self._mode_agent), ({}, None)
        )
        self.modes = {
            **agent_modes,
            DISCUSSION_MODE.id: DISCUSSION_MODE,
        }
        self.current_mode = (
            DISCUSSION_MODE
            if self.discussion_mode
            else agent_modes.get(current_mode_id) if current_mode_id else None
        )

    def _select_agent_modes(self, agent: AgentBase | None) -> None:
        """Select agent-owned commands while keeping modes roster-wide."""
        self._mode_agent = agent
        prompt = self.query_one_optional(Prompt)
        if agent is not None:
            self.agent_slash_commands = self._agent_slash_commands.get(id(agent), [])
            if prompt is not None:
                prompt.slash_commands = self._build_slash_commands()
        self._refresh_displayed_modes()

    async def set_shared_mode(self, policy_id: str) -> None:
        """Translate one CodeSwarm policy and apply it to every active agent."""
        policy = POLICIES_BY_ID.get(policy_id)
        if policy is None:
            self.notify("Unknown CodeSwarm mode", title="Set Mode", severity="error")
            return

        self._desired_mode_policy_id = policy_id
        if self.turn == "agent" and any(
            getattr(agent, "supports_startup_full_access", False)
            for agent in self.session.active_agents
        ):
            self.flash("Mode change queued until the active turn lands")
            return
        await self._sync_desired_mode()

    async def _sync_desired_mode(self) -> bool:
        """Keep every active adapter on CodeSwarm's desired permission policy."""
        async with self._mode_sync_lock:
            policy = POLICIES_BY_ID[self._desired_mode_policy_id]
            active_agents = self.session.active_agents
            states = [self._agent_modes.get(id(agent)) for agent in active_agents]
            if not active_agents or not all(state is not None for state in states):
                return False

            targets: list[tuple[AgentBase, Mode]] = []
            startup_targets: list[tuple[AgentBase, bool]] = []
            unsupported: list[str] = []
            for agent, state in zip(active_agents, states):
                assert state is not None
                modes, current_mode = state
                native_mode = policy.resolve(modes)
                if native_mode is None:
                    unsupported.append(str(agent.get_info()))
                elif native_mode.id == STARTUP_FULL_ACCESS_MODE.id:
                    if not getattr(agent, "startup_full_access", False):
                        startup_targets.append((agent, True))
                elif getattr(agent, "supports_startup_full_access", False) and getattr(
                    agent, "startup_full_access", False
                ):
                    startup_targets.append((agent, False))
                elif current_mode != native_mode.id:
                    targets.append((agent, native_mode))

            if unsupported:
                failure = (
                    f"{policy.name} is not supported by: " + ", ".join(unsupported)
                )
                if failure != self._mode_sync_failure:
                    self.notify(failure, title="Set Mode", severity="error")
                self._mode_sync_failure = failure
                self._refresh_displayed_modes()
                return False

            results = await asyncio.gather(
                *(agent.set_mode(native.id) for agent, native in targets),
                return_exceptions=True,
            )
            failures: list[str] = []
            for (agent, native), result in zip(targets, results):
                if isinstance(result, BaseException):
                    failures.append(f"{agent.get_info()}: {result}")
                elif result is not None:
                    failures.append(f"{agent.get_info()}: {result}")
                else:
                    modes, _current = self._agent_modes[id(agent)]
                    self._agent_modes[id(agent)] = (modes, native.id)

            for previous, enabled in startup_targets:
                replacement = await self.session.restart_for_startup_full_access(
                    previous, self, enabled=enabled
                )
                if replacement is None:
                    failures.append(
                        f"{previous.get_info()}: unable to restart permission mode"
                    )
                else:
                    self._adopt_mode_replacement(previous, replacement)

            self._refresh_displayed_modes()
            if failures:
                failure = "Mode could not be synchronized: " + "; ".join(failures)
                if failure != self._mode_sync_failure:
                    self.notify(failure, title="Set Mode", severity="error")
                self._mode_sync_failure = failure
                return False

            self._mode_sync_failure = None
            return True

    async def watch_prompt_history_index(self, previous_index: int, index: int) -> None:
        if previous_index == 0:
            self.prompt_history.current = self.prompt.text
        try:
            history_entry = await self.prompt_history.get_entry(index)
        except IndexError:
            pass
        else:
            self.prompt.text = history_entry["input"]

    @on(events.Key)
    async def on_key(self, event: events.Key):
        if (
            event.character is not None
            and event.is_printable
            and (event.character.isalnum() or event.character in "/@")
            and self.window.has_focus
        ):
            self.prompt.focus()
            self.prompt.prompt_text_area.post_message(event)

    def compose(self) -> ComposeResult:
        with Window():
            with ContentsGrid():
                yield Contents(id="contents")
        yield Flash()
        yield Prompt().data_bind(
            project_path=Conversation.project_path,
            working_directory=Conversation.working_directory,
            agent_info=Conversation.agent_info,
            agent_ready=Conversation.agent_ready,
            current_mode=Conversation.current_mode,
            modes=Conversation.modes,
            collaboration_mode=Conversation.collaboration_mode,
            status=Conversation.status,
            queued_messages=Conversation.queued_messages,
        )

    @on(messages.Flash)
    def on_flash(self, event: messages.Flash) -> None:
        event.stop()
        self.flash(event.content, duration=event.duration, style=event.style)

    def flash(
        self,
        content: str | Content,
        *,
        duration: float | None = None,
        style: Literal["default", "warning", "error", "success"] = "default",
    ) -> None:
        """Flash a single-line message to the user.

        Args:
            content: Content to flash.
            style: A semantic style.
            duration: Duration in seconds of the flash, or `None` to use default in settings.
        """
        self.query_one(Flash).flash(content, duration=duration, style=style)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "mode_switcher":
            return bool(self.modes)
        if action == "toggle_pause":
            return self._relay_active
        if action == "cancel":
            return True if (self.agent and self.turn == "agent") else None
        if action in {"expand_block", "collapse_block"}:
            if (cursor_block := self.cursor_block) is None:
                return False
            elif isinstance(cursor_block, ExpandProtocol):
                if action == "expand_block":
                    return False if cursor_block.is_block_expanded() else True
                else:
                    return True if cursor_block.is_block_expanded() else False
            return None if action == "expand_block" else False
        if action in {"copy_to_clipboard", "copy_to_prompt"}:
            return self.cursor_block is not None
        return True

    async def action_expand_block(self) -> None:
        if (cursor_block := self.cursor_block) is not None:
            if isinstance(cursor_block, ExpandProtocol):
                cursor_block.expand_block()
                self.refresh_bindings()

    async def action_collapse_block(self) -> None:
        if (cursor_block := self.cursor_block) is not None:
            if isinstance(cursor_block, ExpandProtocol):
                cursor_block.collapse_block()
                self.refresh_bindings()

    async def post_agent_response(self, fragment: str = "") -> AgentResponse | None:
        """Get or create an agent response widget."""
        from codeswarm.widgets.agent_response import AgentMessage, AgentResponse

        follow_output = self.window.follow_output
        async with self._post_lock:
            if self._agent_response is None:
                # ACP chunks carry their source agent. Prefer that immutable
                # attribution over the relay's mutable current-speaker field:
                # Textual may render a queued chunk after the relay advances.
                response_agent = (
                    self._response_agent or self._active_relay_agent or self.agent
                )
                agent_index = 0
                if response_agent is not None:
                    active_agents = self.session.active_agents
                    try:
                        agent_index = active_agents.index(response_agent)
                    except ValueError:
                        pass
                agent_response = AgentResponse(fragment)
                if response_agent is not None:
                    agent_response.add_class(f"-agent-tone-{agent_index % 4}")
                self._agent_response = agent_response
                if response_agent is not None:
                    agent_message = await self.ensure_agent_message(response_agent)
                    await agent_message.add_response(agent_response)
                else:
                    await self.post(agent_response, new_block=False)
            else:
                await self._agent_response.append_fragment(fragment)
            self._scroll_output_if_following(follow_output)
            return self._agent_response

    def _scroll_output_if_following(self, follow_output: bool) -> None:
        """Keep new streamed content visible without hijacking manual scrolling."""
        if follow_output:
            self.call_after_refresh(self.window.scroll_end_for_output)

    async def ensure_agent_message(self, agent: AgentBase) -> AgentMessage:
        """Get or create the attributed container for the current agent turn."""
        from codeswarm.widgets.agent_response import AgentMessage

        if self._agent_message is None:
            active_agents = self.session.active_agents
            try:
                agent_index = active_agents.index(agent)
            except ValueError:
                agent_index = 0
            replied_at = datetime.now().astimezone()
            previous_message = (
                self.contents.displayed_children[-1]
                if self.contents.displayed_children
                else None
            )
            self._agent_message = AgentMessage(
                source_agent=agent,
                speaker=self._agent_display_name(agent),
                timestamp=format_reply_timestamp(replied_at, now=replied_at),
                tone_index=agent_index,
            )
            if (
                isinstance(previous_message, AgentMessage)
                and previous_message.source_agent is agent
            ):
                previous_message.add_class("-continues")
                self._agent_message.add_class("-continuation")
            await self.post(self._agent_message, new_block=False)
        return self._agent_message

    def begin_agent_output(self, agent: AgentBase | None) -> None:
        """Open a distinct response stream when relay output changes source."""
        if agent is not None and agent is not self._response_agent:
            self._response_agent = agent
            self._agent_response = None
            self._agent_thought = None
            self._agent_message = None

    async def post_agent_thought(
        self,
        thought_fragment: str,
        agent: AgentBase | None = None,
    ) -> AgentThought | None:
        """Get or create an agent thought widget."""
        from codeswarm.widgets.agent_response import AgentMessage
        from codeswarm.widgets.agent_thought import AgentThought

        async with self._post_lock:
            agent_message: AgentMessage | None = None
            if agent is not None:
                agent_message = await self.ensure_agent_message(agent)
                agent_message.set_thinking(True)
            if self._agent_thought is None:
                if thought_fragment.strip():
                    self._agent_thought = AgentThought(thought_fragment)
                    if agent_message is not None:
                        await agent_message.add_thought(self._agent_thought)
                    else:
                        await self.post(self._agent_thought, new_block=False)
            else:
                await self._agent_thought.append_fragment(thought_fragment)
            return self._agent_thought

    def clear_agent_thinking(self) -> None:
        """Resolve the thinking indicator when another activity begins."""
        if self._agent_message is not None:
            self._agent_message.set_thinking(False)

    @property
    def cursor_block(self) -> Widget | None:
        """The block next to the cursor, or `None` if no block cursor."""
        if self.cursor_offset == -1 or not self.contents.displayed_children:
            return None
        try:
            block_widget = self.contents.displayed_children[self.cursor_offset]
        except IndexError:
            return None
        return block_widget

    @property
    def cursor_block_child(self) -> Widget | None:
        if (cursor_block := self.cursor_block) is not None:
            if isinstance(cursor_block, BlockProtocol):
                return cursor_block.get_cursor_block()
        return cursor_block

    def get_cursor_block[BlockType](
        self, block_type: type[BlockType] = Widget
    ) -> BlockType | None:
        """Get the cursor block if it matches a type.

        Args:
            block_type: The expected type.

        Returns:
            The widget next to the cursor, or `None` if the types don't match.
        """
        cursor_block = self.cursor_block_child
        if isinstance(cursor_block, block_type):
            return cursor_block
        return None

    @on(AgentReady)
    async def on_agent_ready(self, message: AgentReady) -> None:
        newly_connected = False
        if message.agent is not None:
            newly_connected = id(message.agent) not in self._ready_agents
            self._ready_agents.add(id(message.agent))
            self._refresh_roster_info()

        if self.agent_ready:
            # A newly connected member should still be identified if the
            # session has already started.
            if newly_connected and message.agent is not None:
                self.flash(
                    Content.assemble(
                        "COMMS // ",
                        message.agent.get_info(),
                        " LINK ESTABLISHED",
                    ),
                    style="success",
                )
            return

        active_agents = self.session.active_agents
        ready_agents = [
            agent for agent in active_agents if id(agent) in self._ready_agents
        ]
        waiting_agents = [
            agent for agent in active_agents if id(agent) not in self._ready_agents
        ]

        if message.agent is not None and waiting_agents:
            waiting_names = " + ".join(
                str(agent.get_info()) for agent in waiting_agents
            )
            self.flash(
                Content.assemble(
                    "COMMS // ",
                    message.agent.get_info(),
                    " LINK ESTABLISHED · AWAITING ",
                    waiting_names,
                ),
                style="success",
            )
            return

        if ready_agents:
            names = [str(agent.get_info()) for agent in ready_agents]
            connected_names = " + ".join(names)
            self.flash(
                Content.assemble(
                    "FORMATION // ", connected_names, " ON STATION"
                ),
                style="success",
            )
        elif message.agent is not None:
            # A custom/legacy agent may report readiness before its roster
            # entry is visible. Preserve a useful notification in that case.
            self.flash(
                Content.assemble(
                    "COMMS // ",
                    message.agent.get_info(),
                    " LINK ESTABLISHED",
                ),
                style="success",
            )
        elif self.agent is not None:
            self.flash(
                Content.assemble(
                    "COMMS // ", self.agent.get_info(), " LINK ESTABLISHED"
                ),
                style="success",
            )

        self.agent_ready = True

    async def on_unmount(self) -> None:
        await self.shutdown()
        self.app.unregister_conversation(self)

    async def shutdown(self) -> None:
        """Stop local watchers and every agent in this conversation once."""
        if self._shutdown:
            return
        self._shutdown = True
        if self._agent_status_timer is not None:
            self._agent_status_timer.stop()
            self._agent_status_timer = None
        for terminal in self.terminals.values():
            terminal.kill()
            terminal.release()
        self.terminals.clear()
        for terminal in self._local_shells:
            terminal.kill()
            terminal.release()
        self._local_shells.clear()
        await self.session.stop()

    @on(AgentFail)
    async def on_agent_fail(self, message: AgentFail) -> None:
        failed_agent = message.agent
        replacement = await self.session.restart_gemini_once(
            failed_agent,
            self,
            idle=self.turn != "agent",
        )
        if replacement is not None and failed_agent is not None:
            self._ready_agents.discard(id(failed_agent))
            self._agent_modes.pop(id(failed_agent), None)
            if self._working_agent is failed_agent:
                self._working_agent = None
                self._agent_started_at = None
            if self._active_relay_agent is failed_agent:
                self._active_relay_agent = replacement
            if self._mode_agent is failed_agent:
                self._mode_agent = replacement
            self.agent = self.session.primary_agent
            self._refresh_roster_info()
            self._refresh_displayed_modes()
            self.flash(
                f"COMMS LOST // {failed_agent.get_info()} · REACQUIRING LINK",
                style="warning",
            )
            return

        failed_index = self.session.mark_failed(message.agent)
        if message.agent is not None:
            self._ready_agents.discard(id(message.agent))
            self._finish_agent_status(message.agent)
        self.agent = self.session.primary_agent
        self._refresh_roster_info()
        self._refresh_displayed_modes()

        # A failed adapter must never receive a turn. If another configured
        # member is already connected, degrade to it (or to a smaller relay)
        # instead of leaving the whole conversation permanently disabled.
        active_agents = self.session.active_agents
        self.agent_ready = bool(active_agents) and all(
            id(agent) in self._ready_agents for agent in active_agents
        )
        self._move_relay_queue_to_solo_agent()
        self.update_slash_commands()
        if failed_index is not None:
            await self._persist_roster()
        self.notify(message.message, title="Agent failure", severity="error", timeout=5)

        if message.message:
            error = Content.assemble(
                Content(message.message).stylize("$text-error"),
                " — ",
                Content(message.details.strip()).stylize("dim"),
            )
        else:
            error = Content(message.details.strip()).stylize("$text-error")
        await self.post(Note(error, classes="-error"))

        from codeswarm.widgets.markdown_note import MarkdownNote

        if message.help in AGENT_FAIL_HELP:
            help = AGENT_FAIL_HELP[message.help]
        else:
            help = AGENT_FAIL_HELP["fail"]

        await self.post(MarkdownNote(help, classes="-error"))

    @on(messages.WorkStarted)
    def on_work_started(self) -> None:
        self.busy_count += 1

    @on(messages.WorkFinished)
    def on_work_finished(self) -> None:
        self.busy_count -= 1

    @work
    @on(messages.ChangeMode)
    async def on_change_mode(self, event: messages.ChangeMode) -> None:
        if event.mode_id == DISCUSSION_MODE.id:
            self._set_discussion_mode(
                "off" if self.discussion_mode else "discuss"
            )
            return
        if self.discussion_mode:
            self._set_discussion_mode("off")
        await self.set_mode(event.mode_id)

    @on(messages.ChangeCollaborationMode)
    def on_change_collaboration_mode(
        self, event: messages.ChangeCollaborationMode
    ) -> None:
        self._set_collaboration_mode(event.mode)

    @on(acp_messages.ModeUpdate)
    async def on_mode_update(self, event: acp_messages.ModeUpdate) -> None:
        agent = event.agent or self._mode_agent
        if agent is not None:
            modes, _current_mode = self._agent_modes.get(id(agent), ({}, None))
            modes, current_mode = self._effective_agent_mode_state(
                agent, modes, event.current_mode
            )
            self._agent_modes[id(agent)] = (modes, current_mode)
            self._refresh_displayed_modes()
            await self._sync_desired_mode()
            return
        # An adapter may report an unknown mode during a mode-list refresh.
        # Clear the old selection rather than displaying a mode it never owned.
        if not self.discussion_mode:
            self.current_mode = self.modes.get(event.current_mode)

    @on(messages.UserInputSubmitted)
    async def on_user_input_submitted(self, event: messages.UserInputSubmitted) -> None:
        if not event.body.strip():
            return
        if text := event.body.strip():
            await self.prompt_history.append(event.body)
            self.prompt_history_index = 0
            if text.startswith("!"):
                command_text = text[1:].strip()
                if not command_text:
                    self.flash("Type a command after !", style="error")
                    return
                await self.post(UserInput(text))
                self.window.scroll_end(animate=False)
                self.run_local_shell(command_text)
                return
            if text.startswith("/") and await self.slash_command(text):
                # CodeSwarm has processed the slash command.
                return
            if not self.agent_ready:
                self.app.bell()
                self.flash(
                    "Agent is not ready. Please wait while the agent connects…",
                    style="error",
                )
                return
            direct_target: tuple[int, str] | None = None
            if (
                direct_target is None
                and text.startswith("/")
                and self._relay_active
                and self._mode_agent is not None
                and any(
                    slash.command == text.partition(" ")[0]
                    for slash in self.agent_slash_commands
                )
            ):
                agent_index = self.session.index_of_agent(self._mode_agent)
                if agent_index is not None:
                    direct_target = (agent_index, text)
            if direct_target is not None and self._relay_active:
                agent_index, direct_prompt = direct_target
                if self.relay_paused:
                    self._queue_relay_prompt(
                        self.session.enqueue_direct(agent_index, direct_prompt),
                        "TX HOLD // FORMATION PAUSED",
                        direct_prompt,
                        direct=True,
                    )
                    return
                if self.turn == "agent":
                    self._queue_relay_prompt(
                        self.session.enqueue_direct(agent_index, direct_prompt),
                        "TX HOLD // DIRECT CHANNEL",
                        direct_prompt,
                        direct=True,
                    )
                    return
                await self.post(UserInput(direct_prompt))
                self.window.scroll_end(animate=False)
                self.turn = "agent"
                self.send_direct_prompt_to_agent(agent_index, direct_prompt)
                return
            if self._relay_active and self.turn == "agent":
                active_agent = self._routing_agent()
                active_name = (
                    self._agent_display_name(active_agent)
                    if active_agent is not None
                    else "the active agent"
                )
                self._queue_relay_prompt(
                    self.session.enqueue_human(text),
                    f"TX HOLD // {active_name}",
                    text,
                )
                return
            if self._relay_active and self.relay_paused:
                self._queue_relay_prompt(
                    self.session.enqueue_human(text),
                    "TX HOLD // FORMATION PAUSED",
                    text,
                )
                return
            if self._queue_solo_prompt_if_busy(text):
                return
            await self.post(UserInput(text))
            self.window.scroll_end(animate=False)
            self.turn = "agent"
            self.send_prompt_to_agent(text)

    def _queue_solo_prompt_if_busy(self, prompt: str) -> bool:
        """Queue a follow-up instead of overlapping requests to one ACP agent."""
        if self._relay_active or self.turn != "agent":
            return False
        if len(self._pending_solo_prompts) >= MAX_QUEUED_PROMPTS:
            self.flash(
                f"Queue is full ({MAX_QUEUED_PROMPTS} messages); wait for the agent",
                style="error",
            )
            return True
        self._pending_solo_prompts.append(prompt)
        self._add_queued_prompt(prompt, False, "TX HOLD // ACTIVE WINGMAN")
        return True

    def _queue_relay_prompt(
        self, accepted: bool, success_message: str, prompt: str, *, direct: bool = False
    ) -> None:
        """Show accepted relay prompts above the composer until dispatch."""
        if accepted:
            self._add_queued_prompt(prompt, direct, success_message)
            return
        self.flash(
            f"Queue is full ({MAX_QUEUED_PROMPTS} messages); wait for an agent",
            style="error",
        )

    def _add_queued_prompt(self, prompt: str, direct: bool, label: str) -> None:
        self._queued_prompt_previews.append((prompt, direct, label))
        self._refresh_queued_prompt_previews()

    def _refresh_queued_prompt_previews(self) -> None:
        self.queued_messages = tuple(
            f"{label} · {prompt}"
            for prompt, _direct, label in self._queued_prompt_previews
        )

    def _discard_queued_prompt(self, prompt: str, direct: bool) -> None:
        """Remove one accepted prompt from the composer holding area."""
        previews = list(self._queued_prompt_previews)
        for index, (queued_prompt, queued_direct, _label) in enumerate(previews):
            if queued_prompt == prompt and queued_direct == direct:
                previews.pop(index)
                self._queued_prompt_previews = deque(previews)
                break
        self._refresh_queued_prompt_previews()

    @on(QueuedMessages.CancelRequested)
    def on_queued_message_cancel(
        self, event: QueuedMessages.CancelRequested
    ) -> None:
        """Cancel the queued item represented by a visible preview row."""
        event.stop()
        previews = list(self._queued_prompt_previews)
        if not 0 <= event.index < len(previews):
            return
        prompt, direct, _label = previews[event.index]
        if self._relay_active:
            occurrence = sum(
                queued_prompt == prompt and queued_direct == direct
                for queued_prompt, queued_direct, _ in previews[: event.index]
            )
            if not self.session.cancel_queued_prompt(
                prompt, direct, occurrence=occurrence
            ):
                return
        else:
            pending = list(self._pending_solo_prompts)
            try:
                pending.remove(prompt)
            except ValueError:
                return
            self._pending_solo_prompts.clear()
            self._pending_solo_prompts.extend(pending)
        previews.pop(event.index)
        self._queued_prompt_previews = deque(previews)
        self._refresh_queued_prompt_previews()

    async def _label_queued_relay_turn_start(
        self, _round_number: int, _agent: AgentBase, prompt: str, direct: bool
    ) -> None:
        self._discard_queued_prompt(prompt, direct)
        await self.post(UserInput(prompt))
        self.window.scroll_end(animate=False)

    @property
    def has_interruptible_work(self) -> bool:
        """Whether Ctrl+C should cancel work instead of quitting CodeSwarm."""
        return self.turn == "agent" or bool(self._local_shells)

    async def cancel_active_work(self) -> bool:
        """Cancel agent work and direct shell commands owned by this conversation."""
        cancelled = False
        if self.turn == "agent":
            try:
                cancelled = await self.session.cancel_active() or cancelled
            except Exception:
                # A broken adapter must not prevent Ctrl+C from stopping an
                # unrelated local command in the same conversation.
                pass
            workers = self.app.workers.cancel_group(self, AGENT_TURN_WORKER_GROUP)
            for worker in workers:
                with suppress(WorkerCancelled, WorkerError):
                    await worker.wait()
            if workers:
                cancelled = True
                if self._working_agent is not None:
                    self._finish_agent_status(self._working_agent)
                self._mark_collaboration_complete()
                await self.agent_turn_over("end_turn")
                if self._relay_active and self.session.queued_prompt_count:
                    self.turn = "agent"
                    self.send_prompt_to_agent("", resume_queued=True)
        for terminal in tuple(self._local_shells):
            cancelled = terminal.kill() or cancelled
        return cancelled

    @work
    async def run_local_shell(self, command_text: str) -> None:
        """Run an explicit ``!`` command locally without involving any agent."""
        from codeswarm.widgets.terminal_tool import Command, TerminalTool

        width = self.window.size.width - 5 - self.window.styles.scrollbar_size_vertical
        height = max(1, self.window.scrollable_content_region.height - 2)
        terminal = TerminalTool(
            Command(command_text, [], {}, self.working_directory),
            minimum_terminal_width=width,
            classes="-local-shell",
        )
        self._local_shells.add(terminal)
        self.busy_count += 1
        try:
            await self.post(terminal)
            await terminal.start(width, height)
            await terminal.wait_for_exit()
        except Exception as error:
            if terminal.is_attached:
                await terminal.remove()
            await self.post(
                Note(
                    Content.assemble(
                        ("Unable to run shell command", "$text-error"),
                        " — ",
                        (str(error).strip() or "no details were provided", "dim"),
                    ),
                    classes="-error",
                )
            )
        finally:
            self.busy_count -= 1
            self._local_shells.discard(terminal)
            if self.turn != "agent":
                self.app.clear_interrupt_request()

    async def _dispatch_next_solo_prompt(self) -> bool:
        """Start the next queued solo prompt after the current turn ends."""
        if not self._pending_solo_prompts or self.agent is None:
            return False
        prompt = self._pending_solo_prompts.popleft()
        self._discard_queued_prompt(prompt, False)
        await self.post(UserInput(prompt))
        self.window.scroll_end(animate=False)
        self.turn = "agent"
        self.send_prompt_to_agent(prompt)
        return True

    def _move_relay_queue_to_solo_agent(self) -> None:
        """Keep queued follow-ups useful after a relay loses all but one peer."""
        queued_prompts = self.session.drain_relay_prompts_for_solo_agent()
        if queued_prompts:
            self._pending_solo_prompts.extend(queued_prompts)
            self._queued_prompt_previews = deque(
                (prompt, False, label)
                for prompt, _direct, label in self._queued_prompt_previews
            )
            self._refresh_queued_prompt_previews()

    def _agent_display_name(self, agent: AgentBase) -> str:
        return self.session.display_name(agent)

    @staticmethod
    def _roster_tone_style(index: int, *, bold: bool) -> str:
        """Style one roster name with its own identity hue.

        The index basis matches ``AgentMessage``: both count positions in
        ``session.active_agents`` and wrap at four, so a footer name and its
        reply always agree.
        """
        tone = f"$agent-name-{index % 4}"
        return f"{tone} bold" if bold else tone

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        minutes, seconds = divmod(max(0, seconds), 60)
        return f"{minutes}:{seconds:02d}"

    def _begin_agent_status(self, agent: AgentBase) -> None:
        """Start the compact roster timer for one sequential agent turn."""
        if self._agent_status_timer is not None:
            self._agent_status_timer.stop()
        # A relay can return to the same agent for several rounds. The
        # collaboration reset owns clearing this value; each turn only starts
        # a new interval that is added when the turn finishes.
        self._agent_elapsed.setdefault(id(agent), 0)
        self._working_agent = agent
        self._agent_started_at = monotonic()
        self._agent_status_timer = self.set_interval(1, self._refresh_roster_info)
        self._refresh_roster_info()

    def _finish_agent_status(self, agent: AgentBase) -> None:
        """Freeze one agent's elapsed time in the roster."""
        if self._working_agent is not agent or self._agent_started_at is None:
            return
        elapsed = int(monotonic() - self._agent_started_at)
        self._agent_elapsed[id(agent)] = self._agent_elapsed.get(id(agent), 0) + elapsed
        if self._agent_message is not None and self._response_agent is agent:
            self._agent_message.finalize(elapsed)
        if self._agent_status_timer is not None:
            self._agent_status_timer.stop()
        self._agent_status_timer = None
        self._working_agent = None
        self._agent_started_at = None
        self._refresh_roster_info()

    def _begin_collaboration(self) -> None:
        """Reset compact status for a newly submitted unit of work."""
        self._collaboration_complete = False
        self._agent_elapsed.clear()

    def _mark_collaboration_complete(self) -> None:
        """Return the roster to its idle state after a collaboration finishes."""
        self._collaboration_complete = True
        self._active_relay_agent = None
        self._refresh_roster_info()

    async def _post_collaboration_summary(self) -> None:
        """Add the per-agent elapsed time label for a completed relay batch."""
        if not self.session.active_agents:
            return
        # ACP adapters post their final streamed update to Textual before
        # returning the relay result, but that message may still be waiting in
        # Textual's queue. Let one refresh cycle drain it before appending the
        # batch footer, otherwise the footer can appear above the final reply.
        rendered = asyncio.Event()
        self.call_after_refresh(rendered.set)
        await rendered.wait()
        elapsed_by_agent = [
            (
                self._agent_display_name(agent),
                self._format_elapsed(self._agent_elapsed.get(id(agent), 0)),
            )
            for agent in self.session.active_agents
        ]
        # Each agent's time carries that agent's hue, matching its message
        # header and its roster entry. The label itself is chrome, and the
        # separators use an explicit tone rather than `dim`, which emits SGR 2
        # and is applied inconsistently across terminals.
        parts: list[Content | tuple[str, str]] = [
            ("Batch complete", "$chrome-text-strong bold")
        ]
        for index, (name, elapsed) in enumerate(elapsed_by_agent):
            parts.extend(
                [
                    (" · ", "$message-meta"),
                    (
                        f"{name} {elapsed}",
                        self._roster_tone_style(index, bold=False),
                    ),
                ]
            )
        await self.post(Note(Content.assemble(*parts), classes="-batch-summary"))

    def _routing_agent(self) -> AgentBase | None:
        """Return the agent that would receive the next user message."""
        active_agents = self.session.active_agents
        relay = self.session.relay
        if relay is None:
            return active_agents[0] if len(active_agents) == 1 else None
        if self.session.collaboration_mode == "pair":
            if self.session.roster and self.session.roster[0].agent in active_agents:
                return self.session.roster[0].agent
            return None
        if self.session.collaboration_mode == "manual":
            index = getattr(relay, "pinned_agent_index", None)
            if isinstance(index, int) and 0 <= index < len(self.session.roster):
                entry = self.session.roster[index]
                if entry.active and entry.agent in active_agents:
                    return entry.agent
            return None
        index = self.session.selected_agent_index
        if not isinstance(index, int):
            if self._working_agent in active_agents:
                return self._working_agent
            if self._active_relay_agent in active_agents:
                return self._active_relay_agent
            index = relay.next_agent_index
        if not isinstance(index, int):
            index = self.session.first_agent
        if not isinstance(index, int):
            return active_agents[0] if active_agents else None
        if 0 <= index < len(self.session.roster):
            entry = self.session.roster[index]
            if entry.active and entry.agent in active_agents:
                return entry.agent
        return active_agents[0] if active_agents else None

    def select_routing_agent_at(self, offset: int) -> None:
        """Select the roster entry whose visible name was clicked."""
        roster_text = self.agent_info.plain
        search_start = 0
        for agent in self.session.active_agents:
            name = self._agent_display_name(agent)
            name_start = roster_text.find(name, search_start)
            if name_start < 0:
                continue
            # The marker and optional routing arrow immediately precede a
            # name. Accept clicks on either so the compact target is forgiving.
            if name_start - 4 <= offset < name_start + len(name):
                index = self.session.index_of_agent(agent)
                if index is not None:
                    try:
                        if self.session.collaboration_mode == "manual":
                            self.session.select_pinned_agent(index)
                        elif self.session.collaboration_mode == "pair":
                            return
                        else:
                            self.session.select_agent(index)
                    except (IndexError, ValueError):
                        self.flash(
                            "Pinned agent is unavailable; select an active agent",
                            style="warning",
                        )
                        return
                    self._refresh_roster_info()
                    self.prompt.prompt_text_area.focus()
                return
            search_start = name_start + len(name)

    def _refresh_roster_info(self) -> None:
        """Show the complete roster and current speaker in the prompt footer.

        A roster name carries the same hue as that agent's message header and
        card rail, so the eye can match a name here to a reply above without
        reading either. Every entry used to be teal, which meant the footer
        could not tell two agents apart at all; work state is carried by the
        marker and by weight instead.
        """
        agents = self.session.active_agents
        routing_agent = self._routing_agent()
        discussion_indicator: tuple[str, str] = (" · Chat", "$chrome-text")
        if len(agents) <= 1:
            if agents:
                agent = agents[0]
                is_working = agent is self._working_agent
                elapsed = self._agent_elapsed.get(id(agent), 0)
                if is_working and self._agent_started_at is not None:
                    elapsed += int(monotonic() - self._agent_started_at)
                prefix = "→ " if agent is routing_agent else ""
                tone = self._roster_tone_style(0, bold=is_working)
                if is_working:
                    agent_info = Content.styled(
                        f"{prefix}● {self._agent_display_name(agent)} · "
                        f"{self._format_elapsed(elapsed)}",
                        tone,
                    )
                else:
                    agent_info = Content.styled(
                        f"{prefix}○ {self._agent_display_name(agent)}",
                        tone,
                    )
            else:
                # Not an agent, so it gets no identity hue.
                agent_info = Content.styled("shell", "$chrome-text")
            self.agent_info = Content.assemble(
                agent_info,
                discussion_indicator if self.discussion_mode else "",
            )
            return

        roster: list[Content | tuple[str, str]] = []
        for index, agent in enumerate(agents):
            if index:
                # Explicit tone rather than `dim`, which emits SGR 2 and is
                # rendered inconsistently across terminals.
                roster.append((" · ", "$message-meta"))
            is_current = (
                agent is self._working_agent or agent is self._active_relay_agent
            )
            is_ready = id(agent) in self._ready_agents
            elapsed = self._agent_elapsed.get(id(agent), 0)
            is_timed = agent is self._working_agent and self._agent_started_at is not None
            if is_timed and self._agent_started_at is not None:
                elapsed += int(monotonic() - self._agent_started_at)
            if is_current:
                marker = "●"
            elif self.session.collaboration_mode == "manual" and agent is routing_agent:
                marker = "⌖"
            else:
                marker = "○" if is_ready else "…"
            prefix = (
                ""
                if self.session.collaboration_mode == "manual"
                else "→ " if agent is routing_agent else ""
            )
            timer = (
                f" · {self._format_elapsed(elapsed)}"
                if is_timed
                else ""
            )
            roster.append(
                Content.styled(
                    f"{prefix}{marker} {self._agent_display_name(agent)}{timer}",
                    self._roster_tone_style(index, bold=is_current),
                )
            )
        if self.discussion_mode:
            roster.append(discussion_indicator)
        self.agent_info = Content.assemble(*roster)

    def _set_collaboration_mode(self, value: str) -> None:
        """Select the routing strategy without changing ACP permission modes."""
        mode = value.strip().lower()
        if mode not in {"roster", "manual", "pair"}:
            self.flash("Use /collab roster, /collab manual, or /collab pair", style="error")
            return
        if mode == self.session.collaboration_mode:
            return
        if self.turn == "agent":
            self._pending_collaboration_mode = mode
            self.flash(
                f"COLLAB // {mode.upper()} queued until the active turn lands",
                style="warning",
            )
            return
        self._apply_collaboration_mode(mode)

    def _apply_collaboration_mode(self, mode: str) -> None:
        try:
            self.session.set_collaboration_mode(mode)  # type: ignore[arg-type]
        except (IndexError, ValueError) as error:
            self.flash(f"Unable to switch collaboration mode: {error}", style="error")
            return
        self.collaboration_mode = mode.title()
        self._refresh_roster_info()
        self.update_slash_commands()
        self.flash(f"COLLAB // {mode.upper()} active", style="success")

    def _set_discussion_mode(self, value: str) -> None:
        """Switch the CodeSwarm-owned conversation policy for every agent."""
        mode = value.strip().lower()
        if mode in {"chat", "discuss", "discussion"}:
            self.discussion_mode = True
            self.session.set_turn_instructions(DISCUSSION_INSTRUCTIONS)
            self._refresh_displayed_modes()
            self._refresh_roster_info()
            self.flash(
                "Chat enabled — agents are instructed not to inspect code",
                style="success",
            )
        elif mode == "off":
            self.discussion_mode = False
            self.session.set_turn_instructions("")
            self._refresh_displayed_modes()
            self._refresh_roster_info()
            self.flash("Chat ended — agent mode restored", style="success")
        else:
            self.flash("Use /mode to choose a mode", style="error")

    def _prepare_solo_prompt(self, prompt: str) -> str:
        if not self.discussion_mode:
            return prompt
        return f"{DISCUSSION_INSTRUCTIONS}\n\nUser question:\n{prompt}"

    @work(group=AGENT_TURN_WORKER_GROUP)
    async def send_direct_prompt_to_agent(self, agent_index: int, prompt: str) -> None:
        if not self._relay_active:
            return
        self._begin_collaboration()
        self.busy_count += 1
        self.turn = "agent"
        stop_reason: str | None = None
        agent: AgentBase | None = None
        try:
            agent = self.session.agent_at(agent_index)
            await self._label_relay_turn_start(0, agent)
            stop_reason = await self.session.send_direct_prompt(agent_index, prompt)
        except Exception as error:
            await self._post_agent_communication_error(error)
        finally:
            if agent is not None:
                self._finish_agent_status(agent)
                self._mark_collaboration_complete()
            self.busy_count -= 1
            self._active_relay_agent = None
            self._refresh_roster_info()
        self.call_later(self.agent_turn_over, stop_reason)

    @work(group=AGENT_TURN_WORKER_GROUP)
    async def send_prompt_to_agent(
        self, prompt: str, *, resume_queued: bool = False
    ) -> None:
        if self._relay_active:
            self._begin_collaboration()
            self.busy_count += 1
            try:
                self.turn = "agent"
                if resume_queued:
                    result = await self.session.send_prompt(
                        prompt, resume_queued=True
                    )
                else:
                    result = await self.session.send_prompt(prompt)
                assert isinstance(result, RelayResult)
                if result.reason == "max_rounds":
                    from codeswarm.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "The relay stopped after reaching its "
                            f"{result.rounds}-round safety limit.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "paused":
                    from codeswarm.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "All agents are paused. Queued work will resume when "
                            "you resume the relay.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "roster_collapsed":
                    from codeswarm.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "The relay stopped because fewer than two agents "
                            "are left in the roster.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "stop_token":
                    self._mark_collaboration_complete()
                    await self._post_collaboration_summary()
            except Exception as error:
                await self._post_agent_communication_error(error)
            finally:
                self.busy_count -= 1
                self._active_relay_agent = None
                self._refresh_roster_info()
            self.call_later(self.agent_turn_over, "end_turn")
        elif self.agent is not None:
            self._begin_collaboration()
            active_agent = self.agent
            self._begin_agent_status(active_agent)
            stop_reason: str | None = None
            self.busy_count += 1
            try:
                self.turn = "agent"
                stop_reason = await self.session.send_prompt(self._prepare_solo_prompt(prompt))
            except jsonrpc.APIError as error:
                self.turn = "client"
                await self._post_agent_communication_error(error)
            except Exception as error:
                await self._post_agent_communication_error(error)
            finally:
                self._finish_agent_status(active_agent)
                self._mark_collaboration_complete()
                self.busy_count -= 1
                self._active_relay_agent = None
                self._refresh_roster_info()
            self.call_later(self.agent_turn_over, stop_reason)

    async def _post_agent_communication_error(self, error: Exception) -> None:
        """Surface an adapter failure while still returning the prompt to idle."""
        message = str(error).strip() or "no details were provided"
        if len(message) > MAX_AGENT_ERROR_DISPLAY_CHARS:
            message = (
                message[:MAX_AGENT_ERROR_DISPLAY_CHARS]
                + "\n[CodeSwarm truncated the remaining error details.]"
            )
        await self.post(
            Note(
                Content.assemble(
                    ("Agent request failed", "$text-error"),
                    " — ",
                    (message, "dim"),
                    "\n",
                    ("Try the prompt again.", "dim"),
                ),
                classes="-error",
            )
        )

    async def agent_turn_over(self, stop_reason: str | None) -> None:
        """Called when the agent's turn is over.

        Args:
            stop_reason: The stop reason returned from the Agent, or `None`.
        """
        self.turn = "client"
        self.app.clear_interrupt_request()
        if self._agent_thought is not None and self._agent_thought.loading:
            await self._agent_thought.remove()
        self._agent_response = None
        self._agent_thought = None
        self._response_agent = None
        self._agent_message = None

        self.prompt.project_directory_updated()

        if self._pending_collaboration_mode is not None:
            pending_mode = self._pending_collaboration_mode
            self._pending_collaboration_mode = None
            self._apply_collaboration_mode(pending_mode)

        await self._sync_desired_mode()

        if await self._dispatch_next_solo_prompt():
            return

        if stop_reason != "end_turn":
            from codeswarm.widgets.markdown_note import MarkdownNote

            agent = (self.agent_title or "agent").title()

            if stop_reason == "max_tokens":
                await self.post(
                    MarkdownNote(
                        STOP_REASON_MAX_TOKENS.replace("$AGENT", agent),
                        classes="-stop-reason",
                    )
                )
            elif stop_reason == "max_turn_requests":
                await self.post(
                    MarkdownNote(
                        STOP_REASON_MAX_TURN_REQUESTS.replace("$AGENT", agent),
                        classes="-stop-reason",
                    )
                )
            elif stop_reason == "refusal":
                await self.post(
                    MarkdownNote(
                        STOP_REASON_REFUSAL.replace("$AGENT", agent),
                        classes="-stop-reason",
                    )
                )

        if self.app.settings.get("notifications.turn_over", bool):
            agent_title = self.agent_title or "Agent"
            self.app.system_notify(
                f"MISSION COMPLETE // {agent_title}",
                title="AWAITING ORDERS",
            )

    def action_focus_block(self, block_id: str) -> None:
        with suppress(NoMatches):
            self.query_one(f"#{block_id}").focus()

    @on(messages.HistoryMove)
    async def on_history_move(self, message: messages.HistoryMove) -> None:
        message.stop()
        await self.prompt_history.open()
        self.prompt_history_index += message.direction

    def _build_slash_commands(self) -> list[SlashCommand]:
        slash_commands = [
            SlashCommand("/help", "Show CodeSwarm commands"),
            SlashCommand("/config", "Configure CodeSwarm preferences"),
            SlashCommand("/export", "Export the conversation as Markdown"),
            SlashCommand("/mode", "Open the mode picker"),
            SlashCommand(
                "/collab",
                "Choose Roster, Manual, or Pair routing",
                "roster | manual | pair",
            ),
            SlashCommand(
                "/close",
                "Close the current session",
            ),
        ]
        if self._relay_active:
            slash_commands.append(
                SlashCommand("/pause", "Pause or resume all agents")
            )

        # CodeSwarm handles its own commands locally, so they must retain their
        # description and completion entry even when an ACP agent advertises a
        # command with the same name.
        slash_commands.extend(self.agent_slash_commands)
        deduplicated_slash_commands: dict[str, SlashCommand] = {}
        for slash_command in slash_commands:
            deduplicated_slash_commands.setdefault(slash_command.command, slash_command)
        slash_commands = sorted(
            deduplicated_slash_commands.values(), key=attrgetter("command")
        )
        return slash_commands

    def update_slash_commands(self) -> None:
        """Update slash commands, which may have changed since mounting."""
        self.prompt.slash_commands = self._build_slash_commands()

    async def on_mount(self) -> None:
        self.app.register_conversation(self)
        self.trap_focus()
        self.prompt.focus()
        self.prompt.slash_commands = self._build_slash_commands()
        if self.session.owner_data is not None:
            self.call_after_refresh(self._start_agents)

        else:
            self.agent_ready = True

        self.update_title()
        self.window.anchor()

    async def _start_agents(self) -> None:
        """Start the configured roster and report a setup failure in context."""
        try:
            await self.session.start(
                self,
                on_turn_start=self._label_relay_turn_start,
                on_queued_turn_start=self._label_queued_relay_turn_start,
                on_queued_turn_discarded=self._discard_queued_prompt,
                on_turn=self._label_relay_turn,
            )
        except Exception as error:
            details = str(error).strip() or "no details were provided"
            self.agent_ready = False
            self.notify(
                "Unable to start the selected agent roster",
                title="Agent startup",
                severity="error",
            )
            await self.post(
                Note(
                    Content.assemble(
                        ("Unable to start the selected agent roster", "$text-error"),
                        " — ",
                        (details, "dim"),
                    ),
                    classes="-error",
                )
            )
            return

        self.agent = self.session.owner
        self._refresh_roster_info()
        self._refresh_displayed_modes()
        await self._sync_desired_mode()
        self.update_slash_commands()
        await self._persist_roster()

    async def reconcile_roster(
        self,
        requested_identities: Sequence[str],
        catalog: dict[str, AgentData],
    ) -> list[str]:
        """Apply an idle config selection without disturbing stable live indices."""
        desired = list(dict.fromkeys(requested_identities))
        if not self.session.roster:
            return []
        owner_identity = self.session.roster[0].data["identity"]
        if owner_identity not in desired:
            desired.insert(0, owner_identity)

        active_identities = {
            entry.data["identity"]
            for entry in self.session.roster
            if entry.active
        }
        failures: list[str] = []

        # Establish every requested replacement before removing a healthy peer.
        for identity in desired:
            if identity in active_identities:
                continue
            data = catalog.get(identity)
            if data is None:
                failures.append(identity)
                continue
            try:
                await self.session.add(
                    data,
                    self,
                    on_turn_start=self._label_relay_turn_start,
                    on_queued_turn_start=self._label_queued_relay_turn_start,
                    on_queued_turn_discarded=self._discard_queued_prompt,
                    on_turn=self._label_relay_turn,
                )
            except Exception as error:
                failures.append(data["name"])
                details = str(error).strip() or "no details were provided"
                self.flash(
                    f"Unable to add {data['name']} — {details}",
                    style="error",
                )
            else:
                active_identities.add(identity)

        # If an addition failed, keep all healthy peers as a safe fallback.
        if not failures:
            for index, entry in enumerate(self.session.roster[1:], start=1):
                identity = entry.data["identity"]
                if not entry.active or identity in desired:
                    continue
                dropped_agent = entry.agent
                try:
                    await self.session.drop(index)
                except Exception as error:
                    if entry.active:
                        failures.append(entry.data["name"])
                        details = str(error).strip() or "no details were provided"
                        self.flash(
                            f"Unable to remove {entry.data['name']} — {details}",
                            style="error",
                        )
                        continue
                if dropped_agent is not None:
                    self._ready_agents.discard(id(dropped_agent))
                    self._agent_modes.pop(id(dropped_agent), None)
                    self._agent_elapsed.pop(id(dropped_agent), None)
                    self._agent_slash_commands.pop(id(dropped_agent), None)
                    if self._working_agent is dropped_agent:
                        self._working_agent = None
                        self._agent_started_at = None
                    if self._active_relay_agent is dropped_agent:
                        self._active_relay_agent = None
                    if self._mode_agent is dropped_agent:
                        self._mode_agent = None

        self.agent = self.session.primary_agent
        active_agents = self.session.active_agents
        self.agent_ready = bool(active_agents) and all(
            id(agent) in self._ready_agents for agent in active_agents
        )
        self._move_relay_queue_to_solo_agent()
        self._refresh_roster_info()
        self._refresh_displayed_modes()
        self.update_slash_commands()

        actual = [
            entry.data["identity"]
            for entry in self.session.roster
            if entry.active
        ]
        launcher_order = [identity for identity in desired if identity in actual]
        launcher_order.extend(
            identity for identity in actual if identity not in launcher_order
        )
        await self._persist_roster(launcher_order)
        return failures

    async def _persist_roster(
        self, launcher_identities: Sequence[str] | None = None
    ) -> None:
        """Best-effort record of the roster in the session's meta JSON.

        Not read back in this version: only the session owner (index 0) has
        an ``agent_session_id`` to resume against, so restoring peers is
        deferred. Writing the roster now keeps a future resume-aware reader
        forward-compatible without a schema change.

        Also updates the ``launcher.roster`` setting, which is what a bare
        ``codeswarm`` invocation restores on the next launch.
        """
        await self.session.persist_roster(
            self.app.settings,
            self.app.save_settings,
            launcher_identities,
        )

    async def _label_relay_turn(
        self, round_number: int, agent: AgentBase, response: str
    ) -> None:
        """Freeze the active agent's compact roster timer."""
        if (
            response == DEFAULT_STOP_ACKNOWLEDGMENT
            and (getattr(agent, "last_response", "") or "").strip() == STOP_TOKEN
        ):
            # ACP streaming hides control tokens before they reach Textual. A
            # token-only reviewer response therefore needs a small visible
            # acknowledgment so the transcript does not appear to skip them.
            self.begin_agent_output(agent)
            await self.post_agent_response(DEFAULT_STOP_ACKNOWLEDGMENT)
        self._finish_agent_status(agent)

    async def _label_relay_turn_start(
        self, round_number: int, agent: AgentBase
    ) -> None:
        """Mark the current speaker without adding a transcript-sized header."""
        self._active_relay_agent = agent
        self._select_agent_modes(agent)
        self._begin_agent_status(agent)

    def watch_agent(self, agent: AgentBase | None) -> None:
        self._refresh_roster_info()
        expected_agents = sum(1 for entry in self.session.roster if entry.active)
        if agent is not None and len(self._ready_agents) < expected_agents:
            self.agent_ready = False
        self.update_title()

    @work
    async def watch_agent_ready(self, ready: bool) -> None:
        if ready and (agent_data := self.session.owner_data) is not None:
            welcome = agent_data.get("welcome", None)
            if welcome is not None:
                from codeswarm.widgets.markdown_note import MarkdownNote

                await self.post(MarkdownNote(welcome))
        if ready and self._initial_prompt is not None:
            self.post_message(messages.UserInputSubmitted(self._initial_prompt))
            self._initial_prompt = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._mouse_down_offset = event.screen_offset

    def on_click(self, event: events.Click) -> None:
        if (
            self._mouse_down_offset is not None
            and event.screen_offset != self._mouse_down_offset
        ):
            return
        widget = event.widget

        contents = self.contents
        if self.screen.get_selected_text():
            return
        if widget is None or widget.is_maximized:
            return
        try:
            widget.query_ancestor(Prompt)
        except NoMatches:
            pass
        else:
            return

        nested_block_target: Widget | None = None
        if widget in contents.displayed_children:
            self.cursor_offset = contents.displayed_children.index(widget)
            self.refresh_block_cursor()
            return
        for parent in widget.ancestors:
            if not isinstance(parent, Widget):
                break
            if (
                parent is self or parent is contents
            ) and widget in contents.displayed_children:
                self.cursor_offset = contents.displayed_children.index(widget)
                self.refresh_block_cursor()
                break
            if (
                isinstance(parent, BlockProtocol)
                and parent in contents.displayed_children
            ):
                self.cursor_offset = contents.displayed_children.index(parent)
                parent.block_select(nested_block_target or widget)
                self.refresh_block_cursor()
                break
            if isinstance(parent, BlockProtocol) and nested_block_target is None:
                # A displayed block may wrap another selectable block (for
                # example AgentMessage -> AgentResponse -> MarkdownParagraph).
                # Keep the inner block that the click actually selected while
                # walking through the wrapper.
                nested_block_target = widget
            widget = parent

    def new_block(self) -> None:
        """Start a new block for agent response."""
        self._agent_thought = None
        self._agent_response = None
        self._agent_message = None

    async def post[WidgetType: Widget](
        self,
        widget: WidgetType,
        *,
        loading: bool = False,
        new_block: bool = True,
    ) -> WidgetType:
        """Post a widget to the conversation.

        Args:
            widget: Widget to post.
            loading: Set the widget to an initial loading state?
            new_block: Start a new block?

        Returns:
            The widget that was mounted.
        """
        if new_block and not loading:
            self.new_block()
        if not self.contents.is_attached:
            return widget
        await self.contents.mount(widget)

        widget.loading = loading
        self._require_check_prune = True
        self.call_after_refresh(self.check_prune)
        return widget

    async def check_prune(self) -> None:
        """Check if a prune is required."""
        if self._require_check_prune:
            self._require_check_prune = False
            low_mark = self.app.settings.get("ui.prune_low_mark", int)
            high_mark = low_mark + self.app.settings.get("ui.prune_excess", int)
            await self.prune_window(low_mark, high_mark)

    async def prune_window(self, low_mark: int, high_mark: int) -> None:
        """Remove older children to keep within a certain range.

        Args:
            low_mark: Height to aim for.
            high_mark: Height to start pruning.
        """

        assert high_mark >= low_mark

        contents = self.contents

        height = contents.virtual_size.height
        if height <= high_mark:
            return
        prune_children: list[Widget] = []
        bottom_margin = 0
        prune_height = 0

        if low_mark == 0:
            prune_children = list(contents.children)
        else:
            for child in contents.children:
                if not child.display:
                    prune_children.append(child)
                    continue
                top, _, bottom, _ = child.styles.margin
                child_height = child.outer_size.height
                prune_height = (
                    (prune_height - bottom_margin + max(bottom_margin, top))
                    + bottom
                    + child_height
                )
                bottom_margin = bottom
                if height - prune_height <= low_mark:
                    break
                prune_children.append(child)

        self.cursor_offset = -1
        contents.refresh(layout=True)

        if prune_children:
            await contents.remove_children(prune_children)

        self.call_later(self.window.anchor)

    def action_cursor_up(self) -> None:
        if not self.contents.displayed_children or self.cursor_offset == 0:
            # No children
            return
        if self.cursor_offset == -1:
            # Start cursor at end
            self.cursor_offset = len(self.contents.displayed_children) - 1
            cursor_block = self.cursor_block
            if isinstance(cursor_block, BlockProtocol):
                cursor_block.block_cursor_clear()
                cursor_block.block_cursor_up()
        else:
            cursor_block = self.cursor_block
            if isinstance(cursor_block, BlockProtocol):
                if cursor_block.block_cursor_up() is None:
                    self.cursor_offset -= 1
                    cursor_block = self.cursor_block
                    if isinstance(cursor_block, BlockProtocol):
                        cursor_block.block_cursor_clear()
                        cursor_block.block_cursor_up()
            else:
                # Move cursor up
                self.cursor_offset -= 1
                cursor_block = self.cursor_block
                if isinstance(cursor_block, BlockProtocol):
                    cursor_block.block_cursor_clear()
                    cursor_block.block_cursor_up()
        self.refresh_block_cursor()

    def action_cursor_down(self) -> None:
        if not self.contents.displayed_children or self.cursor_offset == -1:
            # No children, or no cursor
            return

        cursor_block = self.cursor_block
        if isinstance(cursor_block, BlockProtocol):
            if cursor_block.block_cursor_down() is None:
                self.cursor_offset += 1
                if self.cursor_offset >= len(self.contents.displayed_children):
                    self.cursor_offset = -1
                    self.refresh_block_cursor()
                    return
                cursor_block = self.cursor_block
                if isinstance(cursor_block, BlockProtocol):
                    cursor_block.block_cursor_clear()
                    cursor_block.block_cursor_down()
        else:
            self.cursor_offset += 1
            if self.cursor_offset >= len(self.contents.displayed_children):
                self.cursor_offset = -1
                self.refresh_block_cursor()
                return
            cursor_block = self.cursor_block
            if isinstance(cursor_block, BlockProtocol):
                cursor_block.block_cursor_clear()
                cursor_block.block_cursor_down()
        self.refresh_block_cursor()

    async def _close_session(self) -> bool:
        """Cancel any running agents and close this session.

        Returns:
            `True` if the session was closed.
        """
        if self.turn == "agent" and self.agent is not None:
            await self.session.cancel_active()
        if self.screen.id is not None:
            self.post_message(messages.SessionClose(self.screen.id))
            return True
        return False

    @work
    async def action_close_session(self) -> None:
        await self._close_session()

    @work
    async def action_toggle_pause(self) -> None:
        """Pause or resume the complete relay."""
        if not self._relay_active:
            self.flash(
                "Pause is available when 2+ agents are running", style="warning"
            )
            return
        if self.relay_paused:
            self.relay_paused = False
            self.session.resume()
            # Mark the synthetic resume turn before scheduling its worker so
            # a prompt submitted immediately afterwards is queued rather than
            # racing a second relay run.
            self.turn = "agent"
            self.flash("FORMATION // FLIGHT RESUMED", style="success")
            self.send_prompt_to_agent(
                "Resume the collaboration from the current shared workspace."
            )
            return

        self.relay_paused = True
        self.session.pause()
        for agent in self.session.active_agents:
            await agent.cancel()
        self.flash("FORMATION // HOLDING PATTERN", style="warning")

    def focus_prompt(self, reset_cursor: bool = True, scroll_end: bool = True) -> None:
        """Focus the prompt input.

        Args:
            reset_cursor: Reset the block cursor.
            scroll_end: Scroll t the end of the content.
        """
        if reset_cursor:
            self.cursor_offset = -1
        if scroll_end:
            self.window.scroll_end()
        self.prompt.focus()

    def action_copy_to_clipboard(self) -> None:
        block = self.get_cursor_block()
        if isinstance(block, BlockContentProtocol):
            text = block.get_block_content("clipboard")
        elif isinstance(block, MarkdownFence):
            text = block._content.plain
        elif isinstance(block, MarkdownBlock):
            text = block.source
        else:
            return
        if text:
            self.app.copy_to_clipboard(text)
            self.flash("Copied to clipboard")

    def action_copy_to_prompt(self) -> None:
        block = self.get_cursor_block()
        if isinstance(block, BlockContentProtocol):
            text = block.get_block_content("prompt")
        elif isinstance(block, MarkdownFence):
            # Copy to prompt leaves MD formatting
            text = block.source
        elif isinstance(block, MarkdownBlock):
            text = block.source
        else:
            return

        if text:
            self.prompt.append(text)
            self.flash("Copied to prompt")
            self.focus_prompt()

    async def action_mode_switcher(self) -> None:
        self.prompt.mode_switcher.focus()

    def refresh_block_cursor(self) -> None:
        if (cursor_block := self.cursor_block_child) is not None:
            self.window.focus()
            self.call_after_refresh(
                self.window.scroll_to_center, cursor_block, immediate=True
            )
        else:
            self.window.anchor(False)
            self.window.scroll_end(duration=2 / 10)
            self.prompt.focus()
        self.refresh_bindings()

    async def slash_command(self, text: str) -> bool:
        """Give CodeSwarm the opportunity to process slash commands.

        Args:
            text: The prompt, including the slash in the first position.

        Returns:
            `True` if CodeSwarm has processed the slash command, `False` if it should
                be forwarded to the agent.
        """
        command, _, parameters = text[1:].partition(" ")
        if command == "help":
            from codeswarm.widgets.markdown_note import MarkdownNote

            await self.post(
                MarkdownNote(
                    """## CodeSwarm help

### Conversation

- `!<command>` — run a shell command directly in the current workspace
- `/mode` — choose one mode for every active agent
- `/mode chat` — chat without workspace inspection or tools
- `/collab roster` — sequential review relay around the active roster
- `/collab manual` — manually route each turn to the selected agent
- `/collab pair` — start each doer→verifier batch with the first agent
- `/pause` — pause or resume a multi-agent relay
- `/export` — export the conversation as Markdown
- `/close` — close this workspace and return to agent selection

### CodeSwarm

- `/config` — settings and the next workspace's agent roster

Drag over conversation text and press `Ctrl+C` to copy it. Otherwise,
`Ctrl+C` cancels active work; press it again within three seconds to quit.
"""
                )
            )
            return True
        if command == "config":
            from codeswarm.screens.config import ConfigScreen

            self.app.push_screen(ConfigScreen(self))
            return True
        if command == "export":
            try:
                export_path = self._export_conversation()
            except OSError as error:
                self.flash(f"Export failed: {error}", style="error")
            else:
                self.flash(f"Conversation exported to {export_path}", style="success")
            return True
        if command == "mode":
            if parameters.strip().lower() in {"chat", "discuss", "discussion"}:
                self._set_discussion_mode("chat")
            elif parameters.strip():
                self.flash("Use /mode to choose a mode", style="error")
            else:
                await self.action_mode_switcher()
            return True
        if command == "collab":
            if parameters.strip():
                self._set_collaboration_mode(parameters)
            else:
                self.flash(
                    "Use /collab roster, /collab manual, or /collab pair",
                    style="warning",
                )
            return True
        if command == "pause":
            self.action_toggle_pause()
            return True
        if command == "close":
            return await self._close_session()
        if any(
            slash.command.removeprefix("/") == command
            for slash in self.agent_slash_commands
        ):
            return False
        self.flash(
            f"Unknown command: /{command}. Type /help to see CodeSwarm commands.",
            style="error",
        )
        return True

    def _export_conversation(self) -> Path:
        """Write the retained user/agent conversation as a Markdown file."""
        from codeswarm.widgets.agent_response import AgentMessage, AgentResponse

        lines = ["# CodeSwarm Conversation", ""]
        for block in self.contents.displayed_children:
            if isinstance(block, UserInput):
                content = block.content.strip()
                if content:
                    lines.extend(["## User", "", content, ""])
            elif isinstance(block, AgentMessage):
                if block.response is None or not block.response.source.strip():
                    continue
                lines.extend(
                    [
                        f"## {block._speaker} · {block._timestamp}",
                        "",
                        block.response.source.strip(),
                        "",
                    ]
                )
            elif isinstance(block, AgentResponse) and block.source.strip():
                lines.extend(["## Agent", "", block.source.strip(), ""])

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        export_dir = Path(self.project_path)
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"codeswarm-conversation-{stamp}.md"
        suffix = 2
        while export_path.exists():
            export_path = export_dir / f"codeswarm-conversation-{stamp}-{suffix}.md"
            suffix += 1
        export_path.write_text("\n".join(lines).rstrip() + "\n")
        return export_path
