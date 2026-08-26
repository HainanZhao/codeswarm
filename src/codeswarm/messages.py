from dataclasses import dataclass

from typing import Literal

from textual.content import Content
from textual.widget import Widget
from textual.message import Message

class WorkStarted(Message):
    """Work has started."""


class WorkFinished(Message):
    """Work has finished."""


@dataclass
class HistoryMove(Message):
    """Getting a new item form history."""

    direction: int


@dataclass
class UserInputSubmitted(Message):
    body: str
    auto_complete: bool = False


@dataclass
class PromptSuggestion(Message):
    suggestion: str


@dataclass
class Dismiss(Message):
    widget: Widget

    @property
    def control(self) -> Widget:
        return self.widget


@dataclass
class InsertPath(Message):
    path: str


@dataclass
class ChangeMode(Message):
    mode_id: str | None


@dataclass
class Flash(Message):
    """Request a message flash.

    Args:
        Message: Content of flash.
        style: Semantic style.
        duration: Duration in seconds or `None` for default.
    """

    content: str | Content
    style: Literal["default", "warning", "success", "error"]
    duration: float | None = None


@dataclass
class SessionClose(Message):
    name: str
    """Name of the session."""

@dataclass
class LaunchAgent(Message):
    """Inform app to launch agent."""

    identity: str
    session_id: str | None = None
    pk: int | None = None
    prompt: str | None = None
    peers: tuple[str, ...] = ()
    """Identities of additional agents to launch alongside `identity`,
    forming a relay roster. Empty for a solo session."""
