from typing import Literal

from textual.content import Content
from textual.reactive import var
from textual.widgets import Static
from textual.timer import Timer
from textual import getters


from wingmen.app import WingmenApp


class Flash(Static):
    DEFAULT_CSS = """
    Flash {
        height: 1;
        width: 100%;
        margin-bottom: 1;
        padding: 0 1;
        background: $primary 18%;
        color: $primary;
        text-align: left;
        visibility: hidden;
        text-wrap: nowrap;
        text-overflow: ellipsis;     
        # overlay: screen;
        # offset-y: -1;           
        &.-default {
            background: $primary 18%;
            color: $primary;
        }
        
        &.-success {
            background: $primary 18%;
            color: $primary;
        }
        
        
        &.-warning {
            background: $primary 18%;
            color: $primary;
        }

        &.-error {
            background: $primary 18%;
            color: $primary;
        }
    }
    """
    app = getters.app(WingmenApp)
    flash_timer: var[Timer | None] = var(None)

    def flash(
        self,
        content: str | Content,
        *,
        duration: float | None = None,
        style: Literal["default", "success", "warning", "error"] = "default",
    ) -> None:
        """Flash the content for a brief period.

        Args:
            content: Content to show.
            duration: Duration in seconds to show content.
            style: A semantic style.
        """
        if self.flash_timer is not None:
            self.flash_timer.stop()
        self.visible = False

        def hide() -> None:
            """Hide the content after a while."""
            self.visible = False

        self.update(content)
        self.remove_class("-default", "-success", "-warning", "-error", update=False)
        self.add_class(f"-{style}")
        self.visible = True

        if duration is None:
            duration = self.app.settings.get("ui.flash_duration", float)

        self.flash_timer = self.set_timer(duration or 3, hide)
