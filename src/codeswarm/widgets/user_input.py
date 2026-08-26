from textual.app import ComposeResult
from textual import containers
from textual.widgets import Markdown

class UserInput(containers.HorizontalGroup):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        with containers.HorizontalGroup(id="user-bubble"):
            yield Markdown(self.content, id="content")

    def get_block_content(self, destination: str) -> str | None:
        return self.content
