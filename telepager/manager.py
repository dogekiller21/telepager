import abc
import asyncio
from types import NotImplementedType

from .flag import ANY_ORDERING, ANY_QUALITY, FLAG_T
from .settings import PaginatorSettings
from .structs import FetcherIter, Line, Page, PageBook


class Fetcher[T]:
    def __init__(self, incremental_fetching_step: int, iter: FetcherIter[T]) -> None:
        self.lines: list[Line[T]] = []

        self.incremental_fetching_step = incremental_fetching_step
        self._average_page_size: int = 0
        self._iter = iter
        self._iter_is_alive = True
        self._iter_lock = asyncio.Lock()

    async def fetch_more(self):
        if not self._iter_is_alive:
            return

        async with self._iter_lock:
            for _ in range(1, self.incremental_fetching_step):
                try:
                    elem = await self._iter.__anext__()
                except (StopAsyncIteration, RuntimeError):
                    self._iter_is_alive = False
                    return

                self.lines.append(elem)

    async def fetch_all(self):
        while self._iter_is_alive:
            await self.fetch_more()

    @property
    def fetched_pages(self) -> int | None:
        if not self._average_page_size:
            return

        return len(self.lines) // self._average_page_size + (
            1 if len(self.lines) % self._average_page_size != 0 else 0
        )

    def all_fetched(self) -> bool:
        return not self._iter_is_alive


def filter_lines_by_quality[T](
    lines: list[Line[T]], asked_quality: int, quality_t: FLAG_T | None = None
) -> list[Line[T]]:
    if asked_quality == ANY_QUALITY or quality_t is None:
        return lines
    else:
        result: list[Line[T]] = []

        for line in lines:
            try:
                quality_of_line = quality_t(line.quality)
                asked_quality = quality_t(asked_quality)
                if asked_quality in quality_of_line:
                    result.append(line)
            except ValueError:  # enum's boundary=STRICT
                continue

        return result


class ABCPageBuilder[T](abc.ABC):
    @abc.abstractmethod
    async def build_page(self, lines: list[Line[T]]) -> Page | None: ...

    @abc.abstractmethod
    async def empty_page(self) -> Page: ...

    """
    Used by paginator when PageBook for _some_ quality is empty.
    """

    async def order_by(
        self, lines: list[Line[T]], asked_ordering: int
    ) -> NotImplementedType | list[Line[T]]:
        return NotImplemented


class NaivePageBuilder[T](ABCPageBuilder[T]):
    def __init__(self, base_text: str):
        self.base_text = base_text

    async def empty_page(self) -> Page:
        return Page(self.base_text)

    async def build_page(self, lines: list[Line[T]]) -> Page | None:
        if not lines:
            return
        text = self.base_text + "\n"
        text += "\n".join([line.text for line in lines])

        return Page(text)


class FormattingPageBuilder[T](ABCPageBuilder[T]):
    def __init__(
        self,
        base_text: str,
        formatting_template_name: str,
        empty_page_format_text: str | None = None,
    ) -> None:
        self.base_text = base_text
        self.formatting_template_name = formatting_template_name

        self.empty_page_format_text = empty_page_format_text

    async def empty_page(self) -> Page:
        if self.empty_page_format_text is None:
            text = self.base_text.format(**{self.formatting_template_name: str()})
        else:
            text = self.base_text.format(
                **{self.formatting_template_name: self.empty_page_format_text}
            )

        return Page(text)

    async def build_page(self, lines: list[Line[T]]) -> Page | None:
        if not lines:
            return
        text = "\n".join([line.text for line in lines])

        return Page(self.base_text.format(**{self.formatting_template_name: text}))


class RecordManager[T]:
    def __init__(self, fetcher: Fetcher[T], settings: PaginatorSettings[T]):
        self.fetcher = fetcher
        self.settings = settings

    async def get_empty_page(self, builder: ABCPageBuilder[T]) -> Page:
        return await builder.empty_page()

    async def build_page_book(
        self,
        asked_quality: int,
        asked_ordering: int,
        builder: ABCPageBuilder[T],
        quality_t: FLAG_T | None = None,
    ) -> PageBook:
        filtered_lines = filter_lines_by_quality(
            self.fetcher.lines, asked_quality, quality_t
        )
        if asked_ordering != ANY_ORDERING:
            ordered_lines = await builder.order_by(filtered_lines, asked_ordering)
            if ordered_lines is not NotImplemented:
                filtered_lines = ordered_lines

        page_book = [
            await builder.build_page(selected_lines)
            for selected_lines in self.settings.page_sizer_factory()(  # type: ignore
                filtered_lines
            )
        ]
        page_book = [page for page in page_book if page]
        if len(page_book):
            self.fetcher._average_page_size = len(filtered_lines) // len(page_book)  # type: ignore

        return page_book
