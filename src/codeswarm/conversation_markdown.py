from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownBlock
from textual.content import Content



class ConversationCodeFence(Markdown.BLOCKS["fence"]):
    @classmethod
    def highlight(
        cls, code: str, language: str, ansi: bool = False, dark: bool = False
    ) -> Content:
        """Render agent code with the same foreground as surrounding prose."""
        return Content(code)

    def get_block_content(self, destination: str) -> str | None:
        if destination == "clipboard":
            return self._content.plain
        return self.source


CUSTOM_BLOCKS = {"fence": ConversationCodeFence}


class ConversationMarkdown(Markdown):
    """Markdown widget with custom blocks."""

    def get_block_class(self, block_name: str) -> type[MarkdownBlock]:
        if (custom_block := CUSTOM_BLOCKS.get(block_name)) is not None:
            return custom_block
        return super().get_block_class(block_name)
