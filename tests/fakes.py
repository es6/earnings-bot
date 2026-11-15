import asyncio
import itertools
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
import discord
import pytest
from yarl import URL

from earnings_bot import recorder
from earnings_bot.browser import _SETTLED, DesktopChrome, RenderedPage
from earnings_bot.clock import TrustedClock
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.pipeline import DurableDiscordPoster, Poster
from earnings_bot.recorder import Target
from earnings_bot.sources import Sighting, Source
from earnings_bot.summary import (
    Summarizer,
    Summary,
)
from earnings_bot.text import html_to_text
from tests.builders import GUILD, OWNER, PLTR_FEED, TRACKING_RESULTS


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
    for embed_field in embed.fields:
        parts += [embed_field.name or "", embed_field.value or ""]
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
        self,
        url: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
        *,
        ready: Callable[[RenderedPage], bool] | None = None,
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


class FakeTabSocket:
    """Answers DevTools commands the way a tab does, and remembers what it was asked."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.value: Any = None  # what the next in-page fetch answers, when set

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        result: dict[str, Any] = {}
        if message["method"] == "Runtime.evaluate":
            if message["params"]["expression"] == _SETTLED:
                value: Any = ["complete", 1, 10]
            elif self.value is not None:
                value = self.value
            else:
                value = {
                    "url": "https://ir.example.com/results",
                    "status": 200,
                    "title": "Results",
                    "text": "Q3 2026 Earnings Release",
                    "links": [["https://ir.example.com/q3.pdf", "Earnings Release", "card"]],
                    "frames": [],
                }
            result = {"result": {"value": value}}
        # A tab sends events between replies, which the reader must skip
        self.replies += [
            {"method": "Network.requestWillBeSent", "params": {}},
            {"id": 1, "result": result},
        ]

    async def receive_json(self) -> dict[str, Any]:
        return self.replies.pop(0)

    async def close(self) -> None:
        return None


class BrowserFake(RoutedFetcher):
    def __init__(self, responses: Iterable[tuple[int, bytes] | Exception]) -> None:
        super().__init__({})
        self._responses = iter(responses)
        self.browser_calls: list[tuple[str, str, bool]] = []

    async def browser_request(
        self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
    ) -> Response:
        self.browser_calls.append((url, warmup, urgent))
        reply = next(self._responses)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return Response(url, status, {"x-cache": f"cache for {url}"}, body, 0.015)


class FakePoster(Poster):
    """Records each step, and stamps it with the current time the way Discord would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send(self, content: str) -> datetime | None:
        self.calls.append(("send", content))
        return datetime.now(UTC)

    async def edit(self, content: str) -> datetime | None:
        self.calls.append(("edit", content))
        return datetime.now(UTC)

    async def attach(self, name: str, data: bytes) -> datetime | None:
        self.calls.append(("attach", name))
        return datetime.now(UTC)

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        self.calls.append(("summary", f"{text} | {footer}"))
        return datetime.now(UTC)


class FakeSummarizer(Summarizer):
    def __init__(self) -> None:
        self.references = False
        self.calls: list[str] = []  # the release text of each request
        self.contexts: list[dict | None] = []

    async def write(
        self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
    ) -> Summary:
        self.calls.append(release)
        self.contexts.append(context)
        return Summary("UBER $UBER Q3'26 EARNINGS HIGHLIGHTS", ["12%"], 0.0, 1000, 200)


def fetcher() -> RoutedFetcher:
    return RoutedFetcher({".pdf": (200, b"%PDF-1.7")})


class QueuedFetcher(RoutedFetcher):
    """Answers from routes, but takes its turn in the host's queue like the real Fetcher."""

    def __init__(self, routes: dict[str, list[bytes]]) -> None:
        super().__init__({})
        self.pages = routes

    async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Response:
        await self.wait_turn(urlsplit(url).hostname or "")
        for part, bodies in self.pages.items():
            if part in url:
                body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
                return Response(url, 200, {"age": "0"}, body, 0.01)
        return Response(url, 404, {}, b"", 0.01)


class ListSource(Source):
    """Lists the keys in the response body, one per line."""

    name = "list"
    interval = 0.01

    def url(self) -> str:
        return "https://example.com/list"

    def parse(self, response: Response) -> list[Sighting]:
        keys = response.body.decode().split()
        return [Sighting(key, f"Item {key}", "", earnings=True) for key in keys]


class FakeFetcher(Fetcher):
    """Serves canned responses in order, then repeats the last one."""

    def __init__(self, *responses: tuple[int, str], queued: float = 0.0) -> None:
        super().__init__("Test Person test@example.com")
        self.responses = deque(responses)
        self.queued = queued  # how long each request waits for its host's turn
        self.sent = 0

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
        self.sent += 1
        status, body = self.responses[0] if len(self.responses) == 1 else self.responses.popleft()
        if status == 0:
            raise TimeoutError
        return Response(url, status, {"age": "3"}, body.encode(), 0.02, self.queued)


class CompanyFeed(Source):
    """One feed for every company, listing "TICKER:key" items, read for one company."""

    name = "feed"
    interval = 0.01

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    @property
    def share_key(self) -> Hashable:
        return self.name

    def url(self) -> str:
        return "https://example.com/feed"

    def parse(self, response: Response) -> list[Sighting]:
        items = response.body.decode().split()
        return [Sighting(i, i, "", earnings=True) for i in items if i.startswith(self.ticker)]


class FakeStream:
    def __init__(self, message: SimpleNamespace) -> None:
        self.message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def get_final_message(self) -> SimpleNamespace:
        return self.message


def summarizer_answering(stop_reason: str, *texts: str) -> tuple[Summarizer, list[dict[str, Any]]]:
    """A Summarizer whose model answers with these text blocks, and the requests it gets."""
    asked: list[dict[str, Any]] = []
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="redacted_thinking")]
        + [SimpleNamespace(type="text", text=text) for text in texts],
        usage=SimpleNamespace(input_tokens=9000, output_tokens=700),
    )

    def stream(**request: Any) -> FakeStream:
        asked.append(request)
        return FakeStream(message)

    summarizer = Summarizer("not-a-real-key")
    summarizer.client = cast(Any, SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return summarizer, asked


def unknown_message() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(cast(Any, response), {"message": "Unknown Message", "code": 10008})


@dataclass
class FakeMessage:
    id: int
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    edited_at: datetime | None = None
    author: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=42))
    attachments: list[Any] = field(default_factory=list)
    embed: discord.Embed | None = None
    extra_embeds: list[discord.Embed] = field(default_factory=list)
    deleted: bool = False

    @property
    def embeds(self) -> list[discord.Embed]:
        return self.extra_embeds or ([self.embed] if self.embed else [])

    async def edit(
        self,
        *,
        content: str | None = None,
        attachments: list[Any] | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
    ) -> "FakeMessage":
        if self.deleted:
            raise unknown_message()
        if embed is not None:
            self.embed = embed
            self.extra_embeds = []
        if embeds is not None:
            self.extra_embeds = embeds
            self.embed = embeds[0] if embeds else None
        if content is not None:
            self.content = content
        if attachments is not None:
            # As Discord stores them: it drops "%" from a filename ("Nebius%20Q3...pdf" came
            # back as "Nebius20Q3...pdf" on Nov 12)
            self.attachments = [
                SimpleNamespace(filename=a.filename.replace("%", "")) for a in attachments
            ]
        self.edited_at = datetime.now(UTC)
        return self

    async def delete(self) -> None:
        if self.deleted:
            raise unknown_message()
        self.deleted = True


class FakeDiscord:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.ids = itertools.count(1)  # never reused, like Discord's
        self.nonces: dict[str, FakeMessage] = {}
        self.attempts = 0
        self.fail_before_send = False
        self.lose_response = False

    async def send(
        self,
        content: str = "",
        *,
        nonce: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        allowed_mentions: object = None,
    ) -> FakeMessage:
        self.attempts += 1
        if self.fail_before_send:
            self.fail_before_send = False
            raise TimeoutError("request timed out before delivery")
        if nonce is not None and nonce in self.nonces:
            return self.nonces[nonce]
        message = FakeMessage(next(self.ids), content, embed=embed, extra_embeds=embeds or [])
        self.messages[message.id] = message
        if nonce is not None:
            self.nonces[nonce] = message
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("Discord accepted the message but the response was lost")
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if message_id not in self.messages:
            raise unknown_message()
        return self.messages[message_id]

    def delete(self, message_id: int) -> None:
        self.messages.pop(message_id).deleted = True

    async def history(
        self, *, limit: int | None, after: datetime | None = None
    ) -> AsyncIterator[FakeMessage]:
        for message in self.messages.values():
            if after is None or message.created_at > after:
                yield message

    def poster(self, store: DeliveryStore, target: Target) -> DurableDiscordPoster:
        return DurableDiscordPoster(
            cast(discord.abc.Messageable, self), store, target.release_key, 42
        )


class WatchedDiscord(FakeDiscord):
    """Sets `sent` when a message goes out."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()

    async def send(
        self,
        content: str = "",
        *,
        nonce: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        allowed_mentions: object = None,
    ) -> FakeMessage:
        message = await super().send(content, nonce=nonce, embed=embed, embeds=embeds)
        self.sent.set()
        return message


async def call(
    handler: Callable[..., Awaitable[None]],
    *args: str,
    user: int = 1,
    admin: bool = False,
    guild: int = GUILD,
    events: list[str] | None = None,
    app: discord.Client | None = None,
    **options: str | bool | None,
) -> str:
    """Runs a command handler as one server member and returns its one reply, sent at once or
    after deferring. `events` records "defer" and "reply" in order. `app` is the bot's client."""
    replies: list[str] = []
    events = [] if events is None else events

    async def send_message(
        content: str | None = None, *, embed: discord.Embed | None = None, ephemeral: bool = False
    ) -> None:
        assert ephemeral, "calendar replies are only for the member who asked"
        assert "defer" not in events, "Discord refuses a second response"
        events.append("reply")
        replies.append(embed_text(embed) if embed is not None else content or "")

    async def defer(*, ephemeral: bool = False, thinking: bool = False) -> None:
        assert ephemeral and thinking, "only the member who asked sees the bot thinking"
        assert not events, "Discord refuses a second response"
        events.append("defer")

    async def follow_up(
        content: str | None = None, *, embed: discord.Embed | None = None, ephemeral: bool = False
    ) -> None:
        assert ephemeral, "calendar replies are only for the member who asked"
        assert "defer" in events, "a follow-up needs a deferred response"
        events.append("reply")
        replies.append(embed_text(embed) if embed is not None else content or "")

    interaction = SimpleNamespace(
        guild_id=guild,
        user=SimpleNamespace(id=user),
        permissions=discord.Permissions(manage_guild=admin),
        response=SimpleNamespace(send_message=send_message, defer=defer),
        followup=SimpleNamespace(send=follow_up),
        client=app,
    )
    await handler(cast(discord.Interaction, interaction), *args, **options)
    [reply] = replies
    return reply


class ReleaseDay(RoutedFetcher):
    """Palantir's IR site, whose press release list shows its results from `released_at`
    (time.monotonic()) on. Everything else answers 404."""

    def __init__(self) -> None:
        super().__init__({})
        self.released_at = float("inf")

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
        listed: list[dict[str, Any]] = (
            [TRACKING_RESULTS] if time.monotonic() >= self.released_at else []
        )
        self.routes = {PLTR_FEED: (200, json.dumps({"GetPressReleaseListResult": listed}).encode())}
        return await super().request(
            method,
            url,
            headers,
            give_up_after,
            urgent,
            allow_redirects=allow_redirects,
            encoded_url=encoded_url,
        )


def client(owner: int = OWNER, team: list[int] | None = None) -> discord.Client:
    members = [SimpleNamespace(id=member) for member in team or []]
    application = SimpleNamespace(
        owner=SimpleNamespace(id=owner), team=SimpleNamespace(members=members) if team else None
    )
    return cast(discord.Client, SimpleNamespace(application=application))


class ScanFinished(Exception):
    """Raised in place of DateDiscovery's wait for a change, to end one `run()` scan."""


async def scan_finished() -> None:
    raise ScanFinished


class PolledReleaseDay(ReleaseDay):
    """ReleaseDay whose list shows the results once it has been read `polls_before_release`
    times, so a test needs no clock to put the release after a first poll without it.
    """

    def __init__(self, polls_before_release: int = 1) -> None:
        super().__init__()
        self.polls_before_release = polls_before_release
        self.polls = 0

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
        if PLTR_FEED in url:
            self.polls += 1
            if self.polls > self.polls_before_release:
                self.released_at = 0.0
        return await super().request(
            method,
            url,
            headers,
            give_up_after,
            urgent,
            allow_redirects=allow_redirects,
            encoded_url=encoded_url,
        )


def quick_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes a recorder window react within milliseconds: every route polls every 20 ms, and the
    housekeeping tasks that sleep minutes between rounds look at the clock every 50 ms, so a test
    that moves the clock to the end of a window sees it close at once. The real cadence is
    test_release_band's."""
    monkeypatch.setattr(recorder, "pace", lambda at, times, own: (0.02, None))
    monkeypatch.setattr(recorder, "RESOURCES_EVERY", 0.05)
    monkeypatch.setattr(recorder, "CLOCK_RESYNC", 0.05)


class GatedRequest:
    """Hold a response until released; expose request start and cleanup to async tests."""

    def __init__(self, response: Response) -> None:
        self.response = response
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Response:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.finished.set()
        return self.response
