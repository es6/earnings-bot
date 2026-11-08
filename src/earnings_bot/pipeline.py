"""One Discord post per release: an alert with links the moment any source sees it, then the
release files as they appear, then the summary edited in."""

import asyncio
import contextlib
import hashlib
import io
import re
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord

from earnings_bot.delivery import Delivery, DeliveryStore, retry_delivery
from earnings_bot.estimates import Estimates, EstimatesError, fmp_estimates
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.recorder import ET, Journal, Plan, Target, record
from earnings_bot.sources import (
    BroadcomNewsroom,
    EdgarFilings,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    Sighting,
    Source,
)
from earnings_bot.summary import Summarizer, SummaryError
from earnings_bot.text import release_text, verified_report

FILE_TYPES = (".pdf", ".xlsx", ".xls")
# Discord's upload limit for bots in servers without boosts. Earnings PDFs are well under 1 MB.
MAX_FILE_BYTES = 10 * 1024 * 1024
UNKNOWN_MESSAGE = 10008  # Discord's error code for a missing message
MAX_CONTENT = 2000  # Discord's limit for a message's text
# Seconds between tries for a file that a feed lists before it's uploaded. Cloudflare's Q3 2026
# supplement was listed at 4:15:32 PM and went up at 4:38:11.
ATTACH_RETRY_SECONDS = 30.0
# Seconds between requests that keep the connection to Discord open while a window is open.
# discord.py's HTTP client closes a connection after 15 s idle, and opening a new one cost 86 to
# 175 ms from the owner's PC, against 24 ms on an open one: a third of the 0.2 to 0.4 s from
# detection to a visible alert in the November live runs.
KEEP_WARM_SECONDS = 10.0


async def keep_warm(
    ping: Callable[[], Awaitable[object]],
    windows: Callable[[], Iterable[tuple[datetime, datetime]]],
    journal: Journal,
) -> None:
    """Calls `ping`, a cheap Discord request, every KEEP_WARM_SECONDS while any window is open,
    so the alert doesn't have to open a connection first. Runs until cancelled."""
    while True:
        now = journal.now()
        if any(start <= now < end for start, end in windows()):
            # A failed ping costs nothing but the warm connection
            with contextlib.suppress(discord.DiscordException, aiohttp.ClientError, OSError):
                await ping()
        await asyncio.sleep(KEEP_WARM_SECONDS)


class Poster(ABC):
    """Where a release gets posted: one message, created once and then edited. Each step returns
    the time the platform put on the change, when it gives one."""

    resumed = False

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
        self.nonce = secrets.token_hex(12)
        self.message: discord.Message | None = None

    def _sent(self) -> discord.Message:
        if self.message is None:
            raise RuntimeError("send() must come before any edit")
        return self.message

    async def send(self, content: str) -> datetime | None:
        self.message = await self.channel.send(content, nonce=self.nonce)
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


class DurableDiscordPoster(DiscordPoster):
    def __init__(
        self,
        channel: discord.abc.Messageable,
        store: DeliveryStore,
        key: str,
        author_id: int,
    ) -> None:
        super().__init__(channel)
        self.store = store
        self.key = key
        self.author_id = author_id

    def _nonce(self, saved: Delivery) -> str:
        """The same for every try at one stored alert, across restarts, so Discord drops a
        repeat. A new alert for the key, after the old one was deleted, gets a new one."""
        intent = f"{self.store.channel_id}:{self.key}:{saved.prepared.isoformat()}"
        return hashlib.blake2s(intent.encode(), digest_size=12).hexdigest()

    async def send(self, content: str) -> datetime | None:
        async with self.store.lock(self.key):
            return await self._deliver(content)

    async def _send_prepared(self, saved: Delivery) -> datetime:
        self.message = await self.channel.send(saved.content, nonce=self._nonce(saved))
        self.store.delivered(self.key, self.message.id)
        return self.message.created_at

    async def _deliver(self, content: str) -> datetime | None:
        saved = self.store.get(self.key)
        message = None
        if saved is not None and saved.message_id is not None:
            try:
                message = await self.channel.fetch_message(saved.message_id)
            except discord.NotFound as error:
                if error.code != UNKNOWN_MESSAGE:
                    raise
                # Keep the cached links when this poster watched the deleted alert.
                cached = self.message
                prior = (
                    cached.content if cached and cached.id == saved.message_id else saved.content
                )
                content = self._merge_content(prior, content)
                self.store.forget(self.key)
                saved = None
        if saved is None:
            saved = self.store.prepare(self.key, content)
        elif message is None:
            # Discord's nonce protection is short-lived. After a crash, look for the
            # exact persisted initial message before sending again.
            after = saved.prepared - timedelta(seconds=5)
            async for candidate in self.channel.history(limit=None, after=after):
                if candidate.author.id == self.author_id and candidate.content == saved.content:
                    message = candidate
                    break
        self.resumed = message is not None
        if message is None:
            created = await self._send_prepared(saved)
            message = self._sent()
        else:
            created = None
            self.message = message
            self.store.delivered(self.key, message.id)
        if content == message.content:
            return created
        merged = self._merge_content(message.content, content)
        if merged != message.content:
            try:
                await super().edit(merged)
            except discord.NotFound as error:
                if error.code != UNKNOWN_MESSAGE:
                    raise
                # The message was deleted between loading it and editing it.
                self.store.forget(self.key)
                self.resumed = False
                return await self._send_prepared(self.store.prepare(self.key, merged))
        return created

    def _merge_content(self, existing: str, content: str) -> str:
        lines = existing.splitlines()
        urls = [line.rsplit("<", 1)[-1].rstrip(">") for line in lines[2:]]
        for line in content.splitlines()[2:]:
            url = line.rsplit("<", 1)[-1].rstrip(">")
            if url not in urls and not any(_same_filing(url, known) for known in urls):
                lines.append(line)
                urls.append(url)
        merged = "\n".join(lines)
        return merged if len(merged) <= MAX_CONTENT else merged[: MAX_CONTENT - 1] + "…"

    async def _update(
        self,
        action: Callable[[], Awaitable[datetime | None]],
        content: str,
        *,
        retry_after_replacement: bool = False,
    ) -> datetime | None:
        try:
            return await action()
        except discord.NotFound as error:
            if error.code != UNKNOWN_MESSAGE:
                raise
        posted = await retry_delivery(partial(self.send, content))
        return await action() if retry_after_replacement else posted

    async def edit(self, content: str) -> datetime | None:
        message = self._sent()
        merged = self._merge_content(message.content, content)
        if merged == message.content:
            return message.edited_at
        return await self._update(partial(super().edit, merged), merged)

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        if any(attachment.filename == name for attachment in message.attachments):
            return message.edited_at
        return await self._update(
            partial(super().attach, name, data), message.content, retry_after_replacement=True
        )

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        return await self._update(
            partial(super().set_summary, text, footer),
            self._sent().content,
            retry_after_replacement=True,
        )


def describe_source(source: Source) -> str:
    if isinstance(source, Q4PressReleases | Q4FinancialReports):
        return "IR site"
    if isinstance(source, EdgarFilings):
        return "SEC 8-K"
    if isinstance(source, BroadcomNewsroom):
        return "Broadcom newsroom"
    if source.name.startswith("businesswire"):
        return "Business Wire"
    if source.name.startswith("prnewswire"):
        return "PR Newswire"
    if source.name.startswith("globenewswire"):
        return "GlobeNewswire"
    if isinstance(source, GuessedPage):
        return source.name.removeprefix("page:")
    if source.name.startswith("guess:"):
        return "guessed URL"
    return source.name.removeprefix("rss:")


_FILING_FOLDER = re.compile(r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/")


def _filing_folder(url: str) -> str | None:
    """The SEC filing folder a link points into, or None for any other link."""
    match = _FILING_FOLDER.match(url)
    return match.group(0) if match else None


def _same_filing(a: str, b: str) -> bool:
    """Whether two SEC links point into one filing's folder: one SEC source links its index page
    and the other its main document."""
    folder = _filing_folder(a)
    return folder is not None and folder == _filing_folder(b)


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
        self._downloading: set[str] = set()
        self.attach_retry = ATTACH_RETRY_SECONDS
        self.first_seen: datetime | None = None
        self.first_seen_at = 0.0  # time.monotonic(), for timing the steps
        # The first sighting was already out when the bot started watching (Sighting.caught_up)
        self.caught_up = False
        self.posted = False
        self.has_text = False  # some source has delivered the release's text
        self.first_text_at: datetime | None = None
        # Both SEC sources list the same 8-K. These make sure its exhibit is read once: one lock
        # per filing folder, and the text each folder's exhibit gave.
        self._filing_locks: dict[str, asyncio.Lock] = {}
        self._filing_texts: dict[str, str] = {}
        self.summarizing = False
        self.lock = asyncio.Lock()  # one Discord edit at a time, in order
        self.tasks: set[asyncio.Task[None]] = set()
        # Only the summary uses estimates
        self.estimates = asyncio.create_task(self._estimates(fmp_api_key if summarizer else None))

    def add(self, source: Source, sighting: Sighting, *, measure: bool = False) -> None:
        """Called by the poller for each new item that looks like the release. Returns at once;
        the work runs in the background."""
        if self.first_seen is None:
            self.first_seen, self.first_seen_at = self.journal.now(), time.monotonic()
            self.caught_up = sighting.caught_up
        label = describe_source(source)
        is_file = urlsplit(sighting.url).path.lower().endswith(FILE_TYPES)
        self._spawn(self._link(_file_name(sighting.url) if is_file else label, sighting.url))
        if is_file:
            self._spawn(self._attach(sighting.url, sighting.body))
        elif measure or not self.has_text or (self.summarizer and not self.summarizing):
            self._spawn(self._read(source, sighting, label, measure))

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
        # Not a race won: the release was already out when the bot started watching
        late = "Already out when the bot started watching. " if self.caught_up else ""
        lines = [
            f"🚨 **${self.target.ticker} earnings are out**{self.note}",
            f"-# {late}First seen {seen} ET on the {first_label}",
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
            if url in self.links or any(_same_filing(url, known) for known in self.links):
                return
            self.links[url] = label
            try:
                if self.posted:
                    stamp = await retry_delivery(lambda: self.poster.edit(self._content()))
                    event = "linked"
                else:
                    stamp = await retry_delivery(lambda: self.poster.send(self._content()))
                    self.posted = True
                    event = "resumed" if self.poster.resumed else "posted"
            except Exception:
                del self.links[url]
                raise
        live = self._live_after(stamp)
        self.journal.write(
            event,
            ticker=self.target.ticker,
            source=label,
            seconds=self._since_first_seen(),
            live_seconds=live,
            visible=stamp,
        )
        if event == "posted":
            self.journal.say(
                f"{self.target.ticker}: posted the alert ({label}), live after {live}s"
            )
        elif event == "resumed":
            self.journal.say(f"{self.target.ticker}: resumed the existing alert")
        else:
            self.journal.say(f"{self.target.ticker}: added the {label} link, live after {live}s")

    async def _attach(self, url: str, body: bytes | None = None) -> None:
        name = _file_name(url)
        if name in self.files:
            return
        status = 200
        if body is None:
            if name in self._downloading:
                return
            self._downloading.add(name)
            try:
                response = await self._download(url, name)
                status, body = response.status, response.body
            finally:
                self._downloading.discard(name)
        if status != 200 or len(body) > MAX_FILE_BYTES:
            self.journal.say(
                f"{self.target.ticker}: couldn't attach {name} (HTTP {status}, {len(body):,} bytes)"
            )
            return
        async with self.lock:
            if name in self.files:
                return
            stamp = await self.poster.attach(name, body)
            self.files.add(name)
        self.journal.write(
            "attached",
            ticker=self.target.ticker,
            file=name,
            seconds=self._since_first_seen(),
            live_seconds=self._live_after(stamp),
            visible=stamp,
        )
        self.journal.say(f"{self.target.ticker}: attached {name}")

    async def _download(self, url: str, name: str) -> Response:
        """Gets a file. A feed can list a file before it's uploaded, so a 404 gets tried again
        until the target's window closes."""
        response = await self.fetcher.request("GET", url)
        _, end = self.target.window
        retry = timedelta(seconds=self.attach_retry)
        if response.status != 404 or self.journal.now() + retry >= end:
            return response
        self.journal.say(
            f"{self.target.ticker}: {name} isn't up yet (HTTP 404), so trying again every "
            f"{self.attach_retry:g}s until {end.astimezone(ET):%H:%M} ET"
        )
        while response.status == 404 and self.journal.now() + retry < end:
            await asyncio.sleep(self.attach_retry)
            try:
                response = await self.fetcher.request("GET", url)
            except (aiohttp.ClientError, TimeoutError):
                continue  # keep the last answer and try again
        return response

    async def _read(
        self, source: Source, sighting: Sighting, label: str, measure: bool = False
    ) -> None:
        """Gets the release's text from this sighting, logs when the first text arrives, and
        starts the summary from it. Several sources can deliver the text; the first one wins."""
        text = await self._release_text(source, sighting)
        if text is None:
            return
        if measure and not sighting.caught_up and verified_report(text):
            self.journal.write(
                "source_body",
                ticker=self.target.ticker,
                source=source.name,
                key=sighting.key,
                url=sighting.url,
            )
        if not self.has_text:
            self.has_text = True
            self.first_text_at = self.journal.now()
            self.journal.write(
                "body",
                ticker=self.target.ticker,
                source=label,
                via="poll" if sighting.body is not None else "fetch",
                seconds=self._since_first_seen(),
                chars=len(text),
            )
            self.journal.say(
                f"{self.target.ticker}: got the release text from the {label} after "
                f"{self._since_first_seen()}s"
            )
        if self.summarizer and not self.summarizing:
            await self._summarize(self.summarizer, text, label)

    async def _release_text(self, source: Source, sighting: Sighting) -> str | None:
        """release_text, except that each SEC filing's exhibit is downloaded once. When both SEC
        sources list one 8-K, the second sighting waits for the first one's read and takes its
        text. If that read got nothing, the second sighting tries again."""
        folder = _filing_folder(sighting.url) if isinstance(source, EdgarFilings) else None
        if folder is None:
            return await release_text(self.fetcher, source, sighting)
        async with self._filing_locks.setdefault(folder, asyncio.Lock()):
            if folder not in self._filing_texts:
                text = await release_text(self.fetcher, source, sighting)
                if text is None:
                    return None
                self._filing_texts[folder] = text
            return self._filing_texts[folder]

    async def _summarize(self, summarizer: Summarizer, text: str, label: str) -> None:
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
            visible=stamp,
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
    ping: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Runs the schedule and posts each release to the channel. `ping`, a cheap Discord request,
    keeps the connection open during windows (see keep_warm)."""
    with log_path.open("a", encoding="utf-8") as file:
        journal = Journal(file)
        async with Fetcher(sec_user_agent) as fetcher:
            releases = Releases(
                lambda: DiscordPoster(channel), fetcher, journal, summarizer, fmp_api_key
            )
            windows = [target.window for target in plan.target]
            warm = asyncio.create_task(keep_warm(ping, lambda: windows, journal)) if ping else None
            try:
                await record(plan, fetcher, journal, releases.on_new)
                await releases.finish()
            finally:
                if warm:
                    warm.cancel()


class Releases:
    """The releases posted while schedules are active."""

    def __init__(
        self,
        new_poster: Callable[[], Poster],
        fetcher: Fetcher,
        journal: Journal,
        summarizer: Summarizer | None,
        fmp_api_key: str | None,
        durable_poster: Callable[[Target], Poster] | None = None,
    ) -> None:
        self.new_poster = new_poster
        self.durable_poster = durable_poster
        self.fetcher = fetcher
        self.journal = journal
        self.summarizer = summarizer
        self.fmp_api_key = fmp_api_key
        self.active: dict[str, Release] = {}

    def on_new(
        self, target: Target, source: Source, sighting: Sighting, *, measure: bool = False
    ) -> None:
        """Called by the recorder for each new item any source lists."""
        if not sighting.earnings:
            return
        if source.trial and not target.trial.post:
            return  # logged by the recorder, but not posted until the source is proven
        key = target.release_key
        release = self.active.get(key)
        if release is None:
            release = self.active[key] = Release(
                target,
                self.durable_poster(target) if self.durable_poster else self.new_poster(),
                self.fetcher,
                self.journal,
                self.summarizer,
                self.fmp_api_key,
            )
        release.add(source, sighting, measure=measure)

    async def finish_target(self, target: Target) -> None:
        if release := self.active.get(target.release_key):
            await release.finish()
            del self.active[target.release_key]

    async def finish(self) -> None:
        for release in self.active.values():
            await release.finish()
