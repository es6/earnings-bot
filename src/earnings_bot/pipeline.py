"""One Discord post per release: an alert with links the moment any source sees it, then the
release files as they appear, then the summary edited in."""

import asyncio
import io
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord

from earnings_bot.estimates import Estimates, EstimatesError, fmp_estimates
from earnings_bot.fetch import Fetcher
from earnings_bot.recorder import ET, Journal, Plan, Target, record
from earnings_bot.sources import EdgarFilings, Q4FinancialReports, Q4PressReleases, Sighting, Source
from earnings_bot.summary import Summarizer, SummaryError
from earnings_bot.text import release_text

FILE_TYPES = (".pdf", ".xlsx", ".xls")
# Discord's upload limit for bots in servers without boosts. Earnings PDFs are well under 1 MB.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CONTENT = 2000  # Discord's limit for a message's text


class Poster(ABC):
    """Where a release gets posted: one message, created once and then edited. Each step returns
    the time the platform put on the change, when it gives one."""

    @abstractmethod
    async def send(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def edit(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def attach(self, name: str, data: bytes) -> datetime | None: ...

    @abstractmethod
    async def set_summary(self, text: str, footer: str) -> datetime | None: ...


class DiscordPoster(Poster):
    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self.message: discord.Message | None = None

    def _sent(self) -> discord.Message:
        if self.message is None:
            raise RuntimeError("send() must come before any edit")
        return self.message

    async def send(self, content: str) -> datetime | None:
        self.message = await self.channel.send(content)
        return self.message.created_at

    async def edit(self, content: str) -> datetime | None:
        self.message = await self._sent().edit(content=content)
        return self.message.edited_at

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        file = discord.File(io.BytesIO(data), filename=name)
        self.message = await message.edit(attachments=[*message.attachments, file])
        return self.message.edited_at

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        # An embed fits 4,096 characters, twice a plain message
        embed = discord.Embed(description=text, color=discord.Color.dark_teal())
        embed.set_footer(text=footer)
        self.message = await self._sent().edit(embed=embed)
        return self.message.edited_at


def describe_source(source: Source) -> str:
    if isinstance(source, Q4PressReleases | Q4FinancialReports):
        return "IR site"
    if isinstance(source, EdgarFilings):
        return "SEC 8-K"
    if source.name.startswith("businesswire"):
        return "Business Wire"
    if source.name.startswith("guess:"):
        return "guessed URL"
    return source.name.removeprefix("rss:")


def _file_name(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


class Release:
    """Everything known about one company's release, and its Discord post."""

    def __init__(
        self,
        target: Target,
        poster: Poster,
        fetcher: Fetcher,
        journal: Journal,
        summarizer: Summarizer | None,
        fmp_api_key: str | None,
        note: str = "",
    ) -> None:
        self.target = target
        self.poster = poster
        self.fetcher = fetcher
        self.journal = journal
        self.summarizer = summarizer
        self.note = note
        self.links: dict[str, str] = {}  # url -> label, in the order they turned up
        self.files: set[str] = set()
        self.first_seen: datetime | None = None
        self.first_seen_at = 0.0  # time.monotonic(), for timing the steps
        self.posted = False
        self.summarizing = False
        self.lock = asyncio.Lock()  # one Discord edit at a time, in order
        self.tasks: set[asyncio.Task[None]] = set()
        self.estimates = asyncio.create_task(self._estimates(fmp_api_key))

    def add(self, source: Source, sighting: Sighting) -> None:
        """Called by the poller for each new item that looks like the release. Returns at once;
        the work runs in the background."""
        if self.first_seen is None:
            self.first_seen, self.first_seen_at = self.journal.now(), time.monotonic()
        label = describe_source(source)
        is_file = urlsplit(sighting.url).path.lower().endswith(FILE_TYPES)
        self._spawn(self._link(_file_name(sighting.url) if is_file else label, sighting.url))
        if is_file:
            self._spawn(self._attach(sighting.url))
        elif self.summarizer and not self.summarizing:
            self._spawn(self._summarize(self.summarizer, source, sighting, label))

    async def finish(self) -> None:
        """Waits for every pending step."""
        while self.tasks:
            await asyncio.gather(*self.tasks)

    def _since_first_seen(self) -> float:
        return round(time.monotonic() - self.first_seen_at, 2)

    def _live_after(self, stamp: datetime | None) -> float | None:
        """Seconds from the first sighting to Discord's own timestamp on a change, which is when
        people could see it. Discord's reply can reach us seconds later than that, when
        discord.py waits out a rate limit before returning."""
        if stamp is None or self.first_seen is None:
            return None
        return round((stamp - self.first_seen).total_seconds(), 2)

    def _content(self) -> str:
        seen = f"{self.first_seen.astimezone(ET):%H:%M:%S.%f}"[:-3] if self.first_seen else ""
        first_label = next(iter(self.links.values()), "")
        lines = [
            f"🚨 **${self.target.ticker} earnings are out**{self.note}",
            f"-# First seen {seen} ET on the {first_label}",
        ]
        lines += [f"{label}: <{url}>" for url, label in self.links.items()]
        content = "\n".join(lines)
        return content if len(content) <= MAX_CONTENT else content[: MAX_CONTENT - 1] + "…"

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(self._guard(work))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _guard(self, work: Coroutine[Any, Any, None]) -> None:
        # These run in the background, so log failures instead of losing them
        try:
            await work
        except Exception as error:
            self.journal.write("post_error", ticker=self.target.ticker, message=repr(error))
            self.journal.say(f"{self.target.ticker}: posting step failed: {error!r}")

    async def _estimates(self, api_key: str | None) -> Estimates | None:
        if not api_key:
            return None
        try:
            estimates = await fmp_estimates(
                self.fetcher, api_key, self.target.ticker, self.target.expected.date()
            )
        except (EstimatesError, aiohttp.ClientError, TimeoutError, ValueError, KeyError) as error:
            self.journal.write("estimates_error", ticker=self.target.ticker, message=str(error))
            self.journal.say(f"{self.target.ticker}: no estimates: {error}")
            return None
        self.journal.write(
            "estimates", ticker=self.target.ticker, eps=estimates.eps, revenue=estimates.revenue
        )
        return estimates

    async def _link(self, label: str, url: str) -> None:
        async with self.lock:
            if url in self.links:
                return
            self.links[url] = label
            if self.posted:
                await self.poster.edit(self._content())
                return
            stamp = await self.poster.send(self._content())
            self.posted = True
        live = self._live_after(stamp)
        self.journal.write(
            "posted", ticker=self.target.ticker, seconds=self._since_first_seen(), live_seconds=live
        )
        self.journal.say(f"{self.target.ticker}: posted the alert ({label}), live after {live}s")

    async def _attach(self, url: str) -> None:
        name = _file_name(url)
        if name in self.files:
            return
        self.files.add(name)
        response = await self.fetcher.request("GET", url)
        if response.status != 200 or len(response.body) > MAX_FILE_BYTES:
            self.journal.say(
                f"{self.target.ticker}: couldn't attach {name} (HTTP "
                f"{response.status}, {len(response.body):,} bytes)"
            )
            return
        async with self.lock:
            stamp = await self.poster.attach(name, response.body)
        self.journal.write(
            "attached",
            ticker=self.target.ticker,
            file=name,
            seconds=self._since_first_seen(),
            live_seconds=self._live_after(stamp),
        )
        self.journal.say(f"{self.target.ticker}: attached {name}")

    async def _summarize(
        self, summarizer: Summarizer, source: Source, sighting: Sighting, label: str
    ) -> None:
        text = await release_text(self.fetcher, source, sighting)
        # Several sources can deliver the text; the first one wins
        if text is None or self.summarizing:
            return
        self.summarizing = True
        estimates = await self.estimates
        started = time.monotonic()
        try:
            summary = await summarizer.write(self.target.ticker, text, estimates)
        except SummaryError as error:
            self.summarizing = False  # let a later source try
            self.journal.write("summary_error", ticker=self.target.ticker, message=str(error))
            self.journal.say(f"{self.target.ticker}: summary failed: {error}")
            return
        took = time.monotonic() - started
        footer = f"Summary written by Claude in {took:.0f}s from the {label} text"
        if summary.unverified:
            shown = ", ".join(summary.unverified[:8])
            footer += f" · Check these, they aren't in the release as written: {shown}"
        async with self.lock:
            stamp = await self.poster.set_summary(summary.text, footer[:2048])
        self.journal.write(
            "summary",
            ticker=self.target.ticker,
            seconds=self._since_first_seen(),
            live_seconds=self._live_after(stamp),
            claude_seconds=round(took, 1),
            source=label,
            text=summary.text,
            unverified=summary.unverified,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        )
        self.journal.say(f"{self.target.ticker}: summary posted after {self._since_first_seen()}s")


async def watch_schedule(
    plan: Plan,
    log_path: Path,
    channel: discord.abc.Messageable,
    sec_user_agent: str,
    summarizer: Summarizer | None,
    fmp_api_key: str | None,
) -> None:
    """Runs the schedule and posts each release to the channel."""
    with log_path.open("a", encoding="utf-8") as file:
        journal = Journal(file)
        async with Fetcher(sec_user_agent) as fetcher:
            releases: dict[str, Release] = {}

            def on_new(target: Target, source: Source, sighting: Sighting) -> None:
                if not sighting.earnings:
                    return
                if target.ticker not in releases:
                    poster = DiscordPoster(channel)
                    releases[target.ticker] = Release(
                        target, poster, fetcher, journal, summarizer, fmp_api_key
                    )
                releases[target.ticker].add(source, sighting)

            await record(plan, fetcher, journal, on_new)
            for release in releases.values():
                await release.finish()
