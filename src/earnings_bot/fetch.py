"""One HTTP client for every source: a shared connection pool, honest User-Agents, and pacing
per host (per site, for SEC) so that a busy evening can't hammer any one site."""

import asyncio
import heapq
import itertools
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from earnings_bot.browser import (
    MAX_BROWSER_BYTES,
    BrowserError,
    CacheMode,
    DesktopChrome,
    RenderedPage,
)

# Research during an open release window runs in the background (see Fetcher.background): it
# waits behind every poll, never asks a host a window is polling, and asks SEC, which every
# window shares, at most once every BACKGROUND_SEC_GAP seconds
BACKGROUND_SEC_GAP = 2.0
# A host that answered a window's poll this recently is being watched
LIVE_SECONDS = 30.0


@dataclass(frozen=True)
class _Background:
    chrome: bool | Callable[[], bool]
    active: bool | Callable[[], bool]
    paused: bool | Callable[[], bool]
    deferred: asyncio.Event = field(default_factory=asyncio.Event)


def _flag(value: bool | Callable[[], bool]) -> bool:
    return value() if callable(value) else value


def _background() -> _Background | None:
    mode = _BACKGROUND.get()
    if mode is None:
        return None
    if mode.deferred.is_set():
        raise HostBusy("This research check yielded to monitoring and must retry later")
    if not _flag(mode.active):
        return None
    if _flag(mode.paused):
        mode.deferred.set()
        raise HostBusy("Research waits for the release's priority interval to finish")
    return mode


_BACKGROUND: ContextVar[_Background | None] = ContextVar("background_research", default=None)


@dataclass(frozen=True)
class _Observation:
    admit: Callable[[str], None]
    check: Callable[[], None]


_OBSERVATION: ContextVar[_Observation | None] = ContextVar("record_only_observation", default=None)


class HostBusy(TimeoutError):
    """Background research asked a host that an open window is polling. It gives way at once,
    as a timeout does, and the research tries again later."""


class ResponseTooLarge(ValueError):
    """A bounded trial exceeded its response-byte allowance; it is not usable content."""


# Says who we are, in the same form search engine crawlers use
USER_AGENT = "Mozilla/5.0 (compatible; earnings-bot/0.1; +https://github.com/es6/earnings-bot)"

# Minimum seconds between requests to one host, or to one site whose hosts share a queue. SEC
# allows 10 requests a second across everything we send it, so all of sec.gov gets 8 a second,
# leaving headroom. The file CDN serves many companies, and Apple's newsroom is a few guessed URLs
# plus a feed on one very large site. PR Newswire's earnings feed is read fresh every second and
# from its CDN every two. Every other host gets one request a second.
_HOST_GAPS = {"sec.gov": 0.125, "q4cdn.com": 0.2, "apple.com": 0.25, "prnewswire.com": 0.5}
_DEFAULT_GAP = 1.0
# Sites whose hosts all wait in one queue, because the site's limit covers them together:
# www.sec.gov (the latest-filings feed, filings and exhibits) and data.sec.gov (each company's
# submissions) count against SEC's one limit. Every other host has its own queue, so each of
# the file CDN's hosts (s1.q4cdn.com, s23.q4cdn.com, ...) is paced apart.
_SHARED_QUEUES: tuple[str, ...] = ("sec.gov",)

# aiohttp stops at 8190 bytes for any one header line. cloudflare.com's 404 pages sometimes
# carry a 13 KB Content-Security-Policy-Report-Only header, which made every guess there fail.
MAX_HEADER_BYTES = 64 * 1024


# A site that answers 403 is tried again after 30 s, then 60 s, up to every 5 minutes. Nebius
# refused one poll 12 minutes before its Q3 2026 release and answered normally afterwards, but
# the route had been stopped for the rest of the window.
BLOCK_PAUSE, BLOCK_PAUSE_MAX = 30.0, 300.0


@dataclass
class RoutePause:
    """When one route may be asked again after it refused us or asked us to wait. Every loop
    that polls the route shares one (see Fetcher.route_pause): Nebius's newsroom refused us after
    an hour of one request a second from one address, and a release's recovery read of the same
    newsroom mustn't add to that while the route's own watcher waits."""

    blocks: int = 0  # refusals (HTTP 403) in a row, which lengthen the pause
    until: float = 0.0  # time.monotonic() before which the route isn't asked again

    def refused(self) -> float:
        """Records a 403 and returns the pause it starts."""
        self.blocks += 1
        pause = min(BLOCK_PAUSE * 2 ** (self.blocks - 1), BLOCK_PAUSE_MAX)
        self.hold(pause)
        return pause

    def hold(self, seconds: float) -> None:
        """Leaves the route alone for `seconds`, such as a Retry-After."""
        self.until = max(self.until, time.monotonic() + seconds)

    def answered(self) -> None:
        self.blocks = 0
        self.until = 0.0

    def left(self) -> float:
        """Seconds until the route may be asked again."""
        return max(0.0, self.until - time.monotonic())


def _on_domain(host: str, domain: str) -> bool:
    host = host.rstrip(".")
    return host == domain or host.endswith("." + domain)


def host_gap(host: str) -> float:
    return next(
        (gap for domain, gap in _HOST_GAPS.items() if _on_domain(host, domain)), _DEFAULT_GAP
    )


def queue_key(host: str) -> str:
    """The queue a host's requests wait in: its site's, if the site's hosts share one, else the
    host's own."""
    return next((site for site in _SHARED_QUEUES if _on_domain(host, site)), host)


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]  # names in lowercase
    body: bytes
    elapsed: float  # seconds to read the whole response, excluding pacing waits
    queued: float = 0.0  # seconds spent waiting for request slots
    # Chrome's separate waits, in ms: its tab lock, a first navigation, the host's pacing
    waits: dict[str, int] = field(default_factory=dict)


@dataclass
class _HostQueue:
    """Requests waiting for one host's turn (or one site's, see queue_key): urgent ones first,
    then in order of arrival."""

    next_turn: float = 0.0  # time.monotonic() from which the host can take another request
    waiting: list[tuple[int, int, asyncio.Future[None]]] = field(default_factory=list)
    dispatcher: asyncio.Task[None] | None = None
    policies: dict[asyncio.Future[None], _Background] = field(default_factory=dict)
    observations: dict[asyncio.Future[None], _Observation] = field(default_factory=dict)


# An already-encoded URL: allowed characters, and % only as the start of an escape
_ENCODED = re.compile(r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})+")


def _as_written(url: str) -> bool:
    """Whether to send a URL exactly as written. yarl would otherwise decode an escaped letter
    such as %6E back to "n", undoing a spelling meant to miss a cache."""
    return "%" in url and bool(_ENCODED.fullmatch(url))


class Fetcher:
    """Use as `async with Fetcher(...) as fetcher:` so the connection pool gets closed."""

    def __init__(self, sec_user_agent: str, timeout: float = 10.0) -> None:
        self._sec_user_agent = sec_user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._queues: dict[str, _HostQueue] = {}  # by queue_key
        self._arrivals = itertools.count()
        self._browser: DesktopChrome | None = None
        self._research_lock = asyncio.Lock()
        self._route_pauses: dict[str, RoutePause] = {}
        self._live: dict[str, float] = {}  # by queue_key: when a poll last took a turn
        self._background_next: dict[str, float] = {}  # by queue_key: next background turn
        self._background_locks: dict[str, asyncio.Lock] = {}

    def route_pause(self, route: str) -> RoutePause:
        """The pause shared by every loop that polls `route`, such as an IR listing's URL."""
        return self._route_pauses.setdefault(route, RoutePause())

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            max_line_size=MAX_HEADER_BYTES,
            max_field_size=MAX_HEADER_BYTES,
            # Each request stands alone, like curl's. cloudflare.com answered 500 to every request
            # that sent back the analytics cookies its first answer set.
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            await self.close_browser()
        finally:
            for queue in self._queues.values():
                if queue.dispatcher is not None:
                    queue.dispatcher.cancel()
            if self._session is not None:
                await self._session.close()

    @contextmanager
    def observing(self, admit: Callable[[str], None], check: Callable[[], None]) -> Iterator[None]:
        """A record-only trial reserves every transport turn and waits behind live polling.
        Callbacks enforce its own budget/deadline, including queued turns and redirects."""
        token = _OBSERVATION.set(_Observation(admit, check))
        try:
            yield
        finally:
            _OBSERVATION.reset(token)

    @contextmanager
    def background(
        self,
        *,
        chrome: bool | Callable[[], bool],
        active: bool | Callable[[], bool] = True,
        paused: bool | Callable[[], bool] = False,
    ) -> Iterator[None]:
        """Requests made inside, and in tasks started inside, run in the background: behind
        every poll, never to a host an open window is polling, and to SEC at most once every
        BACKGROUND_SEC_GAP seconds. Without `chrome`, research may not open its own Chrome."""
        token = _BACKGROUND.set(_Background(chrome, active, paused))
        try:
            yield
        finally:
            _BACKGROUND.reset(token)

    def chrome_allowed(self) -> bool:
        """Whether research may open its own Chrome now."""
        mode = _BACKGROUND.get()
        return mode is None or (
            not mode.deferred.is_set()
            and (not _flag(mode.active) or (not _flag(mode.paused) and _flag(mode.chrome)))
        )

    def research_deferred(self) -> bool:
        mode = _BACKGROUND.get()
        return mode is not None and mode.deferred.is_set()

    def defer_research(self) -> None:
        if mode := _BACKGROUND.get():
            mode.deferred.set()

    def _browser_allowed(self) -> bool:
        if observation := _OBSERVATION.get():
            observation.check()
        allowed = self.chrome_allowed()
        if not allowed:
            self.defer_research()
        return allowed

    def _check_chrome(self) -> None:
        if not self._browser_allowed():
            raise BrowserError("Research waits for live Chrome windows to close")

    @asynccontextmanager
    async def research_browser(self) -> AsyncIterator[DesktopChrome]:
        self._check_chrome()
        async with self._research_lock:
            self._check_chrome()
            browser = DesktopChrome()
            browser.allowed = self._browser_allowed
            try:
                yield browser
            finally:
                await browser.close()

    async def close_browser(self) -> None:
        if self._browser is not None:
            browser, self._browser = self._browser, None
            await browser.close()

    async def close_observations(self) -> None:
        if self._browser is not None:
            await self._browser.close_observations()

    async def browser_request(
        self,
        url: str,
        warmup: str,
        give_up_after: float,
        *,
        urgent: bool = False,
        cache_mode: CacheMode = "no-store",
        bypass_service_worker: bool | None = None,
        max_bytes: int = MAX_BROWSER_BYTES,
    ) -> Response:
        self._check_chrome()
        if self._session is None:
            raise RuntimeError("Fetcher must be used with 'async with'")
        if self._browser is None:
            self._browser = DesktopChrome()
            self._browser.allowed = self._browser_allowed
        return await self._browser.request(
            url,
            warmup,
            give_up_after,
            lambda host: self.wait_turn(host, urgent),
            urgent=urgent,
            cache_mode=cache_mode,
            bypass_service_worker=bypass_service_worker,
            observational=_OBSERVATION.get() is not None,
            max_bytes=max_bytes,
        )

    async def browser_render(
        self,
        url: str,
        give_up_after: float,
        *,
        ready: Callable[[RenderedPage], bool] | None = None,
    ) -> RenderedPage:
        self._check_chrome()
        if self._session is None:
            raise RuntimeError("Fetcher must be used with 'async with'")
        if self._browser is None:
            self._browser = DesktopChrome()
            self._browser.allowed = self._browser_allowed
        return await self._browser.render(url, give_up_after, self.wait_turn, ready=ready)

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
        max_bytes: int | None = None,
    ) -> Response:
        """Raises TimeoutError if the whole response hasn't arrived within `give_up_after`
        seconds (the Fetcher's own timeout if None). Waiting for the initial slot doesn't count,
        but redirects and retries share the same HTTP deadline. An urgent request, such
        as fetching a release that just turned up, takes the host's next turn ahead of polls.
        SEC's hosts take turns together, so an exhibit download from www.sec.gov also goes
        ahead of polls of data.sec.gov."""
        if self._session is None:
            raise RuntimeError("Fetcher must be used with `async with`")
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("Response byte limit must be positive")
        host = urlsplit(url).hostname or ""
        asked = time.monotonic()
        await self.wait_turn(host, urgent)
        sec = _on_domain(host, "sec.gov")
        user_agent = self._sec_user_agent if sec else USER_AGENT
        headers = {"User-Agent": user_agent, **(headers or {})}
        followup_wait = 0.0
        first_attempt = True

        async def pace_followup(
            request: aiohttp.ClientRequest, send: aiohttp.ClientHandlerType
        ) -> aiohttp.ClientResponse:
            nonlocal first_attempt, followup_wait
            # aiohttp invokes middleware for each redirect and connection retry.
            if not first_attempt:
                attempt_host = request.url.host or ""
                attempt_sec = _on_domain(attempt_host, "sec.gov")
                if attempt_sec != sec:
                    request.headers["User-Agent"] = (
                        self._sec_user_agent if attempt_sec else USER_AGENT
                    )
                # SEC's hops share its one limit. A record-only trial counts every hop against
                # its own budget; monitoring's redirects elsewhere don't wait another turn.
                if attempt_sec or _OBSERVATION.get() is not None:
                    waiting = time.monotonic()
                    await self.wait_turn(attempt_host, urgent)
                    followup_wait += time.monotonic() - waiting
            first_attempt = False
            return await send(request)

        # aiohttp rounds a timeout of 5 s or more up to a whole second; shorter ones are exact
        limit = (
            self._timeout if give_up_after is None else aiohttp.ClientTimeout(total=give_up_after)
        )
        started = time.monotonic()
        async with self._session.request(
            method,
            URL(url, encoded=True) if encoded_url or _as_written(url) else url,
            headers=headers,
            timeout=limit,
            allow_redirects=allow_redirects,
            middlewares=(pace_followup,),
        ) as r:
            if max_bytes is None:
                body = await r.read()
            else:
                chunks = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    if len(chunks) + len(chunk) > max_bytes:
                        raise ResponseTooLarge("Response exceeded the trial's byte allowance")
                    chunks.extend(chunk)
                body = bytes(chunks)
            headers = {name.lower(): value for name, value in r.headers.items()}
        return Response(
            url,
            r.status,
            headers,
            body,
            time.monotonic() - started - followup_wait,
            queued=started - asked + followup_wait,
        )

    async def wait_turn(self, host: str, urgent: bool = False) -> None:
        """Returns once it's the caller's turn to send the host a request. Raises HostBusy for
        background research to a host an open window is polling."""
        key = queue_key(host)
        observation = _OBSERVATION.get()
        if observation:
            observation.admit(host)
        if _background() is None:
            if _BACKGROUND.get() is None and observation is None:
                self._live[key] = time.monotonic()
            await self._take_turn(key, 2 if observation else (0 if urgent else 1))
            _background()
            if observation:
                observation.check()
            return
        if key not in _SHARED_QUEUES:
            if time.monotonic() - self._live.get(key, float("-inf")) < LIVE_SECONDS:
                self.defer_research()
                raise HostBusy(f"{key} is being polled for an open release window")
            await self._take_turn(key, 2)
            _background()
            if time.monotonic() - self._live.get(key, float("-inf")) < LIVE_SECONDS:
                self.defer_research()
                raise HostBusy(f"{key} became active while research waited")
            return
        # SEC is shared by every window and needed by nearly all research, so research gets a
        # sliver of it: one turn every BACKGROUND_SEC_GAP seconds, each behind every poll. The
        # lock makes background requests take their turns one at a time.
        async with self._background_locks.setdefault(key, asyncio.Lock()):
            while (wait := self._background_next.get(key, 0.0) - time.monotonic()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(wait)
            _background()
            await self._take_turn(key, 2)
            _background()
            self._background_next[key] = time.monotonic() + BACKGROUND_SEC_GAP

    async def _take_turn(self, key: str, priority: int) -> None:
        """Waits in the queue: urgent requests (0) first, then polls (1), then background
        research (2), each in order of arrival."""
        queue = self._queues.setdefault(key, _HostQueue())
        now = time.monotonic()
        if not queue.waiting and now >= queue.next_turn:
            queue.next_turn = now + host_gap(key)
            return
        turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if mode := _BACKGROUND.get():
            queue.policies[turn] = mode
        if observation := _OBSERVATION.get():
            queue.observations[turn] = observation
        heapq.heappush(queue.waiting, (priority, next(self._arrivals), turn))
        if queue.dispatcher is None or queue.dispatcher.done():
            queue.dispatcher = asyncio.create_task(self._dispatch(key, queue))
        # A caller that gets cancelled cancels its turn too, and the dispatcher skips it
        try:
            await turn
        finally:
            queue.policies.pop(turn, None)
            queue.observations.pop(turn, None)

    async def _dispatch(self, key: str, queue: _HostQueue) -> None:
        # Hands out the queue's turns one gap apart, best waiter first, until nobody's waiting
        while queue.waiting:
            # asyncio can wake a sleeper up to its clock's resolution early, and a turn must
            # never come sooner than the gap, or SEC could see 9 requests in one second
            while (wait := queue.next_turn - time.monotonic()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(wait)
            while queue.waiting:
                priority, _, turn = heapq.heappop(queue.waiting)
                if not turn.done():
                    if observation := queue.observations.get(turn):
                        try:
                            observation.check()
                        except Exception as error:
                            turn.set_exception(error)
                            continue
                    if mode := queue.policies.get(turn):
                        try:
                            blocked = mode.deferred.is_set() or (
                                _flag(mode.active)
                                and (
                                    priority < 2
                                    or _flag(mode.paused)
                                    or (
                                        key not in _SHARED_QUEUES
                                        and time.monotonic() - self._live.get(key, float("-inf"))
                                        < LIVE_SECONDS
                                    )
                                )
                            )
                        except Exception as error:
                            turn.set_exception(error)
                            continue
                        if blocked:
                            mode.deferred.set()
                            turn.set_exception(
                                HostBusy("Research yielded before taking a monitoring turn")
                            )
                            continue
                    queue.next_turn = time.monotonic() + host_gap(key)
                    turn.set_result(None)
                    break
