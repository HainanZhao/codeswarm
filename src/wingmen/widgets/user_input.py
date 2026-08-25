from textual.app import ComposeResult
from textual import containers
from textual.widgets import Markdown

from wingmen.widgets.non_selectable_label import NonSelectableLabel

class UserInput(containers.HorizontalGroup):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        with containers.HorizontalGroup(id="user-bubble"):
            yield Markdown(self.content, id="content")
            yield NonSelectableLabel("TX ▸", id="prompt")

    def get_block_content(self, destination: str) -> str | None:
        return self.content
