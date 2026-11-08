"""The public @earnings_guy comparison, independent of release detection."""

import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlencode, urlsplit

import aiohttp

from earnings_bot.fetch import Fetcher
from earnings_bot.text import html_to_text

ACCOUNT = "earnings_guy"
# The RSS copies surface candidate links. X verifies the account and text for every result.
TIMELINES = (
    "https://teapawt.org/earnings_guy/rss",
    "https://nitter.cf/earnings_guy/rss",
)
EMBED = "https://publish.x.com/oembed"
POLL_SECONDS = 30.0
_X_EPOCH_MS = 1288834974657
_STATUS = re.compile(r"/earnings_guy/status/(\d{17,20})/?", re.IGNORECASE)
_RESULTS = re.compile(r"^\$([\w.-]+)\s+\([^\n]+?\)\s+#?earnings\s+are\s+out\b", re.IGNORECASE)


@dataclass(frozen=True)
class BenchmarkPost:
    id: str
    ticker: str

    @property
    def posted(self) -> datetime:
        return datetime.fromtimestamp(((int(self.id) >> 22) + _X_EPOCH_MS) / 1000, UTC)

    @property
    def url(self) -> str:
        return f"https://x.com/{ACCOUNT}/status/{self.id}"


@dataclass(frozen=True)
class BenchmarkResult:
    state: Literal["matched", "no_post_found", "unavailable", "unverified"]
    post: BenchmarkPost | None = None


def _ticker(text: str) -> str | None:
    match = _RESULTS.match(text.strip())
    return match.group(1).upper() if match else None


def _parse_feed(body: bytes) -> tuple[BenchmarkPost, ...]:
    channel = ET.fromstring(body).find("channel")
    if channel is None:
        raise ValueError("Missing RSS channel")
    posts = []
    for item in channel.findall("item"):
        link = urlsplit(item.findtext("link") or "")
        match = _STATUS.fullmatch(link.path)
        ticker = _ticker(item.findtext("title") or "")
        if match and ticker:
            posts.append(BenchmarkPost(match.group(1), ticker))
    return tuple(posts)


class EarningsBenchmark:
    """A shared, paced account snapshot for all overlapping earnings windows."""

    def __init__(self, fetcher: Fetcher, now: Callable[[], datetime]) -> None:
        self.fetcher = fetcher
        self.now = now
        self._lock = asyncio.Lock()
        self._next_poll = 0.0
        self._snapshot: tuple[BenchmarkPost, ...] | None = None
        self._retry_at: dict[str, float] = {}

    async def snapshot(self) -> tuple[BenchmarkPost, ...] | None:
        async with self._lock:
            if time.monotonic() < self._next_poll:
                return self._snapshot
            self._snapshot = None
            for url in TIMELINES:
                if time.monotonic() < self._retry_at.get(url, 0):
                    continue
                try:
                    response = await self.fetcher.request(
                        "GET", url, give_up_after=4, allow_redirects=False
                    )
                    if response.status in (429, 503):
                        retry = response.headers.get("retry-after", "")
                        delay = max(POLL_SECONDS, int(retry)) if retry.isdecimal() else 60
                        self._retry_at[url] = time.monotonic() + delay
                    if response.status == 200:
                        self._snapshot = _parse_feed(response.body)
                        break
                except (aiohttp.ClientError, TimeoutError, ValueError, ET.ParseError):
                    continue
            self._next_poll = time.monotonic() + POLL_SECONDS
            return self._snapshot

    async def verify(self, post: BenchmarkPost) -> bool | None:
        """True for an original results post, False for a mismatch, None when X is unavailable."""
        params = urlencode({"url": post.url, "omit_script": "1"})
        try:
            response = await self.fetcher.request(
                "GET", f"{EMBED}?{params}", give_up_after=4, allow_redirects=False
            )
            if response.status != 200:
                return None
            data = json.loads(response.body)
            author = urlsplit(data["author_url"])
            original = urlsplit(data["url"])
            match = _STATUS.fullmatch(original.path)
            return (
                original.hostname in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
                and match is not None
                and match.group(1) == post.id
                and author.hostname in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
                and author.path.rstrip("/").casefold() == f"/{ACCOUNT}"
                and _ticker(html_to_text(data["html"])) == post.ticker
            )
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError, KeyError, AttributeError):
            return None

    async def watch(self, ticker: str, start: datetime, end: datetime) -> BenchmarkResult:
        """Gather the earliest matching post ID during the window, then verify it with X."""
        while (remaining := (start - self.now()).total_seconds()) > 0:  # noqa: ASYNC110
            await asyncio.sleep(min(remaining, POLL_SECONDS))
        candidates: dict[str, BenchmarkPost] = {}
        saw_timeline = False
        while True:
            snapshot = await self.snapshot()
            if snapshot is not None:
                saw_timeline = True
                for post in snapshot:
                    if post.ticker == ticker.upper() and start <= post.posted <= end:
                        candidates[post.id] = post
            remaining = (end - self.now()).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POLL_SECONDS, remaining))

        unverified = False
        for post in sorted(candidates.values(), key=lambda candidate: candidate.posted):
            match = await self.verify(post)
            if match:
                return BenchmarkResult("matched", post)
            unverified |= match is None
        if unverified:
            return BenchmarkResult("unverified")
        return BenchmarkResult("no_post_found" if saw_timeline else "unavailable")
