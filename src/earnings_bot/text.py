"""Getting the full text of a release, wherever it was first seen, for the summary."""

from __future__ import annotations

import asyncio
import io
import json
import re
from bisect import bisect_left
from html.parser import HTMLParser
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

import aiohttp
from pypdf import PdfReader

from earnings_bot.fetch import Fetcher
from earnings_bot.sources import (
    BROADCOM_RELEASE,
    BroadcomNewsroom,
    EdgarFilings,
    GuessedFile,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
    bust_cache,
)

if TYPE_CHECKING:
    from earnings_bot.source_document import SourceDocument

# Shorter than any real earnings release, so error pages and teasers don't count
MIN_RELEASE_CHARS = 2000
# Newswires whose main article hosts block the HTTP client.
BLOCKED_ARTICLES = ("businesswire.com", "globenewswire.com")

# Tags that start a new line
_BLOCK = re.compile(
    r"p|div|br|li|ul|ol|table|tr|section|article|header|footer|h[1-6]|blockquote|pre|hr"
)
_SKIPPED = {"script", "style", "noscript", "template", "svg", "head"}


# A cell that only dresses up the number beside it, which EDGAR tables give a column of its
# own: a currency or percent sign, a negative number's parenthesis, or a footnote mark
_DECORATION = re.compile(r"[$€£¥₹%()]+|[A-Z]{1,3}\$|\([a-z]{1,2}\)|[*†‡]+")
_MAX_SPAN = 1000  # browsers cap colspan and rowspan at about this too


def _clean(text: str) -> str:
    return " ".join(text.split())  # the same whitespace as re's \s, and faster


def _span(value: str | None) -> int:
    if not value or value == "1":
        return 1
    digits = re.match(r"\s*([0-9]{1,6})", value)
    return min(max(int(digits[1]), 1), _MAX_SPAN) if digits else 1


class _Cell(NamedTuple):
    text: str
    span: int  # colspan
    rows: int  # rowspan
    header: bool = False
    scope: str = ""


class _Placed(NamedTuple):
    column: int
    span: int
    text: str


class _Table:
    """One <table>'s rows, kept until it ends so its columns can be lined up."""

    def __init__(self) -> None:
        # Rows of cells, and lines of any text outside the cells, in page order
        self.items: list[list[_Cell] | str] = []
        self.loose: list[str] = []
        self.cell: list[str] | None = None
        self._row: list[_Cell] | None = None
        self._spans = (1, 1)
        self._header = False
        self._scope = ""

    def start_row(self) -> None:
        self.end_row()
        self._flush_loose()
        self._row = []

    def end_row(self) -> None:
        self.end_cell()
        if self._row is not None:
            self.items.append(self._row)
            self._row = None

    def start_cell(
        self, colspan: int, rowspan: int, *, header: bool = False, scope: str = ""
    ) -> None:
        self.end_cell()
        if self._row is None:
            self.start_row()
        self.cell = []
        self._spans = (colspan, rowspan)
        self._header, self._scope = header, scope

    def end_cell(self) -> None:
        if self.cell is not None and self._row is not None:
            self._row.append(
                _Cell(_clean("".join(self.cell)), *self._spans, self._header, self._scope)
            )
        self.cell = None

    def close(self) -> list[str]:
        self.end_row()
        self._flush_loose()
        return _table_lines(self.items)

    def _flush_loose(self) -> None:
        text = "".join(self.loose)
        self.loose.clear()
        self.items.extend(line for raw in text.split("\n") if (line := _clean(raw)))


def _place(items: list[list[_Cell] | str]) -> list[list[_Placed] | str]:
    """Each cell with the column it starts in. A cell from a row above that reaches down with
    rowspan takes up its columns again as an empty cell."""
    placed: list[list[_Placed] | str] = []
    above: list[tuple[int, int, int]] = []  # (column, span, rows still to cover)
    for item in items:
        if isinstance(item, str):
            placed.append(item)
            continue
        taken = sorted((column, span) for column, span, _ in above)
        row: list[_Placed] = []
        reaching: list[tuple[int, int, int]] = []
        column = k = 0
        for cell in item:
            while k < len(taken) and taken[k][0] <= column:
                if column < sum(taken[k]):
                    row.append(_Placed(*taken[k], ""))
                    column = sum(taken[k])
                k += 1
            row.append(_Placed(column, cell.span, cell.text))
            if cell.rows > 1:
                reaching.append((column, cell.span, cell.rows - 1))
            column += cell.span
        above = [(c, s, left - 1) for c, s, left in above if left > 1] + reaching
        placed.append(row)
    return placed


def _table_lines(items: list[list[_Cell] | str]) -> list[str]:
    """One line per row, cells joined with " | ". Spacer columns, empty in every row, are left
    out. An empty cell elsewhere stays as a blank between the bars, so a header like
    "Adjusted" stays over the column it names."""
    rows = [
        row
        for row in _place(items)
        if isinstance(row, str) or any(cell.text for cell in row)  # skip all-blank rows
    ]
    cells = [cell for row in rows if not isinstance(row, str) for cell in row]
    # A cell is a column's own when no narrower cell overlaps it; wider ones are group
    # headings like "Three Months Ended". EDGAR's empty spacer cells count, so they keep a
    # heading that spans "$ | 1,000" from claiming the "$" column.
    narrowest: dict[int, int] = {}
    for cell in cells:
        for column in range(cell.column, cell.column + cell.span):
            narrowest[column] = min(narrowest.get(column, cell.span), cell.span)
    columns = sorted(
        {
            cell.column
            for cell in cells
            if cell.text
            and not _DECORATION.fullmatch(cell.text)
            and all(narrowest[c] == cell.span for c in range(cell.column, cell.column + cell.span))
        }
    )
    lines = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
            continue
        fields: list[str] = []
        for cell in row:
            if cell.text:
                fields.append(cell.text)
            else:
                # A blank for each column this empty cell covers. A "$" column's blanks
                # aren't needed, so EDGAR's "Net sales | $ | 1,000" and "Membership | 50"
                # read as before.
                first = bisect_left(columns, cell.column)
                last = bisect_left(columns, cell.column + cell.span)
                fields.extend("" for _ in columns[first:last])
        while fields and not fields[-1]:
            fields.pop()
        lines.append(" | ".join(fields))
    return lines


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0
        self._tables: list[_Table] = []  # open tables, innermost last

    def _sink(self) -> list[str]:
        if not self._tables:
            return self.parts
        table = self._tables[-1]
        return table.cell if table.cell is not None else table.loose

    def _in_cell(self) -> bool:
        return bool(self._tables) and self._tables[-1].cell is not None

    def _block(self) -> None:
        # Business Wire wraps every table cell's contents in a <p>, which mustn't split the row
        self._sink().append(" " if self._in_cell() else "\n")

    def _table(self) -> _Table:
        if not self._tables:  # a row or cell without its <table>
            self._tables.append(_Table())
        return self._tables[-1]

    def _end_table(self) -> None:
        lines = self._tables.pop().close()
        if self._in_cell():  # a table inside another table's cell stays in that cell
            self._sink().append(" " + " ".join(lines) + " ")
        else:
            self._sink().append("\n" + "\n".join(lines) + "\n")

    def finish(self) -> None:
        while self._tables:
            self._end_table()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skipping += 1
        elif self._skipping:
            return
        elif tag == "table":
            self._tables.append(_Table())
        elif tag == "tr":
            self._table().start_row()
        elif tag in ("td", "th"):
            spans = dict(attrs)
            self._table().start_cell(
                _span(spans.get("colspan")),
                _span(spans.get("rowspan")),
                header=tag == "th",
                scope=spans.get("scope") or "",
            )
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif self._skipping:
            return
        elif tag == "table" and self._tables:
            self._end_table()
        elif tag == "tr" and self._tables:
            self._tables[-1].end_row()
        elif tag in ("td", "th") and self._tables:
            self._tables[-1].end_cell()
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            # Line breaks in the HTML source are just spaces; only tags start new lines
            self._sink().append(re.sub(r"\s+", " ", data))


def verified_report(text: str | None) -> bool:
    return bool(
        text
        and len(text) >= MIN_RELEASE_CHARS
        and re.search(r"\b(?:revenues?|net sales)\b", text, re.IGNORECASE)
        and re.search(
            r"\b(?:net (?:income|loss|earnings)|per (?:diluted )?share|EPS)\b", text, re.IGNORECASE
        )
    )


# A results release runs 10 to 30 pages. More than this is a filing or a deck, not a release.
MAX_PDF_PAGES = 60


def pdf_text(data: bytes) -> str:
    """A release PDF's text, or "" when it can't be read. Takes about 0.3 s for a 20-page
    release, so call it in a thread."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            return ""
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        # pypdf raises its own errors and plain ones (KeyError, ValueError, RecursionError) on a
        # damaged or unusual file. No text is the answer to all of them.
        return ""
    # A line wrapped at a hyphen ("prior-" / "year quarter") reads "prior-year", as the HTML does
    return re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "-", text)


def html_to_text(html: str) -> str:
    """Plain text with one line per paragraph and table row. Cells are joined with " | " so
    financial tables keep their columns."""
    parser = _TextParser()
    parser.feed(html)
    parser.finish()
    return "\n".join(line for raw in "".join(parser.parts).splitlines() if (line := _clean(raw)))


def pick_exhibit(files: list[dict[str, str]], primary: str) -> str | None:
    """The press release among a filing's files: the exhibit named like one, else the biggest
    HTML document that isn't the 8-K itself or an XBRL viewer page."""
    pages = [
        f
        for f in files
        if f["name"].lower().endswith((".htm", ".html"))
        and f["name"] != primary
        and "index" not in f["name"]
        and not re.fullmatch(r"R\d+\.htm", f["name"])
    ]
    named = [
        f for f in pages if re.search(r"ex-?99|99-?1|press|release|earn", f["name"], re.IGNORECASE)
    ]

    def size(f: dict[str, str]) -> int:
        return int(f["size"]) if str(f.get("size", "")).isdigit() else 0

    best = max(named or pages, key=size, default=None)
    return best["name"] if best else None


async def _get_text(fetcher: Fetcher, url: str) -> str | None:
    # The release just turned up, so this goes ahead of the host's polls
    response = await fetcher.request("GET", url, urgent=True)
    if response.status != 200:
        return None
    return response.body.decode(errors="replace")


async def _broadcom_body(fetcher: Fetcher, sighting: Sighting) -> str | None:
    # An ID Broadcom doesn't know yet still answers 200, with a stub that has no "body". This is
    # busted like the listing, in case Cloudflare kept that stub.
    url = bust_cache(BROADCOM_RELEASE.format(path=sighting.details["path"]))
    page = await _get_text(fetcher, url)
    release = json.loads(page) if page else None
    body = release.get("body") if isinstance(release, dict) else None
    return body if isinstance(body, str) and body else None


async def _q4_document(
    fetcher: Fetcher, source: Q4PressReleases, release_id: str
) -> SourceDocument | None:
    # The release that just turned up is nearly always the newest, so ask for it alone first:
    # Uber's list of five came to 459 KB and 0.76 s, its newest alone to 10 KB and 0.2 s. Each
    # length is its own copy on Q4's CDN, so if that copy is a few seconds too old to have the
    # release, the normal list is a second chance.
    for page_size in dict.fromkeys((1, source.page_size)):
        try:
            page = await _get_text(fetcher, source.bodies_url(page_size))
            releases = json.loads(page)["GetPressReleaseListResult"] if page else []
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(releases, list):
            continue
        for release in releases:
            if isinstance(release, dict) and str(release.get("PressReleaseId")) == release_id:
                body = release.get("Body")
                if isinstance(body, str) and (document := await _usable_document(body)):
                    return document
    return None


class _FilingDocuments(HTMLParser):
    """Read the exhibit rows in an EDGAR Filing Detail page's document table."""

    def __init__(self) -> None:
        super().__init__()
        self.exhibits: list[dict[str, str]] = []
        self._in_table = False
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = dict(attrs).get("summary") == "Document Format Files"
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag == "td":
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._in_table = False
        elif tag == "td" and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._in_table and len(self._row) == 5:
            _, description, document, form, size = self._row
            name = document.split(maxsplit=1)[0] if document else ""
            # Use the SEC's exhibit type. The 8-K itself is a separate row in the same table.
            if re.fullmatch(r"EX-99(?:\.\d+)?", form) and re.fullmatch(
                r"[\w.-]+\.html?", name, re.IGNORECASE
            ):
                self.exhibits.append(
                    {
                        "name": name,
                        "size": size.replace(",", ""),
                        "description": description,
                        "form": form,
                    }
                )


def filing_exhibits(html: str) -> list[dict[str, str]]:
    parser = _FilingDocuments()
    parser.feed(html)
    return parser.exhibits


async def _edgar_exhibit(fetcher: Fetcher, sighting: Sighting) -> str | None:
    folder, primary = sighting.url.rsplit("/", 1)
    if urlsplit(sighting.url).path.lower().endswith(("-index.htm", "-index.html")):
        # The latest-filings link is an index, so it doesn't give us the primary filename.
        detail = await _get_text(fetcher, sighting.url)
        if detail is None:
            return None
        exhibits = filing_exhibits(detail)
        results = [
            row
            for row in exhibits
            if re.search(r"\b(?:earnings|results)\b", row["description"], re.IGNORECASE)
        ]
        exhibit_991 = [row for row in exhibits if row["form"] == "EX-99.1"]
        exhibit = pick_exhibit(results or exhibit_991 or exhibits, primary)
    else:
        listing = await _get_text(fetcher, f"{folder}/index.json")
        if listing is None:
            return None
        exhibit = pick_exhibit(json.loads(listing)["directory"]["item"], primary)
    return await _get_text(fetcher, f"{folder}/{exhibit}") if exhibit else None


def globe_article_url(url: str) -> str | None:
    parts = urlsplit(url)
    if (
        parts.scheme == "https"
        and parts.netloc == "www.globenewswire.com"
        and parts.path.startswith("/news-release/")
        and parts.path.endswith(".html")
    ):
        return parts._replace(netloc="rss.globenewswire.com").geturl()
    return None


async def release_text(fetcher: Fetcher, source: Source, sighting: Sighting) -> str | None:
    """Compatibility reader for classifiers/audits that need only the release text."""
    document = await release_document(fetcher, source, sighting)
    return document.text if document else None


async def release_document(
    fetcher: Fetcher, source: Source, sighting: Sighting
) -> SourceDocument | None:
    """The release's text from wherever this sighting points, or None if this source can't
    provide it."""
    from earnings_bot.source_document import document_from_text

    try:
        if sighting.body is not None:
            # The poll already downloaded the page. A PDF is read too: the pipeline asks for
            # one only when no page has delivered the text. Brookfield's HTML came 4 minutes
            # after its PDF.
            if sighting.body.startswith(b"%PDF-"):
                text = await asyncio.to_thread(pdf_text, sighting.body)
                return (
                    await asyncio.to_thread(document_from_text, text)
                    if verified_report(text)
                    else None
                )
            html = sighting.body.decode(errors="replace")
            if sighting.details.get("verified_exhibit") == "true":
                # Already parsed by the classifier; do not reinterpret text as HTML.
                return await asyncio.to_thread(document_from_text, html)
        elif isinstance(source, Q4PressReleases):
            return await _q4_document(fetcher, source, sighting.key)
        elif isinstance(source, BroadcomNewsroom):
            html = await _broadcom_body(fetcher, sighting) if sighting.earnings else None
        elif isinstance(source, EdgarFilings):
            html = await _edgar_exhibit(fetcher, sighting) if sighting.earnings else None
        elif isinstance(source, GuessedFile | RssFeed):
            url = urlsplit(sighting.url)
            if isinstance(source, RssFeed) and (article := globe_article_url(sighting.url)):
                # The publisher's feed hostname serves the same article. Keep the original
                # URL for Discord and skip the main hostname's 403 on the body request.
                html = await _get_text(fetcher, article)
            elif (url.hostname or "").endswith(BLOCKED_ARTICLES) or url.path.lower().endswith(
                ".pdf"
            ):
                return None
            else:
                html = await _get_text(fetcher, sighting.url)
        else:
            return None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    return await _usable_document(html)


async def _usable_document(html: str | None) -> SourceDocument | None:
    # Parsing an 8-K exhibit takes about 30 ms of pure Python. In a thread, the event loop gets
    # the interpreter back every few milliseconds, so other windows' polls don't wait on it.
    from earnings_bot.source_document import document_from_html

    if not html:
        return None
    try:
        document = await asyncio.to_thread(document_from_html, html)
    except ValueError:
        return None
    return document if len(document.text) >= MIN_RELEASE_CHARS else None
