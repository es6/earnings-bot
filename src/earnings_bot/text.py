"""Getting the full text of a release, wherever it was first seen, for the summary."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp

from earnings_bot.fetch import Fetcher
from earnings_bot.sources import (
    EdgarFilings,
    GuessedFile,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
)

# Shorter than any real earnings release, so error pages and teasers don't count
MIN_RELEASE_CHARS = 2000

# Tags that start a new line
_BLOCK = re.compile(
    r"p|div|br|li|ul|ol|table|tr|section|article|header|footer|h[1-6]|blockquote|pre|hr"
)
_SKIPPED = {"script", "style", "noscript", "template", "svg", "head"}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0
        self._in_cell = 0

    def _block(self) -> None:
        # Business Wire wraps every table cell's contents in a <p>, which mustn't split the row
        self.parts.append(" " if self._in_cell else "\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skipping += 1
        elif tag in ("td", "th"):
            self.parts.append(" | ")
            self._in_cell += 1
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in ("td", "th"):
            self._in_cell = max(0, self._in_cell - 1)
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            # Line breaks in the HTML source are just spaces; only tags start new lines
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """Plain text with one line per paragraph and table row. Cells are joined with " | " so
    financial tables keep their columns."""
    parser = _TextParser()
    parser.feed(html)
    lines = []
    for line in "".join(parser.parts).splitlines():
        line = re.sub(r"\s+", " ", line).strip().strip("|").strip()
        line = re.sub(r"(?:\s*\|\s*){2,}", " | ", line)  # empty cells
        if line:
            lines.append(line)
    return "\n".join(lines)


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
    named = [f for f in pages if re.search(r"ex-?99|99-?1|press|release|earn", f["name"], re.I)]

    def size(f: dict[str, str]) -> int:
        return int(f["size"]) if str(f.get("size", "")).isdigit() else 0

    best = max(named or pages, key=size, default=None)
    return best["name"] if best else None


async def _get_text(fetcher: Fetcher, url: str) -> str | None:
    response = await fetcher.request("GET", url)
    if response.status != 200:
        return None
    return response.body.decode(errors="replace")


async def _q4_body(fetcher: Fetcher, source: Q4PressReleases, release_id: str) -> str | None:
    body = await _get_text(fetcher, source.url(with_bodies=True))
    if body is None:
        return None
    for release in json.loads(body)["GetPressReleaseListResult"]:
        if str(release["PressReleaseId"]) == release_id:
            return release.get("Body") or None
    return None


async def _edgar_exhibit(fetcher: Fetcher, sighting: Sighting) -> str | None:
    folder, primary = sighting.url.rsplit("/", 1)
    listing = await _get_text(fetcher, f"{folder}/index.json")
    if listing is None:
        return None
    exhibit = pick_exhibit(json.loads(listing)["directory"]["item"], primary)
    return await _get_text(fetcher, f"{folder}/{exhibit}") if exhibit else None


async def release_text(fetcher: Fetcher, source: Source, sighting: Sighting) -> str | None:
    """The release's text from wherever this sighting points, or None if this source can't
    provide it."""
    try:
        if isinstance(source, Q4PressReleases):
            html = await _q4_body(fetcher, source, sighting.key)
        elif isinstance(source, EdgarFilings):
            html = await _edgar_exhibit(fetcher, sighting) if sighting.earnings else None
        elif isinstance(source, GuessedFile | RssFeed):
            host = urlsplit(sighting.url).hostname or ""
            # Business Wire's article pages drop automated requests, and PDFs need no reading
            # because a release's HTML version always comes too
            if host.endswith("businesswire.com") or sighting.url.lower().endswith(".pdf"):
                return None
            html = await _get_text(fetcher, sighting.url)
        else:
            return None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    text = html_to_text(html) if html else ""
    return text if len(text) >= MIN_RELEASE_CHARS else None
