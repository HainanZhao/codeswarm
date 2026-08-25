from __future__ import annotations


import asyncio
import concurrent.futures

from operator import itemgetter
import os
from pathlib import Path

from typing import Sequence, Self


from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual import work
from textual import getters
from textual import containers
from textual import events

from textual.cache import LRUCache
from textual.reactive import var, Initialize
from textual.content import Content, Span
from textual.strip import Strip
from textual.style import Style
from textual.widget import Widget
from textual import widgets
from textual.visual import RenderOptions
from textual.widgets import OptionList, Input
from textual.widgets.option_list import Option

from wingmen import directory
from wingmen.fuzzy_index import FuzzyIndex
from wingmen.messages import Dismiss, InsertPath, PromptSuggestion
from wingmen.path_filter import PathFilter
from wingmen._path_fuzzy_search import PathFuzzySearch
from wingmen._path_match import match_path

MAX_SEARCH_RESULTS = 30
PATH_SEARCH_CACHE_SIZE = 64


class PathContent(Content):

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        """Render the Visual into an iterable of strips. Part of the Visual protocol.

        Args:
            width: Width of desired render.
            height: Height of desired render or `None` for any height.
            style: The base style to render on top of.
            options: Additional render options.

        Returns:
            An list of Strips.
        """
        if not width:
            return []

        line = self
        if line.cell_length > width:
            while line.cell_length >= width - 3 and "/" in line.plain:
                line = line[line.plain.find("/") + 1 :]
            line = Content.assemble(("⋯ ", "$text-error"), line)

        lines = line._wrap_and_format(
            width,
            tab_size=8,
            overflow="clip",
            no_wrap=True,
            selection=options.selection,
            selection_style=options.selection_style,
            post_style=options.post_style,
            get_style=options.get_style,
        )

        if height is not None:
            lines = lines[:height]

        strip_lines = [Strip(*line.to_strip(style)) for line in lines]
        return strip_lines


class FuzzyPathOptionList(OptionList):
    """Option list with loading indicator override."""

    def get_loading_widget(self) -> Widget:
        from textual.widgets import LoadingIndicator

        return LoadingIndicator()


class FuzzyInput(Input):
    """Adds a Content placeholder to fuzzy input.

    TODO: Add this ability to Textual.
    """

    HELP = """\
## Fuzzy search

Type a few characters from the file you are searching for.

The search is *fuzzy*, and will match characters that aren't necessarily next to each other—only the order matters.
"""

    def render_line(self, y: int) -> Strip:
        if y == 0 and not self.value:
            placeholder = Content.from_markup(self.placeholder).expand_tabs()
            placeholder = placeholder.stylize(self.visual_style)
            placeholder = placeholder.stylize(
                self.get_visual_style("input--placeholder")
            )
            if self.has_focus:
                cursor_style = self.get_visual_style("input--cursor")
                if self._cursor_visible:
                    # If the placeholder is empty, there's no characters to stylise
                    # to make the cursor flash, so use a single space character
                    if len(placeholder) == 0:
                        placeholder = Content(" ")
                    placeholder = placeholder.stylize(cursor_style, 0, 1)

            strip = Strip(placeholder.render_segments())
            return strip

        return super().render_line(y)


class PathSearch(containers.VerticalGroup):

    BINDING_GROUP_TITLE = "Path search"

    CURSOR_BINDING_GROUP = Binding.Group(description="Move selection")
    BINDINGS = [
        Binding(
            "up", "cursor_up", "Cursor up", group=CURSOR_BINDING_GROUP, priority=True
        ),
        Binding(
            "down",
            "cursor_down",
            "Cursor down",
            group=CURSOR_BINDING_GROUP,
            priority=True,
        ),
        Binding("enter", "submit", "Insert path", priority=True, show=False),
        Binding("escape", "dismiss", "Dismiss", priority=True, show=False),
    ]

    def get_fuzzy_search(self) -> PathFuzzySearch:
        return PathFuzzySearch(case_sensitive=False)

    root: var[Path] = var(Path("./"))
    paths: var[list[Path]] = var(list)
    display_paths: var[list[str]] = var(list)
    filtered_path_indices: var[list[int]] = var(list)
    loaded = var(False)
    filter = var("")
    fuzzy_search: var[PathFuzzySearch] = var(Initialize(get_fuzzy_search))

    option_list = getters.query_one(FuzzyPathOptionList)
    input = getters.query_one(Input)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.set_reactive(PathSearch.root, root)
        self.root = root
        self.fuzzy_index = FuzzyIndex()
        self._pool: concurrent.futures.InterpreterPoolExecutor | None = None
        self._mounted = False
        self.search_cache: LRUCache[str, list[tuple[float, Sequence[int], str]]] = (
            LRUCache(PATH_SEARCH_CACHE_SIZE)
        )

    def compose(self) -> ComposeResult:
        yield FuzzyInput(compact=True, placeholder="fuzzy search")
        yield FuzzyPathOptionList()

    def on_mount(self) -> None:
        """Mark the search as available without indexing the project yet."""
        self._mounted = True

    def on_unmount(self) -> None:
        """Release the CPU workers used only while fuzzy search is active."""
        self._mounted = False
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def fuzzy_match_paths(
        self, search: str, paths: list[str]
    ) -> list[tuple[float, Sequence[int], str]]:
        pool = self._pool
        if pool is None:
            pool = self._pool = concurrent.futures.InterpreterPoolExecutor(
                thread_name_prefix=f"fuzzy-path-search-{self.root}"
            )
        scores = list(
            pool.map(
                match_path,
                [(search, path) for path in paths],
                chunksize=10,
            )
        )
        return scores

    @staticmethod
    def rank_matches(
        matches: Sequence[tuple[float, Sequence[int], str]],
    ) -> list[tuple[float, Sequence[int], str]]:
        """Return the best displayable matches without retaining excess results."""
        return sorted(
            (match for match in matches if match[0]),
            key=itemgetter(0),
            reverse=True,
        )[:MAX_SEARCH_RESULTS]

    @work(exclusive=True, description="search_paths")
    async def search(self, search: str) -> None:
        if not search:
            self.option_list.set_options(
                [
                    Option(self.highlight_path(path), path)
                    for path in self.display_paths[:100]
                ],
            )
            return

        display_paths = await self.fuzzy_index.search(search)

        if len(display_paths) > 20:
            if (scored_paths := self.search_cache.get(search)) is None:
                scored_paths = await asyncio.to_thread(
                    self.fuzzy_match_paths, search, display_paths
                )
                scored_paths = self.rank_matches(scored_paths)
                self.search_cache[search] = scored_paths
        else:
            fuzzy_search = self.fuzzy_search
            scored_paths: list[tuple[float, Sequence[int], str]] = [
                (
                    *fuzzy_search.match(search, path),
                    path,
                )
                for path in display_paths
            ]
            scored_paths = self.rank_matches(scored_paths)

        scores = [
            (score, highlights, self.highlight_path(path))
            for score, highlights, path in scored_paths
        ]

        def highlight_offsets(path: Content, offsets: Sequence[int]) -> Content:
            highlighted_path = path.add_spans(
                [Span(offset, offset + 1, "underline") for offset in offsets]
            )
            return PathContent(
                highlighted_path.plain,
                list(highlighted_path.spans),
                highlighted_path.cell_length,
            )

        self.option_list.set_options(
            [
                Option(highlight_offsets(path, offsets), id=path.plain)
                for index, (score, offsets, path) in enumerate(scores)
            ]
        )
        with self.option_list.prevent(OptionList.OptionHighlighted):
            self.option_list.highlighted = 0
        self.post_message(PromptSuggestion(""))

    def action_cursor_down(self) -> None:
        self.option_list.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.option_list.action_cursor_up()

    def action_dismiss(self) -> None:
        self.post_message(Dismiss(self))
        self.filter = ""

    def on_show(self) -> None:
        self.focus()

    def focus(self, scroll_visible: bool = False) -> Self:
        return self.input.focus(scroll_visible=scroll_visible)

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if event.widget == self.input:
            self.post_message(Dismiss(self))

    @on(Input.Changed)
    def on_input_changed(self, event: Input.Changed) -> None:
        self.search(event.value)

    @on(OptionList.OptionHighlighted)
    async def on_option_list_changed(self, event: OptionList.OptionHighlighted):
        event.stop()
        if event.option:
            self.post_message(PromptSuggestion(event.option.id))

    @on(OptionList.OptionSelected)
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_submit()

    def action_submit(self):
        if (highlighted := self.option_list.highlighted) is not None:
            option = self.option_list.options[highlighted]
            if option.id:
                self.post_message(InsertPath(option.id))
                self.post_message(Dismiss(self))

    def get_path_filter(self, project_path: Path) -> PathFilter:
        """Get a PathFilter insance for the give project path.

        Args:
            project_path: Project path.

        Returns:
            `PathFilter` object.
        """
        path_filter = PathFilter.from_git_root(project_path)
        return path_filter

    def reset(self) -> None:
        """Reset and focus input."""
        self.input.clear()
        self.input.focus()

    @work(exclusive=True)
    async def refresh_paths(self):
        if not self._mounted:
            return
        self.option_list.set_loading(True)
        root = self.root
        try:
            path_filter = await asyncio.to_thread(self.get_path_filter, root)
            paths = await directory.scan(
                root, path_filter=path_filter, add_directories=True
            )

            def make_absolute(paths: list[Path]) -> list[Path]:
                """Make all paths absolute.

                Args:
                    paths: A list of paths.

                Returns:
                    List of absolute paths,

                """
                return [path.absolute() for path in paths]

            paths = await asyncio.to_thread(make_absolute, paths)
            self.root = root
            self.paths = paths
            self.loaded = True
        except Exception:
            self.option_list.set_loading(False)
            raise

    def highlight_path(self, path: str) -> PathContent:
        content = Content.styled(path, "$text 50%")
        if os.path.split(path)[-1].startswith("."):
            return PathContent(
                content.plain, list(content.spans), cell_length=content.cell_length
            )
        content = content.highlight_regex("[^/]*?$", style="$text-primary")
        content = content.highlight_regex(r"\.[^/]*$", style="italic")
        return PathContent(
            content.plain, list(content.spans), cell_length=content.cell_length
        )

    @work(exclusive=True, description="watch_paths")
    async def watch_paths(self, paths: list[Path]) -> None:
        if not self._mounted:
            return

        def path_display(path: Path) -> str:
            try:
                is_directory = path.is_dir()
            except OSError:
                is_directory = False
            if is_directory:
                return str(path.relative_to(self.root)) + "/"
            else:
                return str(path.relative_to(self.root))

        def make_display_paths() -> list[str]:
            display_paths = sorted(map(path_display, paths), key=str.lower)
            display_paths.sort(key=lambda path: path.count("/"))
            return display_paths

        self.display_paths = await asyncio.to_thread(make_display_paths)

        self.option_list.highlighted = None
        self._update_paths(self.display_paths)

        self.option_list.set_options(
            [
                Option(self.highlight_path(path), id=path)
                for path in self.display_paths[:100]
            ]
        )
        with self.option_list.prevent(OptionList.OptionHighlighted):
            self.option_list.highlighted = 0

        self.post_message(PromptSuggestion(""))

    @work(exclusive=True, description="update_paths")
    async def _update_paths(self, paths: list[str]) -> None:
        """Update the paths index.

        Args:
            paths: A list of paths.
        """
        await self.fuzzy_index.update_paths(paths)
        self.call_after_refresh(self.option_list.set_loading, False)
