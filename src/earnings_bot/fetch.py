"""One HTTP client for every source: a shared connection pool, honest User-Agents, and per-host
pacing so that a busy evening can't hammer any one site."""

import asyncio
import time
from dataclasses import dataclass
from typing import Self
from urllib.parse import urlsplit

import aiohttp

# Says who we are, in the same form search engine crawlers use
USER_AGENT = "Mozilla/5.0 (compatible; earnings-bot/0.1; +https://github.com/es6/earnings-bot)"

# Minimum seconds between requests to one host. SEC allows 10 a second across everything we send
# it. The file CDN serves many companies, and Apple's newsroom is a few guessed URLs plus a feed
# on one very large site. Every other host gets one request a second.
_HOST_GAPS = {"sec.gov": 0.125, "q4cdn.com": 0.2, "apple.com": 0.25}
_DEFAULT_GAP = 1.0

# aiohttp stops at 8190 bytes for any one header line. cloudflare.com's 404 pages sometimes
# carry a 13 KB Content-Security-Policy-Report-Only header, which made every guess there fail.
MAX_HEADER_BYTES = 64 * 1024


def _on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def host_gap(host: str) -> float:
    return next(
        (gap for domain, gap in _HOST_GAPS.items() if _on_domain(host, domain)), _DEFAULT_GAP
    )


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]  # names in lowercase
    body: bytes
    elapsed: float  # seconds from sending the request to reading the whole body


class Fetcher:
    """Use as `async with Fetcher(...) as fetcher:` so the connection pool gets closed."""

    def __init__(self, sec_user_agent: str, timeout: float = 10.0) -> None:
        self._sec_user_agent = sec_user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._next_slot: dict[str, float] = {}

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
        if self._session is not None:
            await self._session.close()

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
    ) -> Response:
        """Raises TimeoutError if the whole response hasn't arrived within `give_up_after`
        seconds (the Fetcher's own timeout if None). Waiting for this host's turn doesn't count,
        which is why this isn't left to the caller's `asyncio.timeout`."""
        if self._session is None:
            raise RuntimeError("Fetcher must be used with `async with`")
        host = urlsplit(url).hostname or ""
        await self._wait_turn(host)
        user_agent = self._sec_user_agent if _on_domain(host, "sec.gov") else USER_AGENT
        headers = {"User-Agent": user_agent, **(headers or {})}
        # aiohttp rounds a timeout of 5 s or more up to a whole second; shorter ones are exact
        limit = (
            self._timeout if give_up_after is None else aiohttp.ClientTimeout(total=give_up_after)
        )
        started = time.monotonic()
        async with self._session.request(method, url, headers=headers, timeout=limit) as r:
            body = await r.read()
            headers = {name.lower(): value for name, value in r.headers.items()}
        return Response(url, r.status, headers, body, time.monotonic() - started)

    async def _wait_turn(self, host: str) -> None:
        # Book the next free slot for this host, then sleep until it comes up. Nothing awaits
        # between reading and booking, so concurrent callers always get distinct slots.
        now = time.monotonic()
        slot = max(now, self._next_slot.get(host, now))
        self._next_slot[host] = slot + host_gap(host)
        await asyncio.sleep(slot - now)
