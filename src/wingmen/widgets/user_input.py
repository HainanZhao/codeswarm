from textual.app import ComposeResult
from textual import containers
from textual.widgets import Markdown

from wingmen.widgets.non_selectable_label import NonSelectableLabel


class UserInput(containers.HorizontalGroup):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content
        self.queue_status = NonSelectableLabel("", id="user-queue-status")
        self.queue_status.display = False

    def compose(self) -> ComposeResult:
        with containers.VerticalGroup(id="user-bubble"):
            with containers.HorizontalGroup(id="user-content"):
                yield Markdown(self.content, id="content")
                yield NonSelectableLabel("❯", id="prompt")
            yield self.queue_status

    def set_queue_status(self, status: str) -> None:
        """Show a compact delivery state beneath this submitted message."""
        self.queue_status.update(status)
        self.queue_status.display = bool(status)

    def get_block_content(self, destination: str) -> str | None:
        return self.content
