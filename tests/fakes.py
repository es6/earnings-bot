import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import unquote, urljoin

import aiohttp
import discord
from yarl import URL

from earnings_bot.browser import DesktopChrome, RenderedPage
from earnings_bot.clock import TrustedClock
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.text import html_to_text


class PinnedClock(TrustedClock):
    """Advance normally from a fixed daytime instant, independent of the CI host's date."""

    def __init__(self) -> None:
        super().__init__(servers=())
        self.started = time.monotonic()
        self.anchor = datetime(2026, 11, 10, 17, tzinfo=UTC)

    def now(self) -> datetime:
        return self.anchor + timedelta(seconds=time.monotonic() - self.started)


class _Anchors(HTMLParser):
    _BLOCKS = frozenset({"div", "li", "tr", "article", "section"})

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.cards: list[str] = []
        self.frames: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []
        # Each open block's text so far, and the links inside it with how many blocks up
        # from them it is, as Chrome climbs at most four levels for a card of 400 characters
        self._blocks: list[tuple[list[str], list[tuple[int, int]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in self._BLOCKS:
            self._blocks.append(([], []))
        if tag == "a" and values.get("href"):
            self._href, self._text = values["href"], []
        elif tag == "iframe" and values.get("src"):
            self.frames.append(values["src"] or "")

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
        if self._blocks:
            self._blocks[-1][0].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            if text:
                self.links.append((self._href, text))
                self.cards.append(text)
                if self._blocks:
                    self._blocks[-1][1].append((len(self.links) - 1, 0))
            self._href = None
        elif tag in self._BLOCKS and self._blocks:
            parts, waiting = self._blocks.pop()
            card = " ".join(" ".join(parts).split())
            climbing = []
            for index, up in waiting:
                if up < 4 and len(card) <= 400:
                    self.cards[index] = card
                    climbing.append((index, up + 1))
            if self._blocks:
                self._blocks[-1][0].extend(parts)
                self._blocks[-1][1].extend(climbing)


def rendered(url: str, status: int, html: str) -> RenderedPage:
    """What Chrome would show for a page: its text, and its links made absolute."""
    anchors = _Anchors()
    anchors.feed(html)
    title = html.split("<title>", 1)[1].split("</title>", 1)[0] if "<title>" in html else ""
    return RenderedPage(
        url=url,
        status=status,
        title=title,
        text=html_to_text(html),
        links=[(urljoin(url, href), text) for href, text in anchors.links],
        frames=[urljoin(url, src) for src in anchors.frames],
        cards=anchors.cards,
    )


def embed_text(embed: discord.Embed | None) -> str:
    """An embed's title, description and fields as plain lines, for asserting on."""
    if embed is None:
        return ""
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts += [field.name or "", field.value or ""]
    return "\n".join(parts)


class RoutedChrome:
    """Desktop Chrome in a test, answering from its fetcher's routes. `opened` counts how often
    it was started."""

    def __init__(self, fetcher: "RoutedFetcher") -> None:
        self.fetcher = fetcher

    async def request(
        self,
        url: str,
        warmup: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
    ) -> Response:
        self.fetcher.chrome_calls.append(url)
        return await self.fetcher.request("GET", url, give_up_after=give_up_after)

    async def render(
        self, url: str, give_up_after: float, wait_turn: Callable[[str], Awaitable[None]]
    ) -> RenderedPage:
        self.fetcher.chrome_calls.append(url)
        reply = await self.fetcher.request("GET", url, give_up_after=give_up_after)
        return rendered(url, reply.status, reply.body.decode(errors="replace"))

    async def inspect_json(self, url: str) -> list[Any]:
        return []


class RoutedFetcher(Fetcher):
    """Answers each request from the first route whose key appears in the URL."""

    def __init__(
        self, routes: dict[str, tuple[int, bytes]], headers: dict[str, str] | None = None
    ) -> None:
        super().__init__("Test Person test@example.com")
        self.routes = routes
        self.headers = headers or {}
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []
        self.no_redirect: list[str] = []
        self.urgent: list[str] = []  # URLs asked for ahead of the host's polls
        self.chrome_calls: list[str] = []  # URLs read through the research browser
        self.chrome_opened = 0

    @asynccontextmanager
    async def research_browser(self) -> AsyncIterator[DesktopChrome]:
        """Never real Chrome in a test: one that reads the same routes."""
        self.chrome_opened += 1
        yield cast(DesktopChrome, RoutedChrome(self))

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
        encoded_url: bool = False,
    ) -> Response:
        self.requests.append((method, url, headers))
        if not allow_redirects:
            self.no_redirect.append(url)
        if urgent:
            self.urgent.append(url)
        route = unquote(url) if encoded_url else url
        for part, (status, body) in self.routes.items():
            if part in route:
                return Response(url, status, self.headers, body, 0.01)
        return Response(url, 404, {}, b"", 0.01)


# Answers one request with (status, headers, body). It can sleep first, to play a slow server.
Answer = Callable[[str, str], Awaitable[tuple[int, dict[str, str], bytes]]]


def sec_filing_detail(primary: str, exhibit: str) -> bytes:
    return f'''
<table summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>Current report</td><td><a href="/ix?doc={primary}">{primary}</a>
    <span>iXBRL</span></td><td>8-K</td><td>90000</td></tr>
<tr><td>2</td><td></td><td><a href="{exhibit}">{exhibit}</a></td><td>EX-99.1</td>
    <td>50000</td></tr>
</table>'''.encode()


class FakeSession:
    """Stands in for aiohttp's ClientSession under a real Fetcher, so a test goes through the
    Fetcher's pacing and priorities but never touches the network. It returns fixture responses
    directly, without redirecting or retrying. `sent` records (time.monotonic(), method, url)."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.sent: list[tuple[float, str, str]] = []

    def request(
        self,
        method: str,
        url: str | URL,
        *,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        allow_redirects: bool = True,
        middlewares: tuple[aiohttp.ClientMiddlewareType, ...] = (),
    ) -> "_FakeReply":
        return _FakeReply(self, method, str(URL(url)), timeout.total if timeout else None)

    async def close(self) -> None:
        pass


class _FakeReply:
    def __init__(self, session: FakeSession, method: str, url: str, limit: float | None) -> None:
        self.session, self.method, self.url, self.limit = session, method, url, limit
        self.status = 0
        self.headers: dict[str, str] = {}
        self._body = b""

    async def __aenter__(self) -> Self:
        self.session.sent.append((time.monotonic(), self.method, self.url))
        # Raises TimeoutError past the limit, like aiohttp
        async with asyncio.timeout(self.limit):
            self.status, self.headers, self._body = await self.session.answer(self.method, self.url)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def read(self) -> bytes:
        return self._body


class SessionFetcher(Fetcher):
    """A real Fetcher, pacing and all, whose requests go to a FakeSession."""

    def __init__(self, session: FakeSession, timeout: float = 10.0) -> None:
        super().__init__("Test Person test@example.com", timeout)
        self.fake = session

    async def __aenter__(self) -> Self:
        self._session = cast(aiohttp.ClientSession, self.fake)
        return self


def simple_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with these lines of text, built by hand so tests need no PDF writer."""
    shown = " T* ".join(
        "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
        for line in lines
    )
    stream = f"BT /F1 10 Tf 12 TL 40 760 Td {shown} ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    data += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return data
