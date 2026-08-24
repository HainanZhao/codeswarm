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

from rich.segment import Segment

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
from textual.widgets import Static
from textual.widgets.markdown import MarkdownBlock, MarkdownFence
from textual.geometry import Offset, Spacing, Region
from textual.reactive import var
from textual.layouts.grid import GridLayout
from textual.layout import WidgetPlacement
from textual.strip import Strip
from textual.timer import Timer


import wingmen
from wingmen import jsonrpc, messages
from wingmen.db import DB
from wingmen import paths
from wingmen.agent_schema import Agent as AgentData
from wingmen.acp import messages as acp_messages
from wingmen.acp.agent import Mode
from wingmen.acp.relay import DEFAULT_STOP_ACKNOWLEDGMENT, STOP_TOKEN, RelayResult
from wingmen.app import WingmenApp
from wingmen.agent import AgentBase, AgentReady, AgentFail
from wingmen.session import SessionCoordinator
from wingmen.widgets.conversation_acp import ConversationACPHandlers
from wingmen.format_path import format_path
from wingmen.history import History
from wingmen.mode_policy import (
    DEFAULT_MODE_POLICY_ID,
    POLICIES_BY_ID,
    shared_current_mode,
    shared_modes,
)
from wingmen.widgets.flash import Flash
from wingmen.widgets.note import Note
from wingmen.widgets.prompt import Prompt
from wingmen.widgets.user_input import UserInput
from wingmen.widgets.agent_response import format_reply_timestamp
from wingmen.acp.relay import MAX_QUEUED_PROMPTS
from wingmen.slash_command import SlashCommand
from wingmen.protocol import BlockProtocol, BlockContentProtocol, ExpandProtocol

if TYPE_CHECKING:
    from wingmen.widgets.agent_response import AgentMessage, AgentResponse
    from wingmen.widgets.agent_thought import AgentThought
    from wingmen.widgets.terminal_tool import TerminalTool


AGENT_FAIL_HELP = {
    "fail": """\
## Agent failed to run

**The agent failed to start.**

Check that the agent is installed and up-to-date.

Some agents require an ACP adapter. Install or update the agent according to
its upstream documentation, then restart Wingmen.

If that fails, ask for help in [Discussions](https://github.com/batrachianai/wingmen/discussions)!
""",
    "no_resume": """\
## Agent does not support resume

The agent or ACP adapter does not support resuming sessions.

Update the agent and ACP adapter according to their upstream documentation,
then start a new workspace.

- Use `/close` to return to the launcher, or exit with
  `Ctrl+C`.
- Select the agent and press `Enter` to start a fresh workspace.

If that fails, ask for help in [Discussions](https://github.com/batrachianai/wingmen/discussions)!
""",
}

HELP_URL = "https://github.com/batrachianai/wingmen/discussions"

INTERNAL_EROR = f"""\
## Internal error

The agent reported an internal error:

```
$ERROR
```

This is likely an issue with the agent, and not Wingmen.

- Try the prompt again
- Report the issue to the Agent developer

Ask on {HELP_URL} if you need assistance.

"""

STOP_REASON_MAX_TOKENS = f"""\
## Maximum tokens reached

$AGENT reported that your account is out of tokens.

- You may need to purchase additional tokens, or fund your account.
- If your account has tokens, try running any login or auth process again.

If that fails, ask on {HELP_URL}
"""

STOP_REASON_MAX_TURN_REQUESTS = f"""\
## Maximum model requests reached

$AGENT has exceeded the maximum number of model requests in a single turn.

Need help? Ask on {HELP_URL}
"""

STOP_REASON_REFUSAL = f"""\
## Agent refusal

$AGENT has refused to continue.

Need help? Ask on {HELP_URL}
"""

DISCUSSION_INSTRUCTIONS = """\
Discuss the question at a high level. Do not inspect files, search the shared
workspace, run terminal commands, call tools, or make edits. Answer from the
user's prompt and general knowledge only. State uncertainty rather than
checking the codebase.
"""

DISCUSSION_MODE = Mode(
    "wingmen:discuss",
    "Chat",
    "Chat without inspecting the workspace or using tools",
)
NATIVE_MODE = Mode(
    "wingmen:native",
    "Agent Default",
    "The agent is using a native mode without a Wingmen equivalent",
)


class Cursor(Static):
    """The block 'cursor' -- A vertical line to the left of a block in the conversation that
    is used to navigate the discussion history.
    """

    follow_widget: var[Widget | None] = var(None)
    blink = var(True, toggle_class="-blink")

    def on_mount(self) -> None:
        self.visible = False
        self.blink_timer = self.set_interval(0.5, self._update_blink, pause=True)

    def _update_blink(self) -> None:
        if self.query_ancestor(Window).has_focus and self.screen.is_active:
            self.blink = not self.blink
        else:
            self.blink = False

    def watch_follow_widget(self, widget: Widget | None) -> None:
        self.visible = widget is not None

    def update_follow(self) -> None:
        if self.follow_widget and self.follow_widget.is_attached:
            self.styles.height = max(1, self.follow_widget.outer_size.height)
            follow_y = (
                self.follow_widget.virtual_region.y
                + self.follow_widget.parent.virtual_region.y
            )
            self.offset = Offset(0, follow_y)
        else:
            self.styles.height = None

    def follow(self, widget: Widget | None) -> None:
        self.follow_widget = widget
        self.blink = False
        if widget is None:
            self.visible = False
            self.blink_timer.reset()
            self.blink_timer.pause()
            self.styles.height = None
        else:
            self.visible = True
            self.blink_timer.reset()
            self.blink_timer.resume()
            self.update_follow()


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


class CursorContainer(containers.Vertical):
    def render_lines(self, crop: Region) -> list[Strip]:
        rich_style = self.visual_style.rich_style
        strips = [Strip([Segment("▌", rich_style)], cell_length=1)] * crop.height
        if crop.y == 0 and strips:
            strips[0] = Strip([Segment(" ", rich_style)], cell_length=1)

        return strips


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
    cursor = getters.query_one(Cursor)
    prompt = getters.query_one(Prompt)
    app = getters.app(WingmenApp)

    prompt_history_index: var[int] = var(0, init=False)

    agent: var[AgentBase | None] = var(None, bindings=True)
    agent_info: var[Content] = var(Content())
    agent_ready: var[bool] = var(False)
    modes: var[dict[str, Mode]] = var({}, bindings=True)
    current_mode: var[Mode | None] = var(None)
    turn: var[Literal["agent", "client"] | None] = var(None, bindings=True)
    relay_paused: var[bool] = var(False, toggle_class="-relay-paused")
    discussion_mode: var[bool] = var(False, toggle_class="-discussion-mode")
    status: var[str | Content] = var("")

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
            self._agent_modes[id(mode_agent)] = (modes, current_mode_id)
        else:
            self._unattributed_modes = (modes, current_mode_id)
        if self._mode_agent is None:
            self._mode_agent = mode_agent
        self._refresh_displayed_modes()

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
        """Translate one Wingmen policy and apply it to every active agent."""
        policy = POLICIES_BY_ID.get(policy_id)
        if policy is None:
            self.notify("Unknown Wingmen mode", title="Set Mode", severity="error")
            return

        self._desired_mode_policy_id = policy_id
        await self._sync_desired_mode()

    async def _sync_desired_mode(self) -> bool:
        """Keep every active adapter on Wingmen's desired permission policy."""
        async with self._mode_sync_lock:
            policy = POLICIES_BY_ID[self._desired_mode_policy_id]
            active_agents = self.session.active_agents
            states = [self._agent_modes.get(id(agent)) for agent in active_agents]
            if not active_agents or not all(state is not None for state in states):
                return False

            targets: list[tuple[AgentBase, Mode]] = []
            unsupported: list[str] = []
            for agent, state in zip(active_agents, states):
                assert state is not None
                modes, current_mode = state
                native_mode = policy.resolve(modes)
                if native_mode is None:
                    unsupported.append(str(agent.get_info()))
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
                with CursorContainer(id="cursor-container"):
                    yield Cursor()
                yield Contents(id="contents")
        yield Flash()
        yield Prompt().data_bind(
            project_path=Conversation.project_path,
            working_directory=Conversation.working_directory,
            agent_info=Conversation.agent_info,
            agent_ready=Conversation.agent_ready,
            current_mode=Conversation.current_mode,
            modes=Conversation.modes,
            status=Conversation.status,
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
                self.call_after_refresh(self.cursor.follow, cursor_block)

    async def action_collapse_block(self) -> None:
        if (cursor_block := self.cursor_block) is not None:
            if isinstance(cursor_block, ExpandProtocol):
                cursor_block.collapse_block()
                self.refresh_bindings()
                self.call_after_refresh(self.cursor.follow, cursor_block)

    async def post_agent_response(self, fragment: str = "") -> AgentResponse | None:
        """Get or create an agent response widget."""
        from wingmen.widgets.agent_response import AgentMessage, AgentResponse

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
            return self._agent_response

    async def ensure_agent_message(self, agent: AgentBase) -> AgentMessage:
        """Get or create the attributed container for the current agent turn."""
        from wingmen.widgets.agent_response import AgentMessage

        if self._agent_message is None:
            active_agents = self.session.active_agents
            try:
                agent_index = active_agents.index(agent)
            except ValueError:
                agent_index = 0
            replied_at = datetime.now().astimezone()
            self._agent_message = AgentMessage(
                speaker=self._agent_display_name(agent),
                timestamp=format_reply_timestamp(replied_at, now=replied_at),
                tone_index=agent_index,
            )
            await self.post(self._agent_message, new_block=False)
        return self._agent_message

    def begin_agent_output(self, agent: AgentBase | None) -> None:
        """Open a distinct response stream when relay output changes source."""
        if agent is not None and agent is not self._response_agent:
            self._response_agent = agent
            self._agent_response = None
            self._agent_thought = None
            self._agent_message = None

    async def post_agent_thought(self, thought_fragment: str) -> AgentThought | None:
        """Get or create an agent thought widget."""
        from wingmen.widgets.agent_thought import AgentThought

        async with self._post_lock:
            if self._agent_thought is None:
                if thought_fragment.strip():
                    self._agent_thought = AgentThought(thought_fragment)
                    await self.post(self._agent_thought, new_block=False)
            else:
                await self._agent_thought.append_fragment(thought_fragment)
            return self._agent_thought

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
            # A roster mutation after the session has already started (e.g.
            # `/agent add`) should still identify the newly connected member.
            if newly_connected and message.agent is not None:
                self.flash(
                    Content.assemble(message.agent.get_info(), " connected"),
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
            waiting_names = ", ".join(str(agent.get_info()) for agent in waiting_agents)
            self.flash(
                Content.assemble(
                    message.agent.get_info(),
                    " connected · Waiting for ",
                    waiting_names,
                ),
                style="success",
            )
            return

        if ready_agents:
            names = [str(agent.get_info()) for agent in ready_agents]
            if len(names) == 1:
                connected_names = names[0]
            elif len(names) == 2:
                connected_names = f"{names[0]} and {names[1]}"
            else:
                connected_names = f"{', '.join(names[:-1])}, and {names[-1]}"
            self.flash(
                Content.assemble(connected_names, " connected"),
                style="success",
            )
        elif message.agent is not None:
            # A custom/legacy agent may report readiness before its roster
            # entry is visible. Preserve a useful notification in that case.
            self.flash(
                Content.assemble(message.agent.get_info(), " connected"),
                style="success",
            )
        elif self.agent is not None:
            self.flash(
                Content.assemble(self.agent.get_info(), " connected"),
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
                f"{failed_agent.get_info()} disconnected · reconnecting",
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

        from wingmen.widgets.markdown_note import MarkdownNote

        if message.help in AGENT_FAIL_HELP:
            help = AGENT_FAIL_HELP[message.help]
        else:
            help = AGENT_FAIL_HELP["fail"]

        await self.post(MarkdownNote(help))

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

    @on(acp_messages.ModeUpdate)
    async def on_mode_update(self, event: acp_messages.ModeUpdate) -> None:
        agent = event.agent or self._mode_agent
        if agent is not None:
            modes, _current_mode = self._agent_modes.get(id(agent), ({}, None))
            self._agent_modes[id(agent)] = (modes, event.current_mode)
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
                # Wingmen has processed the slash command.
                return
            await self.post(UserInput(text))
            self.window.scroll_end(animate=False)
            direct_target = self._parse_agent_tag(text)
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
                        "Queued while agents are paused",
                    )
                    return
                if self.turn == "agent":
                    self._queue_relay_prompt(
                        self.session.enqueue_direct(agent_index, direct_prompt),
                        "Queued for the tagged agent",
                    )
                    return
                self.turn = "agent"
                self.send_direct_prompt_to_agent(agent_index, direct_prompt)
                return
            if self._relay_active and self.turn == "agent":
                active_agent = self._active_relay_agent
                active_name = (
                    self._agent_display_name(active_agent)
                    if active_agent is not None
                    else "the active agent"
                )
                self._queue_relay_prompt(
                    self.session.enqueue_human(text), f"Queued for {active_name}"
                )
                return
            if self._relay_active and self.relay_paused:
                self._queue_relay_prompt(
                    self.session.enqueue_human(text), "Queued while agents are paused"
                )
                return
            if self._queue_solo_prompt_if_busy(text):
                return
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
        self.flash("Queued for the agent", style="success")
        return True

    def _queue_relay_prompt(self, accepted: bool, success_message: str) -> None:
        """Tell the user whether a busy relay accepted their follow-up."""
        if accepted:
            self.flash(success_message, style="success")
            return
        self.flash(
            f"Queue is full ({MAX_QUEUED_PROMPTS} messages); wait for an agent",
            style="error",
        )

    @property
    def has_interruptible_work(self) -> bool:
        """Whether Ctrl+C should cancel work instead of quitting Wingmen."""
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
        for terminal in tuple(self._local_shells):
            cancelled = terminal.kill() or cancelled
        return cancelled

    @work
    async def run_local_shell(self, command_text: str) -> None:
        """Run an explicit ``!`` command locally without involving any agent."""
        from wingmen.widgets.terminal_tool import Command, TerminalTool

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
        self.turn = "agent"
        self.send_prompt_to_agent(prompt)
        return True

    def _move_relay_queue_to_solo_agent(self) -> None:
        """Keep queued follow-ups useful after a relay loses all but one peer."""
        queued_prompts = self.session.drain_relay_prompts_for_solo_agent()
        if queued_prompts:
            self._pending_solo_prompts.extend(queued_prompts)
            self.flash("Queued work will continue with the remaining agent", style="success")

    def _parse_agent_tag(self, prompt: str) -> tuple[int, str] | None:
        return self.session.parse_agent_tag(prompt)

    def _agent_display_name(self, agent: AgentBase) -> str:
        return self.session.display_name(agent)

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        minutes, seconds = divmod(max(0, seconds), 60)
        return f"{minutes}:{seconds:02d}"

    def _begin_agent_status(self, agent: AgentBase) -> None:
        """Start the compact roster timer for one sequential agent turn."""
        if self._agent_status_timer is not None:
            self._agent_status_timer.stop()
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
        """Represent relay completion with green roster indicators."""
        self._collaboration_complete = True
        self._active_relay_agent = None
        self._refresh_roster_info()

    def _refresh_roster_info(self) -> None:
        """Show the complete roster and current speaker in the prompt footer."""
        agents = self.session.active_agents
        discussion_indicator: tuple[str, str] = (" · Chat", "$text-accent")
        if len(agents) <= 1:
            if agents:
                agent = agents[0]
                is_working = agent is self._working_agent
                elapsed = self._agent_elapsed.get(id(agent), 0)
                if is_working and self._agent_started_at is not None:
                    elapsed += int(monotonic() - self._agent_started_at)
                if is_working or (
                    self._collaboration_complete and id(agent) in self._agent_elapsed
                ):
                    agent_info = Content.styled(
                        f"● {self._agent_display_name(agent)} · "
                        f"{self._format_elapsed(elapsed)}",
                        "$primary bold" if is_working else "$success bold",
                    )
                else:
                    agent_info = agent.get_info()
            else:
                agent_info = Content.styled("shell")
            self.agent_info = Content.assemble(
                agent_info,
                discussion_indicator if self.discussion_mode else "",
            )
            return

        roster: list[Content | tuple[str, str]] = []
        for index, agent in enumerate(agents):
            if index:
                roster.append((" · ", "dim"))
            is_current = (
                agent is self._working_agent or agent is self._active_relay_agent
            )
            is_ready = id(agent) in self._ready_agents
            elapsed = self._agent_elapsed.get(id(agent), 0)
            is_timed = agent is self._working_agent and self._agent_started_at is not None
            if is_timed and self._agent_started_at is not None:
                elapsed += int(monotonic() - self._agent_started_at)
            is_complete = (
                self._collaboration_complete and id(agent) in self._agent_elapsed
            )
            marker = "●" if is_current or is_complete else "○" if is_ready else "…"
            timer = (
                f" · {self._format_elapsed(elapsed)}"
                if is_timed or is_complete
                else ""
            )
            roster.append(
                Content.styled(
                    f"{marker} {self._agent_display_name(agent)}{timer}",
                    "$primary bold"
                    if is_current
                    else "$success bold"
                    if is_complete
                    else "$text-secondary",
                )
            )
        if self.discussion_mode:
            roster.append(discussion_indicator)
        self.agent_info = Content.assemble(*roster)

    def _set_discussion_mode(self, value: str) -> None:
        """Switch the Wingmen-owned conversation policy for every agent."""
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

    @work
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

    @work
    async def send_prompt_to_agent(self, prompt: str) -> None:
        if self._relay_active:
            self._begin_collaboration()
            self.busy_count += 1
            try:
                self.turn = "agent"
                result = await self.session.send_prompt(prompt)
                assert isinstance(result, RelayResult)
                if result.reason == "max_rounds":
                    from wingmen.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "The relay stopped after reaching its "
                            f"{result.rounds}-round safety limit.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "paused":
                    from wingmen.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "All agents are paused. Queued work will resume when "
                            "you resume the relay.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "roster_collapsed":
                    from wingmen.widgets.markdown_note import MarkdownNote

                    await self.post(
                        MarkdownNote(
                            "The relay stopped because fewer than two agents "
                            "are left in the roster.",
                            classes="-stop-reason",
                        )
                    )
                elif result.reason == "stop_token":
                    self._mark_collaboration_complete()
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
        from wingmen.widgets.markdown_note import MarkdownNote

        message = str(error).strip() or "no details were provided"
        await self.post(
            MarkdownNote(
                INTERNAL_EROR.replace("$ERROR", message),
                classes="-stop-reason",
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

        if await self._dispatch_next_solo_prompt():
            return

        if stop_reason != "end_turn":
            from wingmen.widgets.markdown_note import MarkdownNote

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
            self.app.system_notify(
                f"{self.agent_title} has finished working",
                title="Waiting for input",
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
            SlashCommand("/about", "About Wingmen and multi-agent relays"),
            SlashCommand("/help", "Show Wingmen commands"),
            SlashCommand("/config", "Configure Wingmen preferences"),
            SlashCommand("/mode", "Open the mode picker"),
            SlashCommand(
                "/clear",
                "Clear conversation window",
                "<optional number of lines to preserve>",
            ),
            SlashCommand(
                "/close",
                "Close the current session",
            ),
            SlashCommand(
                "/resume",
                "Resume a saved agent session",
                "<optional session number>",
            ),
            SlashCommand(
                "/agent",
                "Manage the relay roster",
                "add <agent> | drop <n> | list",
            ),
        ]
        if self._relay_active:
            slash_commands.append(
                SlashCommand("/pause", "Pause or resume all agents")
            )

        # Wingmen handles its own commands locally, so they must retain their
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

    async def _slash_resume(self, parameters: str) -> None:
        """Launch a saved ACP session, defaulting to the previous one."""
        requested = parameters.strip()
        db = DB()
        if requested:
            try:
                session_pk = int(requested)
            except ValueError:
                self.flash("Use /resume <session number>", style="error")
                return
            session = await db.session_get(session_pk)
        else:
            sessions = await db.session_get_recent()
            current_session_pk = self.session.session_pk
            session = next(
                (
                    candidate
                    for candidate in sessions or []
                    if candidate["id"] != current_session_pk
                ),
                None,
            )

        if session is None:
            self.flash("No saved session is available to resume", style="warning")
            return
        if session.get("protocol") != "acp":
            self.flash("Only ACP sessions can be resumed", style="error")
            return

        self.post_message(
            messages.LaunchAgent(
                session["agent_identity"],
                session_id=session["agent_session_id"],
                pk=session["id"],
            )
        )

    async def _slash_agent(self, parameters: str) -> None:
        """Handle ``/agent [list|add <agent>|drop <n>]``."""
        from wingmen.widgets.markdown_note import MarkdownNote

        verb, _, argument = parameters.partition(" ")
        verb = verb.strip().lower()
        argument = argument.strip()

        if not verb or verb == "list":
            if not self.session.roster:
                self.flash("No agent is running in this session", style="error")
                return
            lines = ["**Roster**"]
            for index, entry in enumerate(self.session.roster):
                status = "" if entry.active else " — dropped"
                owner = " — session owner" if index == 0 else ""
                lines.append(
                    f"{index + 1}. {entry.data['name']} "
                    f"(`{self.session.agent_tag(index)}`){owner}{status}"
                )
            if len(self.session.roster) > 1:
                lines.append(
                    "\nAddress one directly with the displayed `@tag: instruction`."
                )
            await self.post(MarkdownNote("\n".join(lines), classes="-agent-identity"))
            return

        if verb == "add":
            if not argument:
                self.flash("Usage: /agent add <short_name|identity>", style="error")
                return
            await self._roster_add(argument)
            return

        if verb == "drop":
            try:
                index = int(argument) - 1
            except ValueError:
                self.flash("Usage: /agent drop <n>", style="error")
                return
            await self._roster_drop(index)
            return

        self.flash(f"Unknown /agent action: {verb!r}", style="error")

    async def _roster_add(self, name: str) -> None:
        """Resolve and start a new agent, appending it to the roster."""
        if self.agent is None:
            self.flash("No agent is running in this session", style="error")
            return
        from wingmen.agents import resolve_agent

        data = await resolve_agent(name)
        if data is None:
            self.flash(f"Agent not found: {name}", style="error")
            return

        try:
            await self.session.add(
                data,
                self,
                on_turn_start=self._label_relay_turn_start,
                on_turn=self._label_relay_turn,
            )
        except Exception as error:
            details = str(error).strip() or "no details were provided"
            self.flash(f"Unable to add {data['name']}: {details}", style="error")
            return
        self._refresh_roster_info()
        self._refresh_displayed_modes()
        await self._sync_desired_mode()
        self.update_slash_commands()

        self.flash(f"{data['name']} joined the roster", style="success")
        await self._persist_roster()

    async def _roster_drop(self, index: int) -> None:
        """Tombstone a roster entry. The session owner (index 0) is protected."""
        if index == 0:
            self.flash(
                "Agent 1 owns the session; use /close instead",
                style="error",
            )
            return
        if not 0 <= index < len(self.session.roster):
            self.flash("No such agent number", style="error")
            return
        entry = self.session.roster[index]
        if not entry.active:
            self.flash(f"{entry.data['name']} is already dropped", style="warning")
            return
        await self.session.drop(index)
        self._move_relay_queue_to_solo_agent()
        self._refresh_roster_info()
        self._refresh_displayed_modes()
        await self._sync_desired_mode()
        self.update_slash_commands()
        self.flash(f"{entry.data['name']} dropped from the roster", style="success")
        await self._persist_roster()

    async def _persist_roster(self) -> None:
        """Best-effort record of the roster in the session's meta JSON.

        Not read back in this version: only the session owner (index 0) has
        an ``agent_session_id`` to resume against, so restoring peers is
        deferred. Writing the roster now keeps a future resume-aware reader
        forward-compatible without a schema change.

        Also updates the ``launcher.roster`` setting, which is what a bare
        ``wingmen`` invocation restores on the next launch.
        """
        await self.session.persist_roster(self.app.settings, self.app.save_settings)

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
                from wingmen.widgets.markdown_note import MarkdownNote

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
        self.cursor.visible = False
        self.cursor.follow(None)
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
            self.flash("Agents resumed", style="success")
            self.send_prompt_to_agent(
                "Resume the collaboration from the current shared workspace."
            )
            return

        self.relay_paused = True
        self.session.pause()
        for agent in self.session.active_agents:
            await agent.cancel()
        self.flash("All agents paused", style="warning")

    def focus_prompt(self, reset_cursor: bool = True, scroll_end: bool = True) -> None:
        """Focus the prompt input.

        Args:
            reset_cursor: Reset the block cursor.
            scroll_end: Scroll t the end of the content.
        """
        if reset_cursor:
            self.cursor_offset = -1
            self.cursor.visible = False
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
            self.cursor.visible = True
            self.cursor.follow(cursor_block)
            self.call_after_refresh(
                self.window.scroll_to_center, cursor_block, immediate=True
            )
        else:
            self.cursor.visible = False
            self.window.anchor(False)
            self.window.scroll_end(duration=2 / 10)
            self.cursor.follow(None)
            self.prompt.focus()
        self.refresh_bindings()

    async def slash_command(self, text: str) -> bool:
        """Give Wingmen the opportunity to process slash commands.

        Args:
            text: The prompt, including the slash in the first position.

        Returns:
            `True` if Wingmen has processed the slash command, `False` if it should
                be forwarded to the agent.
        """
        command, _, parameters = text[1:].partition(" ")
        if command in {"about", "wingmen:about"}:
            from wingmen.widgets.markdown_note import MarkdownNote

            await self.post(
                MarkdownNote(
                    f"""## Wingmen v{wingmen.get_version()}

Terminal workspace for collaborating with one or more coding agents.

- The roster is shown beside the prompt; the filled marker is speaking.
- Send a normal message to continue the relay, or `@agent: message` to address one agent.
- `/agent list` shows the active roster.

Wingmen is licensed under the [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.txt).""",
                    classes="about",
                )
            )
            return True
        if command == "help":
            from wingmen.widgets.markdown_note import MarkdownNote

            await self.post(
                MarkdownNote(
                    """## Wingmen help

### Conversation

- `!<command>` — run a shell command directly in the current workspace
- `/agent list|add <agent>|drop <n>` — inspect or change this session's roster
- `/mode` — choose one mode for every active agent
- `/mode chat` — chat without workspace inspection or tools
- `/pause` — pause or resume a multi-agent relay
- `/clear [lines]` — clear the conversation window
- `/close` — close this workspace and return to agent selection
- `/resume [session number]` — reopen the previous or selected saved session

### Wingmen

- `/config` — settings and the next workspace's agent roster
- `/about` — version and relay overview

Drag over conversation text and press `Ctrl+C` to copy it. Otherwise,
`Ctrl+C` cancels active work; press it again within three seconds to quit.
"""
                )
            )
            return True
        if command == "config":
            from wingmen.screens.config import ConfigScreen

            self.app.push_screen(ConfigScreen())
            return True
        if command == "mode":
            if parameters.strip().lower() in {"chat", "discuss", "discussion"}:
                self._set_discussion_mode("chat")
            elif parameters.strip():
                self.flash("Use /mode to choose a mode", style="error")
            else:
                await self.action_mode_switcher()
            return True
        # The concise forms are primary. Keep namespaced forms as quiet
        # compatibility aliases for existing prompt histories and scripts.
        if command in {"pause", "wingmen:pause"}:
            self.action_toggle_pause()
            return True
        if command in {"agent", "wingmen:agent"}:
            await self._slash_agent(parameters.strip())
            return True
        if command == "resume":
            await self._slash_resume(parameters)
            return True
        if command in {"clear", "wingmen:clear"}:
            try:
                line_count = max(0, int(parameters) if parameters.strip() else 0)
            except ValueError:
                self.notify(
                    "Unable to clear—a number was expected",
                    title="/clear",
                    severity="error",
                )
                return True
            await self.prune_window(line_count, line_count)
            return True
        elif command in {"close", "wingmen:session-close"}:
            return await self._close_session()
        if any(
            slash.command.removeprefix("/") == command
            for slash in self.agent_slash_commands
        ):
            return False
        self.flash(
            f"Unknown command: /{command}. Type /help to see Wingmen commands.",
            style="error",
        )
        return True
