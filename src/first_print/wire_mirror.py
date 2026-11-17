"""Recover an already public Business Wire release from its syndication copy.

This is body recovery after a wire sighting, not a discovery or early-publication route.
Every candidate must identify the exact wire ID and headline inside its article.
"""

import asyncio
import re
from dataclasses import replace
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from first_print.fetch import Fetcher
from first_print.http_policy import retry_after
from first_print.source_document import SourceDocument, document_from_html
from first_print.sources import Sighting, published_time

HOST = "https://markets.financialcontent.com/stocks/article/"
_WIRE = re.compile(r"/news/home/(\d{14})/en(?:/([^/]*))?/?")
_TITLE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.DOTALL)
_LINK = re.compile(r"<a\b[^>]*href=[\"\']([^\"\']+)[\"\']", re.IGNORECASE)


class _Article(HTMLParser):
    """Keep the complete outer story, including nested article sections and tables."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.html = html
        self.offsets = [0]
        for line in html.split("\n"):
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.start: int | None = None
        self.end: int | None = None
        self.depth = 0
        self.feed(html)

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "article" or self.end is not None:
            return
        if self.depth:
            self.depth += 1
        elif "newsrpm-article" in (dict(attrs).get("class") or "").split():
            self.start = self._offset()
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.end = self.html.find(">", self._offset()) + 1

    @property
    def body(self) -> str | None:
        return self.html[self.start : self.end] if self.start is not None and self.end else None


def identity(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.netloc not in {
        "www.businesswire.com",
        "businesswire.com",
    }:
        return None
    match = _WIRE.fullmatch(parts.path)
    return match[1] if match else None


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", unescape(re.sub(r"<[^>]+>", "", text)).casefold()))


def candidate(item: Sighting) -> str | None:
    key = identity(item.url)
    if key is None or not item.earnings:
        return None
    try:
        at = published_time(item.published)
        date = (
            at.astimezone(ZoneInfo("America/New_York"))
            if at
            else datetime.strptime(key[:8], "%Y%m%d")
        )
        day = f"{date.year}-{date.month}-{date.day}"
    except ValueError:
        return None
    # The headline supplies a candidate even when the wire feed omits its slug. No IDs are
    # guessed: a wrong slug simply fails, and the returned page must link the known wire ID.
    slug = re.sub(r"[^a-z0-9]+", "-", unquote(item.title).lower()).strip("-")
    return f"{HOST}bizwire-{day}-{slug}?Language=english" if slug else None


def candidates(item: Sighting) -> tuple[str, ...]:
    url = candidate(item)
    if not url:
        return ()
    # Both spellings occur in saved public mirror URLs. At most one fallback, only on gone
    # pages; never search for or increment wire IDs.
    at = published_time(item.published)
    key = identity(item.url)
    assert key is not None
    date = (
        at.astimezone(ZoneInfo("America/New_York")) if at else datetime.strptime(key[:8], "%Y%m%d")
    )
    plain = f"{date.year}-{date.month}-{date.day}"
    padded = date.strftime("%Y-%m-%d")
    return tuple(dict.fromkeys((url, url.replace(f"bizwire-{plain}-", f"bizwire-{padded}-", 1))))


async def recover(fetcher: Fetcher, item: Sighting) -> SourceDocument | None:
    pause = fetcher.route_pause(HOST)
    if pause.left():
        return None
    for url in candidates(item):
        response = await fetcher.request("GET", url, urgent=True, give_up_after=10)
        headers = {key.lower(): value for key, value in response.headers.items()}
        wait = headers.get("retry-after", "")
        if response.status == 403:
            pause.refused()
            if wait:
                pause.hold(retry_after(wait))
            return None
        if response.status in {429, 503} or wait:
            pause.hold(retry_after(wait))
            return None
        if response.status in {404, 410}:
            continue
        if response.status != 200:
            return None
        pause.answered()
        return await asyncio.to_thread(_document, response.body.decode(errors="replace"), item, url)
    return None


def _document(html: str, item: Sighting, url: str) -> SourceDocument | None:
    from first_print.text import MIN_RELEASE_CHARS, verified_report

    body = _Article(html).body
    if not body:
        return None
    title = _TITLE.search(body)
    if not title or _words(title[1]) != _words(item.title):
        return None
    links = [unescape(link) for link in _LINK.findall(body)]
    if not any(identity(link) == identity(item.url) for link in links):
        return None
    document = document_from_html(body)
    if len(document.text) < MIN_RELEASE_CHARS or not verified_report(document.text):
        return None
    return replace(document, retrieval_url=url, canonical_url=item.url)
