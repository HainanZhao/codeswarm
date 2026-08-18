from textual.app import ComposeResult
from textual import containers

from textual.widgets import Label, Markdown


ASCII_TAIJI = "☯"


WELCOME_MD = """\
## Taiji v1.0

Welcome, **Will**!


"""


class Welcome(containers.Vertical):
    def compose(self) -> ComposeResult:
        with containers.Center():
            yield Label(ASCII_TAIJI, id="logo")
        yield Markdown(WELCOME_MD, id="message", classes="note")
