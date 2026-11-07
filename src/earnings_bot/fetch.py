"""One HTTP client for every source: a shared connection pool, honest User-Agents, and pacing
per host (per site, for SEC) so that a busy evening can't hammer any one site."""

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

import aiohttp

from earnings_bot.browser import DesktopChrome

# Says who we are, in the same form search engine crawlers use
USER_AGENT = "Mozilla/5.0 (compatible; earnings-bot/0.1; +https://github.com/es6/earnings-bot)"

# Minimum seconds between requests to one host, or to one site whose hosts share a queue. SEC
# allows 10 requests a second across everything we send it, so all of sec.gov gets 8 a second,
# leaving headroom. The file CDN serves many companies, and Apple's newsroom is a few guessed URLs
# plus a feed on one very large site. Every other host gets one request a second.
_HOST_GAPS = {"sec.gov": 0.125, "q4cdn.com": 0.2, "apple.com": 0.25}
_DEFAULT_GAP = 1.0
# Sites whose hosts all wait in one queue, because the site's limit covers them together:
# www.sec.gov (the latest-filings feed, filings and exhibits) and data.sec.gov (each company's
# submissions) count against SEC's one limit. Every other host has its own queue, so each of
# the file CDN's hosts (s1.q4cdn.com, s23.q4cdn.com, ...) is paced apart.
_SHARED_QUEUES: tuple[str, ...] = ("sec.gov",)

# aiohttp stops at 8190 bytes for any one header line. cloudflare.com's 404 pages sometimes
# carry a 13 KB Content-Security-Policy-Report-Only header, which made every guess there fail.
MAX_HEADER_BYTES = 64 * 1024


def _on_domain(host: str, domain: str) -> bool:
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
    elapsed: float  # seconds from sending the request to reading the whole body
    queued: float = 0.0  # seconds spent waiting for the host's turn before sending


@dataclass
class _HostQueue:
    """Requests waiting for one host's turn (or one site's, see queue_key): urgent ones first,
    then in order of arrival."""

    next_turn: float = 0.0  # time.monotonic() from which the host can take another request
    waiting: list[tuple[int, int, asyncio.Future[None]]] = field(default_factory=list)
    dispatcher: asyncio.Task[None] | None = None


class Fetcher:
    """Use as `async with Fetcher(...) as fetcher:` so the connection pool gets closed."""

    def __init__(self, sec_user_agent: str, timeout: float = 10.0) -> None:
        self._sec_user_agent = sec_user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._queues: dict[str, _HostQueue] = {}  # by queue_key
        self._arrivals = itertools.count()
        self._browser: DesktopChrome | None = None

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
            if self._browser is not None:
                await self._browser.close()
        finally:
            for queue in self._queues.values():
                if queue.dispatcher is not None:
                    queue.dispatcher.cancel()
            if self._session is not None:
                await self._session.close()

    async def browser_request(
        self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
    ) -> Response:
        if self._session is None:
            raise RuntimeError("Fetcher must be used with 'async with'")
        if self._browser is None:
            self._browser = DesktopChrome()
        return await self._browser.request(
            url, warmup, give_up_after, lambda host: self.wait_turn(host, urgent)
        )

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
    ) -> Response:
        """Raises TimeoutError if the whole response hasn't arrived within `give_up_after`
        seconds (the Fetcher's own timeout if None). Waiting for this host's turn doesn't count,
        which is why this isn't left to the caller's `asyncio.timeout`. An urgent request, such
        as fetching a release that just turned up, takes the host's next turn ahead of polls.
        SEC's hosts take turns together, so an exhibit download from www.sec.gov also goes
        ahead of polls of data.sec.gov."""
        if self._session is None:
            raise RuntimeError("Fetcher must be used with `async with`")
        host = urlsplit(url).hostname or ""
        asked = time.monotonic()
        await self.wait_turn(host, urgent)
        user_agent = self._sec_user_agent if _on_domain(host, "sec.gov") else USER_AGENT
        headers = {"User-Agent": user_agent, **(headers or {})}
        # aiohttp rounds a timeout of 5 s or more up to a whole second; shorter ones are exact
        limit = (
            self._timeout if give_up_after is None else aiohttp.ClientTimeout(total=give_up_after)
        )
        started = time.monotonic()
        async with self._session.request(
            method, url, headers=headers, timeout=limit, allow_redirects=allow_redirects
        ) as r:
            body = await r.read()
            headers = {name.lower(): value for name, value in r.headers.items()}
        return Response(
            url, r.status, headers, body, time.monotonic() - started, queued=started - asked
        )

    async def wait_turn(self, host: str, urgent: bool = False) -> None:
        """Returns once it's the caller's turn to send the host a request."""
        key = queue_key(host)
        queue = self._queues.setdefault(key, _HostQueue())
        now = time.monotonic()
        if not queue.waiting and now >= queue.next_turn:
            queue.next_turn = now + host_gap(key)
            return
        turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(queue.waiting, (0 if urgent else 1, next(self._arrivals), turn))
        if queue.dispatcher is None or queue.dispatcher.done():
            queue.dispatcher = asyncio.create_task(self._dispatch(key, queue))
        # A caller that gets cancelled cancels its turn too, and the dispatcher skips it
        await turn

    async def _dispatch(self, key: str, queue: _HostQueue) -> None:
        # Hands out the queue's turns one gap apart, best waiter first, until nobody's waiting
        while queue.waiting:
            # asyncio can wake a sleeper up to its clock's resolution early, and a turn must
            # never come sooner than the gap, or SEC could see 9 requests in one second
            while (wait := queue.next_turn - time.monotonic()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(wait)
            while queue.waiting:
                _, _, turn = heapq.heappop(queue.waiting)
                if not turn.done():
                    queue.next_turn = time.monotonic() + host_gap(key)
                    turn.set_result(None)
                    break
