from pathlib import Path
from typing import Sequence

from textual import on
from textual.app import ComposeResult
from textual import getters
from textual.binding import Binding
from textual.screen import Screen
from textual.reactive import var, reactive
from textual import containers


from codeswarm.app import CodeSwarmApp
from codeswarm import messages
from codeswarm.agent_schema import Agent
from codeswarm.widgets.conversation import Conversation


class MainScreen(Screen, can_focus=False):
    AUTO_FOCUS = "Conversation Prompt TextArea"

    CSS_PATH = "main.tcss"

    BINDINGS: list[Binding] = []

    BINDING_GROUP_TITLE = "Screen"
    busy_count = var(0)
    conversation = getters.query_one(Conversation)

    scrollbar = reactive("")
    project_path: var[Path] = var(Path("./").expanduser().absolute())

    app = getters.app(CodeSwarmApp)

    def __init__(
        self,
        project_path: Path,
        agent: Agent | None = None,
        agent_session_id: str | None = None,
        session_pk: int | None = None,
        initial_prompt: str | None = None,
        peers: Sequence[Agent] = (),
        first_agent: int = 0,
        max_rounds: int = 100,
    ) -> None:
        super().__init__()
        self.set_reactive(MainScreen.project_path, project_path)
        self._agent = agent
        self._agent_session_id = agent_session_id
        self._session_pk = session_pk
        self._initial_prompt = initial_prompt
        self._peers = list(peers)
        self._first_agent = first_agent
        self._max_rounds = max_rounds

    def watch_title(self, title: str) -> None:
        self.app.update_terminal_title()

    def compose(self) -> ComposeResult:
        with containers.Center():
            yield Conversation(
                self.project_path,
                self._agent,
                self._agent_session_id,
                self._session_pk,
                initial_prompt=self._initial_prompt,
                peers=self._peers,
                first_agent=self._first_agent,
                max_rounds=self._max_rounds,
            ).data_bind(
                project_path=MainScreen.project_path,
            )

    def update_node_styles(self, animate: bool = True) -> None:
        self.conversation.update_node_styles(animate=animate)

    @on(messages.SessionClose)
    async def on_session_close(self, event: messages.SessionClose) -> None:

        if self.id is None:
            return
        current_mode = self.id
        session_tracker = self.app.session_tracker

        await self.conversation.shutdown()
        session_tracker.close_session(current_mode)
        await self.app.switch_mode("store")

        self.app.call_later(self.app.remove_mode, current_mode)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return True

    def action_focus_prompt(self) -> None:
        self.conversation.focus_prompt()

    def watch_scrollbar(self, old_scrollbar: str, scrollbar: str) -> None:
        if old_scrollbar:
            self.conversation.remove_class(f"-scrollbar-{old_scrollbar}")
        if scrollbar:
            self.conversation.add_class(f"-scrollbar-{scrollbar}")
