from textual.widgets import Markdown


class MarkdownNote(Markdown):
    def get_block_content(self, destination: str) -> str | None:
        return self.source
