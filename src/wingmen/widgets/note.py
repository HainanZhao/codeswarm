from textual.widgets import Static


class Note(Static):
    DEFAULT_CLASSES = "block"

    def get_block_content(self, destination: str) -> str | None:
        return str(self.render())

    def action_hello(self, message: str) -> None:
        self.notify(message, severity="warning")
