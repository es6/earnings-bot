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
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp
import discord

from earnings_bot.browser import BrowserError
from earnings_bot.browser_sources import (
    BrowserPDF,
    BrowserRelease,
    BrowserRss,
    QuarterlyTable,
    TeslaDeck,
    article_matches,
    issuer_pdf_url,
)
from earnings_bot.delivery import (
    DELIVERED,
    FAILED,
    WRITTEN,
    Delivery,
    DeliveryStore,
    SummaryJob,
    retry_delivery,
)
from earnings_bot.edit_priority import EditPriority, release_step
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.formatting import clock, period_label
from earnings_bot.issuer_news import IssuerNews
from earnings_bot.recorder import ET, Journal, Plan, PollError, Target, poll, record
from earnings_bot.release_cards import release_cards, release_content
from earnings_bot.source_document import SourceDocument
from earnings_bot.sources import (
    BroadcomNewsroom,
    EdgarFilings,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
)
from earnings_bot.summary import MODEL, WRITER, Summarizer, SummaryError
from earnings_bot.text import release_document, verified_report

FILE_TYPES = (".pdf", ".xlsx", ".xls")
# A release PDF is summarized as soon as it's read. A page reads better (its tables keep their
# cells: on BN the checker kept 13 of 21 figures from the PDF and 20 of 25 from the page), so
# a page that comes within this long of a PDF-based summary replaces it, once. The alert is
# then under an hour old, below Discord's cap on edits to old messages.
REPLACE_WITHIN = 30 * 60.0  # seconds
# After a wire alert whose article we can't read, how long and how often the issuer's newsroom
# is asked for the same release
ISSUER_TEXT_SECONDS, ISSUER_TEXT_EVERY = 180.0, 5.0
# Limits on paid summary calls. A release gets at most three model calls in all, restarts
# included; at most two run at once; and the day's calls stop at the cap. None of this delays
# an alert. Not measured: a full day has had at most a handful of releases.
MAX_SUMMARY_ATTEMPTS, MAX_MODEL_CALLS, DAILY_SUMMARY_CALLS = 3, 2, 40
# Discord's upload limit for bots in servers without boosts. Earnings PDFs are well under 1 MB.
MAX_FILE_BYTES = 10 * 1024 * 1024
UNKNOWN_MESSAGE = 10008  # Discord's error code for a missing message
MAX_CONTENT = 2000  # Discord's limit for a message's text
# Seconds between tries for a file that a feed lists before it's uploaded. Cloudflare's Q3 2026
# supplement was listed at 4:15:32 PM and went up at 4:38:11.
ATTACH_RETRY_SECONDS = 30.0
# A link or summary that Discord still refuses after retry_delivery's tries is kept and sent
# again after each of these waits, about an hour in all. On Nov 12 NBIS's summary edit failed
# with error 30046 (edits to messages over an hour old); before this, a refused link was
# dropped and a refused summary waited for a restart. A summary job stays "written" until it
# lands, so a restart still posts it.
REDELIVERY_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0, 120.0) + (300.0,) * 11
DISCORD_ERRORS = (discord.HTTPException, aiohttp.ClientError, TimeoutError)
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
    first_seen: datetime | None = None

    @abstractmethod
    async def send(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def edit(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def attach(self, name: str, data: bytes) -> datetime | None: ...

    @abstractmethod
    async def set_summary(self, text: str, footer: str) -> datetime | None: ...


def discord_filename(name: str) -> str:
    """The name a file goes up under: letters, digits, "." "-" and "_" only, which Discord
    keeps as they are. Discord drops other characters, so a resumed alert couldn't recognize
    its own file and attached it again: Nebius's Q3 2026 alert had its PDF three times (read
    back from Discord on Nov 12)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(name)).strip("_") or "file"


class DiscordPoster(Poster):
    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self.nonce = secrets.token_hex(12)
        self.message: discord.Message | None = None

    def _sent(self) -> discord.Message:
        if self.message is None:
            raise RuntimeError("send() must come before any edit")
        return self.message

    def _payload(self, content: str, summary: tuple[str, str] | None = None) -> dict[str, Any]:
        cards = release_cards(
            content, previous=self.message, first_seen=self.first_seen, summary=summary
        )
        if cards is None:
            return {"content": content}
        # Keep the ticker/quarter in push notifications, without duplicating all the links.
        return {"content": content.splitlines()[0], "embeds": cards}

    async def send(self, content: str) -> datetime | None:
        self.message = await self.channel.send(**self._payload(content), nonce=self.nonce)
        return self.message.created_at

    async def edit(self, content: str) -> datetime | None:
        self.message = await self._sent().edit(**self._payload(content))
        return self.message.edited_at

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        file = discord.File(io.BytesIO(data), filename=discord_filename(name))
        self.message = await message.edit(attachments=[*message.attachments, file])
        return self.message.edited_at

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        payload = self._payload(release_content(self._sent()), (text, footer))
        if "embeds" in payload:
            self.message = await self._sent().edit(**payload)
            return self.message.edited_at
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
        self.message = await self.channel.send(
            **self._payload(saved.content), nonce=self._nonce(saved)
        )
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
                    release_content(cached)
                    if cached and cached.id == saved.message_id
                    else saved.content
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
                if (
                    candidate.author.id == self.author_id
                    and release_content(candidate) == saved.content
                ):
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
        existing = release_content(message)
        if content == existing:
            return created
        merged = self._merge_content(existing, content)
        if merged != existing:
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
        urls = [_line_url(line) for line in lines[2:]]
        for line in content.splitlines()[2:]:
            url = _line_url(line)
            if url not in urls and not any(_same_release_link(url, known) for known in urls):
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
        existing = release_content(message)
        merged = self._merge_content(existing, content)
        if merged == existing:
            return message.edited_at
        return await self._update(partial(super().edit, merged), merged)

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        # The forms Discord may have stored it under, an upload from before discord_filename
        # included: it drops "%", so "Nebius%20Q3...pdf" became "Nebius20Q3...pdf"
        stored = {name, discord_filename(name), name.replace("%", "")}
        if any(attachment.filename in stored for attachment in message.attachments):
            return message.edited_at
        return await self._update(
            partial(super().attach, name, data),
            release_content(message),
            retry_after_replacement=True,
        )

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        return await self._update(
            partial(super().set_summary, text, footer),
            release_content(self._sent()),
            retry_after_replacement=True,
        )


def describe_source(source: Source) -> str:
    if isinstance(source, BrowserRss):
        return "IR press release feed"
    if isinstance(source, Q4PressReleases | Q4FinancialReports | BrowserRelease | IssuerNews):
        return "IR site"
    if isinstance(source, EdgarFilings):
        return f"SEC {source.form}"
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
# A link line's URL, in an alert's `[label](<url>)` lines or the older `label: <url>` ones
_PRN_EDITION = re.compile(r"https://www\.prnewswire\.com/news-releases/([^/?#]+)-\d{9}\.html")
_LINE_URL = re.compile(r"<(https?://[^>\s]+)>")


def _line_url(line: str) -> str:
    urls = _LINE_URL.findall(line)
    return urls[-1] if urls else line


def _icon(label: str) -> str:
    if label.startswith("SEC "):
        return "🏛️"
    if label == "IR site":
        return "🌐"
    if label in ("PR Newswire", "Business Wire", "GlobeNewswire"):
        return "📰"
    if urlsplit("https://x/" + label).path.lower().endswith(FILE_TYPES):
        return "📄"
    return "🔗"


def _filing_folder(url: str) -> str | None:
    """The SEC filing folder a link points into, or None for any other link."""
    match = _FILING_FOLDER.match(url)
    return match.group(0) if match else None


def _same_release_link(a: str, b: str) -> bool:
    """Collapse alternate links to the same SEC filing or PR Newswire headline."""
    folder = _filing_folder(a)
    if folder is not None:
        return folder == _filing_folder(b)
    first, second = _PRN_EDITION.fullmatch(a), _PRN_EDITION.fullmatch(b)
    return bool(first and second and first[1] == second[1])


def _file_name(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


class ReleaseText:
    """Share successful reads of a filing or feed article within one release window."""

    def __init__(self, fetcher: Fetcher, *, verified_only: bool = False) -> None:
        self.fetcher = fetcher
        self.verified_only = verified_only
        self._locks: dict[str, asyncio.Lock] = {}
        self._documents: dict[str, SourceDocument] = {}

    async def read(self, source: Source, sighting: Sighting) -> str | None:
        document = await self.read_document(source, sighting)
        return document.text if document else None

    async def read_document(self, source: Source, sighting: Sighting) -> SourceDocument | None:
        key = _filing_folder(sighting.url) if isinstance(source, EdgarFilings) else None
        if isinstance(source, RssFeed):
            key = sighting.url
        if key is None:
            return await self._fetch(source, sighting)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key not in self._documents:
                document = await self._fetch(source, sighting)
                if document is None:
                    return None  # Another sighting can retry the miss.
                self._documents[key] = document
            return self._documents[key]

    async def _fetch(self, source: Source, sighting: Sighting) -> SourceDocument | None:
        document = await release_document(self.fetcher, source, sighting)
        requires_report = (
            self.verified_only
            or isinstance(source, BrowserRss)
            or (isinstance(source, BrowserRelease) and source.verify_text)
        )
        return (
            document
            if document and (not requires_report or verified_report(document.text))
            else None
        )


class Release:
    """Everything known about one company's release, and its Discord post."""

    def __init__(
        self,
        target: Target,
        poster: Poster,
        fetcher: Fetcher,
        journal: Journal,
        summarizer: Summarizer | None,
        note: str = "",
        priority: EditPriority | None = None,
        jobs: DeliveryStore | None = None,
        model_calls: asyncio.Semaphore | None = None,
        issuer_context: Callable[[], dict[str, Any]] = dict,
    ) -> None:
        self.target = target
        self.poster = poster
        self.fetcher = fetcher
        self.journal = journal
        self.summarizer = summarizer
        self.issuer_context = issuer_context
        self.note = note
        # The summary job kept across restarts, and the model calls shared by all releases
        self.jobs = jobs
        self.model_calls = model_calls or asyncio.Semaphore(MAX_MODEL_CALLS)
        # Scorecard edits wait while this release has a post, edit or summary on the way
        self.priority = priority
        self.links: dict[str, str] = {}  # url -> label, in the order they turned up
        self.files: set[str] = set()
        self._downloading: set[str] = set()
        self.attach_retry = ATTACH_RETRY_SECONDS
        self.issuer_text_seconds, self.issuer_text_every = ISSUER_TEXT_SECONDS, ISSUER_TEXT_EVERY
        self._pdfs: dict[str, bytes] = {}  # attached PDFs by name, for reading their text
        self._pdf_read: set[str] = set()
        self._asked_issuer = False
        self.first_seen: datetime | None = None
        self.first_seen_at = 0.0  # time.monotonic(), for timing the steps
        # The first sighting was already out when the bot started watching (Sighting.caught_up)
        self.caught_up = False
        self.posted = False
        self.has_text = False  # some source has delivered the release's text
        self.first_text_at: datetime | None = None
        self._text = ReleaseText(fetcher)
        self.summarizing = False
        # What the summary was written from: "pdf" or "page", its source, and when it began
        self.summary_kind: str | None = None
        self.summary_label = ""
        self.summary_started = 0.0  # time.monotonic()
        self.replaced = False  # a page has replaced a PDF-based summary
        self.summary_posted = False
        self._summary_turn = asyncio.Lock()  # a replacement waits for the summary it replaces
        self.lock = asyncio.Lock()  # one Discord edit at a time, in order
        self.tasks: set[asyncio.Task[None]] = set()
        # What Discord refused, kept to send again: the labels of links not yet on the alert,
        # and the latest summary (text, footer, source, journal details)
        self.redelivery = REDELIVERY_SECONDS
        self._refused_links: list[str] = []
        self._refused_summary: tuple[str, str, str, dict[str, Any]] | None = None
        self._redelivering = False

    def add(self, source: Source, sighting: Sighting, *, measure: bool = False) -> None:
        """Called by the poller for each new item that looks like the release. Returns at once;
        the work runs in the background."""
        if self.first_seen is None:
            self.first_seen, self.first_seen_at = self.journal.now(), time.monotonic()
            self.caught_up = sighting.caught_up
        label = describe_source(source)
        is_file = isinstance(source, BrowserPDF | TeslaDeck) or urlsplit(
            sighting.url
        ).path.lower().endswith(FILE_TYPES)
        if is_file:
            label = self._file_label(sighting.url)
        self._spawn(self._link(label, sighting.url))
        if is_file:
            self._spawn(self._attach(sighting.url, sighting.body))
            if urlsplit(sighting.url).path.lower().endswith(".pdf") and not isinstance(
                source, TeslaDeck
            ):
                self._spawn(self._read_pdf(source, sighting, label))
        elif isinstance(source, BrowserRss):
            self._spawn(self._read_browser_feed(source, sighting, label, measure))
        else:
            if isinstance(source, BrowserRelease) and sighting.body is not None:
                self._spawn(self._browser_attachment(source, sighting, measure))
            if (
                measure
                or not self.has_text
                or (self.summarizer and not self.summarizing)
                or self._page_can_replace()
            ):
                self._spawn(self._read(source, sighting, label, measure))

    def _page_can_replace(self) -> bool:
        """Whether a page's text would still replace the summary: one written from a PDF, not
        yet replaced, begun under REPLACE_WITHIN ago."""
        return (
            self.summarizer is not None
            and self.summary_kind == "pdf"
            and not self.replaced
            and time.monotonic() - self.summary_started < REPLACE_WITHIN
        )

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
        if stamp is None or self.first_seen is None or stamp < self.first_seen:
            # A stamp from before the sighting belongs to an alert resumed after a restart that
            # already had this change, so nothing went live now. It logged "live after
            # -15761.29s" on Nov 12.
            return None
        return round((stamp - self.first_seen).total_seconds(), 2)

    def _content(self) -> str:
        seen = clock(self.first_seen, millis=True) if self.first_seen else ""
        first_label = next(iter(self.links.values()), "")
        period = period_label(self.target.period)
        # Not a race won: the release was already out when the bot started watching
        late = "Already out when the bot started watching · " if self.caught_up else ""
        quarter = f" {period}" if period else ""
        lines = [
            f"🚨 **${self.target.ticker}{quarter} earnings are out**{self.note}",
            f"-# {late}First seen {seen} via {first_label}",
        ]
        # Masked links in angle brackets, so Discord doesn't add a preview of each page
        lines += [
            f"{_icon(label)} [{discord.utils.escape_markdown(label)}](<{url}>)"
            for url, label in self.links.items()
        ]
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

    async def _link(self, label: str, url: str) -> None:
        async with release_step(self.priority), self.lock:
            if url in self.links or any(_same_release_link(url, known) for known in self.links):
                return
            self.links[url] = label
            try:
                stamp, event = await self._deliver_alert()
            except DISCORD_ERRORS as error:
                # Kept: every send or edit carries all the links, so the next one includes it
                self._refused_links.append(label)
                self._refused("alert", error)
                return
            except Exception:
                del self.links[url]
                raise
            labels, self._refused_links = [*self._refused_links, label], []
        self._log_alert(event, labels, stamp)

    async def _deliver_alert(self) -> tuple[datetime | None, str]:
        """Sends the alert, or edits it, with every link so far. The caller holds the lock."""
        if self.posted:
            return await retry_delivery(lambda: self.poster.edit(self._content())), "linked"
        self.poster.first_seen = self.first_seen
        stamp = await retry_delivery(lambda: self.poster.send(self._content()))
        self.posted = True
        return stamp, "resumed" if self.poster.resumed else "posted"

    def _refused(self, step: str, error: Exception) -> None:
        self.journal.write(
            "delivery_refused", ticker=self.target.ticker, step=step, message=repr(error)
        )
        self.journal.say(f"{self.target.ticker}: Discord refused the {step}: {error!r}")
        self._redeliver_soon()

    def _redeliver_soon(self) -> None:
        if not self._redelivering:
            self._redelivering = True
            self._spawn(self._redeliver())

    async def _redeliver(self) -> None:
        """Sends what Discord refused again after each wait in `redelivery`, until it lands.
        Each try sends the whole alert, so it carries every link refused so far."""
        try:
            for attempt, wait in enumerate(self.redelivery, 1):
                await asyncio.sleep(wait)
                try:
                    if await self._deliver_refused():
                        return
                except DISCORD_ERRORS as error:
                    self.journal.write(
                        "delivery_refused",
                        ticker=self.target.ticker,
                        step="retry",
                        attempt=attempt,
                        message=repr(error),
                    )
            self.journal.write(
                "delivery_failed",
                ticker=self.target.ticker,
                links=self._refused_links,
                summary=self._refused_summary is not None,
            )
            self.journal.say(
                f"{self.target.ticker}: Discord kept refusing the alert's update, so it stopped "
                f"trying after {sum(self.redelivery):.0f}s"
            )
        finally:
            self._redelivering = False

    async def _deliver_refused(self) -> bool:
        """One more try at what was refused. Returns whether nothing refused is left."""
        async with release_step(self.priority), self.lock:
            if self._refused_links:
                stamp, event = await self._deliver_alert()
                labels, self._refused_links = self._refused_links, []
                self._log_alert(event, labels, stamp)
            if (refused := self._refused_summary) and self.posted:
                text, footer, label, details = refused
                stamp = await retry_delivery(lambda: self.poster.set_summary(text, footer))
                self._refused_summary = None
                self._summary_delivered(stamp, text, label, details)
            return not self._refused_links and self._refused_summary is None

    def _log_alert(self, event: str, labels: list[str], stamp: datetime | None) -> None:
        """Logs one alert step for each link it delivered: the first is what posted the alert
        (or the new link), and links refused before it went with it."""
        for index, label in enumerate(labels):
            self._log_step(event if index == 0 else "linked", label, stamp)

    def _log_step(self, event: str, label: str, stamp: datetime | None) -> None:
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
            when = f", live after {live}s" if live is not None else ""
            self.journal.say(f"{self.target.ticker}: added the {label} link{when}")

    def _attachment_name(self, url: str) -> str:
        name = _file_name(url)
        if name.lower() == "pdf":
            period = self.target.period or self.target.expected.astimezone(ET).date()
            return f"{self.target.ticker}-{period}-results.pdf"
        return name

    def _file_label(self, url: str) -> str:
        """A file's name as the alert shows it, without URL escapes: Nebius's Q3 2026 PDF read
        "Nebius%20Q3%202026%20Financial%20Results%20Press%20Release.pdf". The attachment keeps
        its name as before, so an alert resumed after a restart still recognizes its file."""
        return unquote(self._attachment_name(url))

    async def _read_browser_feed(
        self, source: BrowserRss, sighting: Sighting, label: str, measure: bool
    ) -> None:
        # An item can appear shortly before its article. Give the next paced slot to the
        # article once before leaving the other sources to supply the body.
        for attempt in range(2):
            try:
                response = await self.fetcher.browser_request(
                    sighting.url, source.warmup, source.timeout, urgent=True
                )
            except (aiohttp.ClientError, BrowserError, TimeoutError):
                if attempt:
                    raise
                continue
            if response.status == 200 and await asyncio.to_thread(
                article_matches,
                sighting.title,
                response.body,
                verify_text=True,
                article=sighting.url,
            ):
                read = replace(sighting, body=response.body)
                self._spawn(self._browser_attachment(source, read, measure))
                await self._read(source, read, label, measure)
                return
            if attempt or response.status not in (0, 200, 404):
                self.journal.write(
                    "browser_article_unavailable",
                    ticker=self.target.ticker,
                    url=sighting.url,
                    status=response.status,
                )
                return

    async def _browser_attachment(
        self, source: BrowserRelease | BrowserRss, sighting: Sighting, measure: bool
    ) -> None:
        if sighting.body is None:
            return
        url = await asyncio.to_thread(
            issuer_pdf_url, sighting.url, sighting.body, title=sighting.title
        )
        if url is None:
            return
        name = self._attachment_name(url)
        if name in self.files or name in self._downloading:
            return
        self._downloading.add(name)
        try:
            warmup = source.warmup if isinstance(source, BrowserRss) else source.listing
            if urlsplit(url).netloc == urlsplit(warmup).netloc:
                if isinstance(source, BrowserRelease):
                    get = partial(source.request_page, self.fetcher, url, urgent=True)
                else:
                    get = partial(
                        self.fetcher.browser_request, url, warmup, source.timeout, urgent=True
                    )
            else:
                get = partial(
                    self.fetcher.request,
                    "GET",
                    url,
                    give_up_after=source.timeout,
                    urgent=True,
                    allow_redirects=False,
                )
            response = await self._download(
                name,
                get,
                pending_statuses=(0, 404),  # Chrome stops at an opaque redirect with status 0.
            )
        finally:
            self._downloading.discard(name)
        if response.status == 200 and response.body.startswith(b"%PDF-"):
            if measure and not sighting.caught_up:
                self.journal.write(
                    "source_body",
                    ticker=self.target.ticker,
                    source=source.name,
                    key=sighting.key,
                    url=url,
                    format="pdf",
                )
            await self._link(self._file_label(url), url)
            await self._attach(url, response.body)
        elif name not in self.files:
            self.journal.write(
                "attachment_unavailable",
                ticker=self.target.ticker,
                file=name,
                status=response.status,
            )

    async def _attach(self, url: str, body: bytes | None = None) -> None:
        name = self._attachment_name(url)
        if name in self.files:
            return
        status = 200
        if body is None:
            if name in self._downloading:
                return
            self._downloading.add(name)
            try:
                response = await self._download(name, partial(self.fetcher.request, "GET", url))
                status, body = response.status, response.body
            finally:
                self._downloading.discard(name)
        if status == 200 and body.startswith(b"%PDF-"):
            self._pdfs.setdefault(name, body)
        if status != 200 or len(body) > MAX_FILE_BYTES:
            self.journal.say(
                f"{self.target.ticker}: couldn't attach {name} (HTTP {status}, {len(body):,} bytes)"
            )
            return
        async with release_step(self.priority), self.lock:
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

    async def _download(
        self,
        name: str,
        request: Callable[[], Awaitable[Response]],
        *,
        pending_statuses: tuple[int, ...] = (404,),
    ) -> Response:
        """Get a file and retry when it has yet to appear during this window."""
        response = await request()
        _, end = self.target.window
        retry = timedelta(seconds=self.attach_retry)
        if response.status not in pending_statuses or self.journal.now() + retry >= end:
            return response
        status = f"HTTP {response.status}" if response.status else "redirect"
        self.journal.say(
            f"{self.target.ticker}: {name} isn't up yet ({status}), so trying again every "
            f"{self.attach_retry:g}s until {end.astimezone(ET):%H:%M} ET"
        )
        while response.status in pending_statuses and self.journal.now() + retry < end:
            await asyncio.sleep(self.attach_retry)
            if name in self.files:
                break
            try:
                response = await request()
            except (aiohttp.ClientError, BrowserError, TimeoutError):
                continue  # keep the last answer and try again
        return response

    async def _read(
        self, source: Source, sighting: Sighting, label: str, measure: bool = False
    ) -> None:
        """Gets the release's text from this sighting, logs when the first text arrives, and
        starts the summary from it. Several sources can deliver the text; the first one wins."""
        document = await self._text.read_document(source, sighting)
        if document is None:
            if isinstance(source, RssFeed) and sighting.earnings:
                self._ask_issuer()
            return
        text = document.text
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
        if not self.summarizer:
            return
        kind = "pdf" if sighting.body is not None and sighting.body.startswith(b"%PDF-") else "page"
        if not self.summarizing:
            await self._summarize(self.summarizer, document, label, kind)
        elif kind == "page" and self._page_can_replace():
            self.replaced = True  # once: later pages don't replace it again
            async with self._summary_turn:  # after the PDF-based summary is posted
                await self._summarize(
                    self.summarizer, document, label, kind, replaces=self.summary_label
                )

    async def _read_pdf(self, source: Source, sighting: Sighting, label: str) -> None:
        """Reads the release from its PDF as soon as it's downloaded. Brookfield's Q3 2026
        summary waited 4 minutes for the 6-K with the PDF already posted, and the 10 s grace
        for a page that followed cost every PDF-first release 10 s."""
        name = self._attachment_name(sighting.url)
        if name in self._pdf_read:
            return
        self._pdf_read.add(name)
        while name in self._downloading:  # noqa: ASYNC110
            await asyncio.sleep(0.2)
        body = sighting.body or self._pdfs.get(name)
        done = self.has_text and (self.summarizer is None or self.summarizing)
        if done or body is None or not body.startswith(b"%PDF-"):
            return  # a page's summary is never replaced by a PDF's
        await self._read(source, replace(sighting, body=body), label)

    def _ask_issuer(self) -> None:
        ir = self.target.ir
        if self._asked_issuer or self.has_text or ir is None or not (ir.listing and ir.headline):
            return
        self._asked_issuer = True
        self._spawn(self._issuer_article())

    async def _issuer_article(self) -> None:
        """A wire's own article page can be closed to us, as Business Wire's is. The issuer's
        newsroom carries the same release, so it is read from there, whatever that route's own
        watcher is doing: Nebius's Q3 2026 summary waited 2h 7m for the 6-K."""
        assert self.target.ir is not None
        day = self.target.expected.astimezone(ET).date()
        source = next(
            (
                found
                for found in self.target.ir.sources(day, self.target.period)
                if isinstance(found, BrowserRelease) and not isinstance(found, QuarterlyTable)
            ),
            None,
        )
        if source is None:
            return
        self.journal.write("issuer_text_search", ticker=self.target.ticker, listing=source.listing)
        deadline = time.monotonic() + self.issuer_text_seconds
        # Shared with the route's own watcher, so neither asks while the newsroom refuses us
        pause = self.fetcher.route_pause(source.pause_route)
        while not self.has_text and (left := deadline - time.monotonic()) > 0:
            if (wait := pause.left()) > 0:
                await asyncio.sleep(min(wait, left))
                continue
            try:
                _, items = await poll(source, self.fetcher)
            except PollError as error:
                # A 403 doubles the shared pause; a 429 or 503 waits out its Retry-After
                retry_in = pause.refused() if error.blocked else error.pause
                pause.hold(retry_in)
                self.journal.write(
                    "issuer_text_error",
                    ticker=self.target.ticker,
                    message=str(error),
                    retry_in=retry_in,
                )
                items = []
            else:
                pause.answered()
            if items:
                self.journal.say(
                    f"{self.target.ticker}: the wire's article can't be read, so reading the "
                    f"release from {source.listing}"
                )
                self.add(source, items[0])
                return
            await asyncio.sleep(self.issuer_text_every)

    async def _summarize(
        self,
        summarizer: Summarizer,
        document: SourceDocument,
        label: str,
        kind: str = "page",
        replaces: str | None = None,
    ) -> None:
        """Summarizes from this text: the release's first summary, or with `replaces`, the one
        page-based replacement of a PDF-based summary (the caller holds _summary_turn)."""
        self.summarizing = True
        job = self.jobs.summary(self.target.release_key) if self.jobs else None
        if job and job.status == DELIVERED and replaces is None:
            # After a restart or a schedule change: Nov 12's restart re-summarized both
            # delivered releases, two paid calls and two edits to hours-old alerts
            self.journal.write(
                "summary_skipped", ticker=self.target.ticker, reason="already delivered"
            )
            return
        if replaces is not None:
            await self._summarize_now(summarizer, document, label, kind, job, replaces)
            return
        async with self._summary_turn:
            await self._summarize_now(summarizer, document, label, kind, job, None)

    async def _summarize_now(
        self,
        summarizer: Summarizer,
        document: SourceDocument,
        label: str,
        kind: str,
        job: SummaryJob | None,
        replaces: str | None,
    ) -> None:
        # The alert waits on this edit from the moment its text arrives
        async with release_step(self.priority):
            if job and job.status == WRITTEN and job.text is not None and replaces is None:
                # Written before a restart and never posted: post it, with no new call
                await self._post_summary(job.text, job.footer or "", job.source, reused=True)
                return
            if why := self._summary_limit(job):
                self.journal.write("summary_skipped", ticker=self.target.ticker, reason=why)
                self.journal.say(f"{self.target.ticker}: no summary: {why}")
                return
            if replaces is not None and not self.summary_posted:
                replaces = None  # the PDF-based summary failed, so nothing is replaced
            self.summary_kind, self.summary_label = kind, label
            self.summary_started = time.monotonic()
            await self._write_summary(summarizer, document, label, replaces)

    def _summary_limit(self, job: SummaryJob | None) -> str | None:
        """Why no model call may start for this release now, if one may not."""
        if job and job.attempts >= MAX_SUMMARY_ATTEMPTS:
            return f"{job.attempts} model calls already made for this release"
        if self.jobs and self.jobs.summary_calls(self._day()) >= DAILY_SUMMARY_CALLS:
            return f"the day's {DAILY_SUMMARY_CALLS} summary calls are used up"
        return None

    def _day(self) -> str:
        return self.journal.now().astimezone(ET).date().isoformat()

    async def _write_summary(
        self,
        summarizer: Summarizer,
        document: SourceDocument,
        label: str,
        replaces: str | None = None,
    ) -> None:
        key = self.target.release_key
        async with self.model_calls:
            if self.jobs:
                evidence = await asyncio.to_thread(document.to_json)
                self.jobs.summary_started(
                    key,
                    document.sha256,
                    label,
                    self._day(),
                    replaces=replaces,
                    source_document=evidence,
                )
            started = time.monotonic()
            try:
                context = self.issuer_context()
                summary = (
                    await summarizer.write_document(self.target.ticker, document, context=context)
                    if context
                    else await summarizer.write_document(self.target.ticker, document)
                )
            except SummaryError as error:
                if self.jobs:
                    self.jobs.summary_status(key, FAILED)
                self.summarizing = False  # let a later source try
                self.journal.write("summary_error", ticker=self.target.ticker, message=str(error))
                self.journal.say(f"{self.target.ticker}: summary failed: {error}")
                return
        took = time.monotonic() - started
        footer = f"Summary by {WRITER} in {took:.0f}s from the {label} text"
        if summary.unverified:
            held = len(summary.unverified)
            figures = "figure" if held == 1 else "figures"
            footer += f" · {held} {figures} held back: the release didn't confirm them"
        footer = footer[:2048]
        if self.jobs:
            self.jobs.summary_written(
                key,
                summary.text,
                footer,
                summary.unverified,
                summary.input_tokens,
                summary.output_tokens,
                evidence=summary.evidence,
            )
        await self._post_summary(
            summary.text,
            footer,
            label,
            model_seconds=round(took, 1),
            held_back=summary.unverified,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            document_sha256=document.sha256,
            validation="source-references/1" if summary.evidence else "legacy",
            **({"replaces": replaces} if replaces else {}),
        )

    async def _post_summary(self, text: str, footer: str, label: str, **details: Any) -> None:
        async with self.lock:
            if not self.posted:
                # Discord refused the alert itself: the summary goes on once the alert is up
                self._refused_summary = (text, footer, label, details)
                self._redeliver_soon()
                return
            try:
                stamp = await retry_delivery(lambda: self.poster.set_summary(text, footer))
            except DISCORD_ERRORS as error:
                # Kept, the latest one: a page-based replacement supersedes a refused summary
                self._refused_summary = (text, footer, label, details)
                self._refused("summary", error)
                return
            self._refused_summary = None
        self._summary_delivered(stamp, text, label, details)

    def _summary_delivered(
        self, stamp: datetime | None, text: str, label: str, details: dict[str, Any]
    ) -> None:
        self.summary_posted = True
        if self.jobs:
            self.jobs.summary_status(self.target.release_key, DELIVERED)
        self.journal.write(
            "summary",
            ticker=self.target.ticker,
            seconds=self._since_first_seen(),
            live_seconds=self._live_after(stamp),
            visible=stamp,
            model=MODEL,
            source=label,
            text=text,
            **details,
        )
        self.journal.say(f"{self.target.ticker}: summary posted after {self._since_first_seen()}s")


async def watch_schedule(
    plan: Plan,
    log_path: Path,
    channel: discord.abc.Messageable,
    sec_user_agent: str,
    summarizer: Summarizer | None,
    ping: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Runs the schedule and posts each release to the channel. `ping`, a cheap Discord request,
    keeps the connection open during windows (see keep_warm)."""
    with log_path.open("a", encoding="utf-8") as file:
        journal = Journal(file)
        async with Fetcher(sec_user_agent) as fetcher:
            releases = Releases(lambda: DiscordPoster(channel), fetcher, journal, summarizer)
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
        durable_poster: Callable[[Target], Poster] | None = None,
        priority: EditPriority | None = None,
        store: DeliveryStore | None = None,
    ) -> None:
        self.new_poster = new_poster
        self.durable_poster = durable_poster
        self.priority = priority
        self.fetcher = fetcher
        self.journal = journal
        self.summarizer = summarizer
        self.store = store  # keeps each release's summary job across restarts
        self.model_calls = asyncio.Semaphore(MAX_MODEL_CALLS)
        self.active: dict[str, Release] = {}
        self.issuer_context: Callable[[Target], dict[str, Any]] = lambda target: {}

    def on_new(
        self, target: Target, source: Source, sighting: Sighting, *, measure: bool = False
    ) -> None:
        """Called by the recorder for each new item any source lists."""
        if not sighting.earnings:
            return
        key = target.release_key
        release = self.active.get(key)
        if release is None:
            release = self.active[key] = Release(
                target,
                self.durable_poster(target) if self.durable_poster else self.new_poster(),
                self.fetcher,
                self.journal,
                self.summarizer,
                priority=self.priority,
                jobs=self.store,
                model_calls=self.model_calls,
                issuer_context=lambda: self.issuer_context(target),
            )
        release.add(source, sighting, measure=measure)

    async def finish_target(self, target: Target) -> None:
        if release := self.active.get(target.release_key):
            await release.finish()
            del self.active[target.release_key]

    async def finish(self) -> None:
        for release in self.active.values():
            await release.finish()
