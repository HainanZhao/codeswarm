import re
from typing import ClassVar

from textual.content import Content, Span
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownBlock


FILE_REFERENCE_PATTERN = re.compile(
    r"(?<![\w])(?:\./|\.\./|/)?"
    r"(?:[\w.-]+/)*(?:[\w.-]+\."
    r"(?:bash|c|cc|cpp|css|go|h|hpp|html|ini|java|js|json|jsx|kotlin|kt|md|php|py|pyi|rb|rs|sh|sql|swift|toml|ts|tsx|xml|yaml|yml)"
    r"|(?:Dockerfile|Makefile|Justfile|Procfile|\.env(?:\.[\w.-]+)?))"
    r"(?::\d+(?:-\d+)?)?(?![\w./-])"
)


def _has_protected_span(content: Content, start: int, end: int) -> bool:
    """Keep code and clickable Markdown spans from being double-styled."""
    for span in content.spans:
        if span.start >= end or span.end <= start:
            continue
        if span.style == ".code_inline" or "@click" in getattr(
            span.style, "meta", {}
        ):
            return True
    return False


def _style_file_references(content: Content) -> Content:
    """Add a visual component style to recognizable source file references."""
    spans: list[Span] = []
    text = content.plain
    for match in FILE_REFERENCE_PATTERN.finditer(text):
        start, end = match.span()
        if "://" in text[max(0, start - 8) : start]:
            continue
        if not _has_protected_span(content, start, end):
            spans.append(Span(start, end, ".file_reference"))
    return content.add_spans(spans)


class _ConversationInlineContentMixin:
    """Apply CodeSwarm's content-level path styling to Markdown blocks."""

    COMPONENT_CLASSES: ClassVar[set[str]] = MarkdownBlock.COMPONENT_CLASSES | {
        "file_reference",
    }

    def _token_to_content(self, token: object) -> Content:
        content = super()._token_to_content(token)  # type: ignore[misc]
        return _style_file_references(content)


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
_INLINE_BLOCKS: dict[type[MarkdownBlock], type[MarkdownBlock]] = {}


def _inline_block_class(
    block_class: type[MarkdownBlock],
) -> type[MarkdownBlock]:
    """Preserve Textual's Markdown block behavior while adding file spans."""
    if block_class not in _INLINE_BLOCKS:
        _INLINE_BLOCKS[block_class] = type(
            f"Conversation{block_class.__name__}",
            (_ConversationInlineContentMixin, block_class),
            {
                "COMPONENT_CLASSES": block_class.COMPONENT_CLASSES
                | {"file_reference"},
            },
        )
    return _INLINE_BLOCKS[block_class]


class ConversationMarkdown(Markdown):
    """Markdown widget with custom blocks."""

    def get_block_class(self, block_name: str) -> type[MarkdownBlock]:
        if (custom_block := CUSTOM_BLOCKS.get(block_name)) is not None:
            return custom_block
        return _inline_block_class(super().get_block_class(block_name))
