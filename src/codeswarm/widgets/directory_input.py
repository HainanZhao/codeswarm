from textual.widgets import Input

from codeswarm.directory_suggester import DirectorySuggester


class DirectoryInput(Input):
    def on_mount(self) -> None:
        self.suggester = DirectorySuggester()
