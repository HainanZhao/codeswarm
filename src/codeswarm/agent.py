from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from textual.content import Content
from textual.message import Message
from textual.message_pump import MessagePump


class AgentReady(Message):
    """Agent is ready."""

    def __init__(self, agent: "AgentBase | None" = None) -> None:
        super().__init__()
        self.agent = agent


@dataclass
class AgentFail(Message):
    """Agent failed to start."""

    message: str
    details: str = ""
    help: str = "fail"
    agent: "AgentBase | None" = None


class AgentBase(ABC):
    """Base class for an 'agent'."""

    def __init__(self, project_root: Path) -> None:
        self.project_root_path = project_root
        super().__init__()

    async def start(self, message_target: MessagePump | None = None) -> None:
        """Start the agent process.

        Concrete protocols may override this. Keeping lifecycle methods on
        the base contract lets session orchestration remain UI-independent.
        """

    @abstractmethod
    async def send_prompt(self, prompt: str) -> str | None:
        """Send a prompt to the agent.

        Args:
            prompt: Prompt text.

        Returns:
            str: The stop reason.
        """

    async def set_mode(self, mode_id: str) -> str | None:
        """Put the agent in a new mode.

        Args:
            mode_id: Mode id.

        Returns:
            str: The stop reason.
        """

    @property
    def supports_startup_full_access(self) -> bool:
        """Whether full access is controlled by an adapter process argument."""
        return False

    @property
    def startup_full_access(self) -> bool:
        """Whether this process is configured to bypass permissions."""
        return False

    def configure_startup_full_access(self, enabled: bool) -> None:
        """Configure process-backed full access before the adapter starts."""
        if enabled:
            raise ValueError("agent does not support startup full access")

    async def cancel(self) -> bool:
        """Cancel prompt.

        Returns:
            bool: `True` if success, `False` if the turn wasn't cancelled.

        """
        return False

    def get_info(self) -> Content:
        return Content("")

    def set_roster_introduction(self, introduction: str) -> None:
        """Provide one-time CodeSwarm context before the first prompt."""

    async def stop(self) -> None:
        """Stop the agent (gracefully exit the process)"""
