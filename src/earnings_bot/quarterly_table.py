"""Find the earnings-release PDF for one quarter on an official results page."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from earnings_bot.browser import RenderedPage, browser_host
from earnings_bot.periods import title_period

_RELEASE = re.compile(r"\b(?:press|earnings|financial results) release\b", re.IGNORECASE)


@dataclass
class _Cell:
    text: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def label(self) -> str:
        return " ".join("".join(self.text).split())


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.tables: list[list[list[_Cell]]] = []
        self.depth = 0
        self.row: list[_Cell] | None = None
        self.cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.tables.append([])
        elif tag == "tr" and self.depth == 1:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None and self.depth == 1:
            self.cell = _Cell()
            self.row.append(self.cell)
        elif tag == "a" and (href := dict(attrs).get("href")):
            self.links.append(href)
            if self.cell is not None and self.depth == 1:
                self.cell.links.append(href)

    def handle_data(self, data: str) -> None:
        if self.cell is not None and self.depth == 1:
            self.cell.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.depth:
            self.depth -= 1
        elif self.depth == 1:
            if tag in {"td", "th"}:
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.tables[-1].append(self.row)
                self.row = self.cell = None


def quarter_pdf(page: str, body: bytes, year: int, quarter: int) -> str | None:
    parser = _Tables()
    parser.feed(body.decode(errors="replace"))
    host = browser_host(page)

    def file(href: str) -> str | None:
        try:
            url = urljoin(page, href)
            return (
                url
                if (browser_host(url) == host and urlsplit(url).path.lower().endswith(".pdf"))
                else None
            )
        except ValueError:
            return None

    def named_period(url: str) -> tuple[int, int] | None:
        return title_period(re.sub(r"[^\w]+|_", " ", unquote(urlsplit(url).path)))

    # Some issuers include both the fiscal period and the kind of document in its file path.
    for href in parser.links:
        if url := file(href):
            name = re.sub(r"[^\w]+|_", " ", unquote(urlsplit(url).path))
            if _RELEASE.search(name) and title_period(name) == (year, quarter):
                return url

    # A generic Earnings Release row only counts when the page identifies its fiscal year
    # and the same table has unambiguous, explicitly labelled quarter columns.
    parts = urlsplit(page)
    query = parse_qs(parts.query)
    if parts.path.rstrip("/").rsplit("/", 1)[-1] != str(year) and not any(
        query.get(key) == [str(year)] for key in ("year", "tab")
    ):
        return None
    for table in parser.tables:
        column = None
        for row in table:
            labels = [cell.label().upper() for cell in row]
            if labels[1:] == ["Q1", "Q2", "Q3", "Q4"]:
                column = quarter
            elif column is not None and len(row) == 5 and _RELEASE.fullmatch(labels[0]):
                return next(
                    (
                        url
                        for href in row[column].links
                        if (url := file(href)) and named_period(url) in (None, (year, quarter))
                    ),
                    None,
                )
    return None


def rendered_pdf(page: RenderedPage, year: int, quarter: int) -> str | None:
    """Some results pages only reveal their dated press-release links after scripts run."""
    origin = browser_host(page.url).removeprefix("www.")
    for url, label in page.links:
        try:
            host = browser_host(url).removeprefix("www.")
            path = unquote(urlsplit(url).path)
        except ValueError:
            continue
        issuer_asset = host == origin or host.endswith((f".{origin}", ".q4cdn.com"))
        if not issuer_asset or not path.lower().endswith(".pdf"):
            continue
        filename = re.sub(r"[^\w]+|_", " ", path.rsplit("/", 1)[-1])
        if _RELEASE.search(label) and (title_period(label) or title_period(filename)) == (
            year,
            quarter,
        ):
            return url
    return None
