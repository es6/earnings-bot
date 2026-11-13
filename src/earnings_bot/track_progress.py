"""Edit one durable Discord scorecard for each tracked issuer, as the bot researches it."""

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
import discord

from earnings_bot.calendar import Calendar, Tracked, evidence_text
from earnings_bot.date_discovery import CHECKING_CACHE, READING_IR, RECHECKING, VERIFYING_IR
from earnings_bot.delivery import DeliveryStore
from earnings_bot.edit_priority import CardDeferred, EditPriority
from earnings_bot.formatting import (
    AMBER,
    BLUE,
    GREEN,
    GREY,
    RED,
    bullets,
    clock,
    duration,
    period_label,
    source_label,
    stamp,
    truncate,
    watch_list,
)
from earnings_bot.investigate import Report, due
from earnings_bot.ir_research import issuer_listing
from earnings_bot.known import KnownCompany, known_companies, to_target
from earnings_bot.pipeline import UNKNOWN_MESSAGE
from earnings_bot.recorder import ET, Target
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.version import release_version

log = logging.getLogger(__name__)
REFRESH = timedelta(days=1)
# Scorecards share a channel's edit budget (Discord allows about 5 edits per 5 s) with release
# links and summaries. Spacing card edits leaves most of it free: on Nov 11 the logs had 283
# rate-limited edits, nearly all scorecards re-sent together after a restart or a window opening.
CARD_EDIT_SPACING = 2.5  # seconds
# Discord caps edits to messages more than an hour old (error 30046), across the channel, and a
# resumed alert's links and summary count against the same cap. A card older than this is edited
# only to show a settled result, one card at a time, OLD_CARD_SPACING apart. After the Nov 12
# restart, old cards edited 2.5 s apart got 429 with a retry time of about 5.5 s for minutes.
# The spacing is about five times that; Discord doesn't publish the cap, so it isn't measured.
OLD_MESSAGE = timedelta(minutes=55)
OLD_CARD_SPACING = 30.0  # seconds
# After an old card's edit is refused or times out, old cards wait this long, doubling with each
# refusal in a row, or Discord's own retry time if that's longer
CARD_BACKOFF, CARD_BACKOFF_MAX = 60.0, 900.0  # seconds
# discord.py retries a 429 itself, up to five times. Past this, a card request gives up.
CARD_REQUEST_SECONDS = 15.0
REPLACE_AFTER = 3  # refused edits of one old card in a row before it's posted as a new message

DONE, WORKING, WAITING, FAILED, TODO = "✅", "⏳", "⏸️", "⚠️", "▫️"
# A scorecard's state as one dot, for lists such as /upcoming
READY, RESEARCHING, NOT_READY, PROBLEM = "🟢", "🔵", "🟡", "🔴"


def _card_body(card: discord.Embed) -> dict[str, object]:
    """What a card says. Not its update time, or its footer, which names the bot's version: a
    deploy alone doesn't edit every card."""
    data = dict(card.to_dict())
    data.pop("timestamp", None)
    data.pop("footer", None)
    return data


def _same_card(shown: discord.Embed, embed: discord.Embed) -> bool:
    """The message already shows this card; only its update time or version can differ."""
    return _card_body(shown) == _card_body(embed)


def _retry_after(error: Exception) -> float | None:
    """Seconds Discord asked us to wait, for a refused edit (429, including error 30046) or one
    that ran out of time; None for any other failure."""
    if isinstance(error, TimeoutError):
        return 0.0
    if not isinstance(error, discord.HTTPException) or error.status != 429:
        return None
    headers = getattr(error.response, "headers", None) or {}
    try:
        return float(headers.get("Retry-After", 0))
    except (TypeError, ValueError):
        return 0.0


async def _no_pause() -> None:
    pass


@dataclass(eq=False)
class _Pending:
    """The latest content of a card that differs from what Discord shows."""

    embed: discord.Embed
    research: bool  # the card shows research, so an old card waits for it to settle


class CardPoster:
    """One message holding an embed, sent once and then edited. Its ID is kept in the delivery
    store, so a restart edits the same message, and only if it shows something else."""

    def __init__(
        self,
        channel: discord.abc.Messageable,
        store: DeliveryStore,
        key: str,
        author_id: int,
        pace: Callable[[], Awaitable[None]] = _no_pause,
    ) -> None:
        self.channel, self.store, self.key, self.author_id = channel, store, key, author_id
        self.pace = pace

    async def show(self, embed: discord.Embed) -> str:
        """Sends or edits the card. Returns "sent", "edited", or "unchanged" when the message
        already showed it."""
        title = embed.title or ""
        saved = self.store.get(self.key)
        message = None
        if saved is not None and saved.message_id is not None:
            try:
                message = await self.channel.fetch_message(saved.message_id)
            except discord.NotFound as error:
                if error.code != UNKNOWN_MESSAGE:
                    raise
                self.store.forget(self.key)
                saved = None
        if saved is None:
            saved = self.store.prepare(self.key, title)
        elif message is None:
            # Sent before a crash, before its ID was saved
            after = saved.prepared - timedelta(seconds=5)
            async for candidate in self.channel.history(limit=50, after=after):
                if candidate.author.id == self.author_id and any(
                    shown.title == saved.content for shown in candidate.embeds
                ):
                    message = candidate
                    break
        if message is None:
            intent = f"{self.store.channel_id}:{self.key}:{saved.prepared.isoformat()}"
            nonce = hashlib.blake2s(intent.encode(), digest_size=12).hexdigest()
            await self.pace()
            message = await self.channel.send(embed=embed, nonce=nonce)
            outcome = "sent"
        elif (
            not message.content
            and len(message.embeds) == 1
            and _same_card(message.embeds[0], embed)
        ):
            # A restart re-renders every card; one that still reads the same is left alone
            outcome = "unchanged"
        else:
            await self.pace()
            await message.edit(content=None, embed=embed)
            outcome = "edited"
        self.store.delivered(self.key, message.id)
        return outcome

    async def replace(self, embed: discord.Embed) -> None:
        """Posts the card as a new message, then deletes the old one, for a card whose edits
        Discord keeps refusing. A new message's edits are free of the cap on old ones."""
        saved = self.store.get(self.key)
        old = saved.message_id if saved else None
        self.store.forget(self.key)
        await self.show(embed)
        if old is not None:
            try:
                await (await self.channel.fetch_message(old)).delete()
            except (discord.HTTPException, aiohttp.ClientError, TimeoutError) as error:
                log.warning("Couldn't delete the replaced scorecard %s: %s", old, error)


def _last_release_field(speed: Speed) -> tuple[str, str]:
    """Which source had the last results first, and how far behind the others were."""
    field = "⏱️ Last release"
    if speed.day is None:
        return field, "Couldn't date its last results release."
    basis, timings = speed.fastest()
    day = f"{speed.day:%b} {speed.day.day}, {speed.day.year}"
    if not timings:
        return field, f"No timings recorded for its {day} release."
    by_label: dict[str, Timing] = {}
    for timing in timings:
        by_label.setdefault(source_label(timing.source, speed.form), timing)
    first, *rest = by_label.items()
    label, earliest = first
    at = earliest.at
    when = clock(at, seconds=not earliest.resolution_seconds)
    lead = (rest[0][1].at - at).total_seconds() if rest else None
    if lead is not None and (lead == 0 or lead < earliest.resolution_seconds):
        lines = ["The reported times overlap, so the order is unresolved."]
        lines += [
            f"**{name}** at {clock(t.at, seconds=not t.resolution_seconds)}"
            for name, t in list(by_label.items())[:5]
        ]
    elif rest:
        field = "⚡ Fastest last time"
        lines = [f"🥇 **{label}** at {when}"]
        for name, timing in rest[:4]:
            if earliest.resolution_seconds or timing.resolution_seconds:
                lines.append(f"{name} at {clock(timing.at, seconds=not timing.resolution_seconds)}")
            else:
                lines.append(f"{name} +{duration((timing.at - at).total_seconds())}")
    else:
        lines = [
            f"Only one time is known for it: **{label}** at {when}.",
            "Which source was first isn't known yet. The next release is timed live.",
        ]
    lines.append(f"-# {day} release · {basis}")
    if speed.benchmark:
        lines.append(f"-# {speed.benchmark}")
    return field, "\n".join(lines)


class TrackProgress:
    def __init__(
        self,
        calendar: Calendar,
        store: DeliveryStore,
        channel: discord.abc.Messageable,
        author_id: int,
        planned: Callable[[], dict[str, Target]],
        now: Callable[[], datetime],
        busy: Callable[[], bool],
        audit: Callable[[str, KnownCompany, datetime], Awaitable[Speed]],
        date_phase: Callable[[int], str | None] | None = None,
        research: Callable[[int], Report | None] | None = None,
        ir_note: Callable[[int], str | None] | None = None,
        cache_notes: Callable[[int], list[str] | None] | None = None,
        cache_caveats: Callable[[int], list[str] | None] | None = None,
        card_spacing: float = CARD_EDIT_SPACING,
        priority: EditPriority | None = None,
        settled: Callable[[int], bool] | None = None,
        old_card_spacing: float = OLD_CARD_SPACING,
        research_paused: Callable[[], bool] | None = None,
        in_window: Callable[[int], bool] | None = None,
        research_mode: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
        research_deferred: Callable[[], bool] = lambda: False,
        profile_note: Callable[[int], str | None] | None = None,
    ) -> None:
        self.calendar, self.store, self.channel = calendar, store, channel
        # Source audits stop while research is paused (by default, while any window is open);
        # otherwise they run in `research_mode`, never for a company whose own window is open
        self.research_paused = research_paused or busy
        self.in_window = in_window or (lambda _: False)
        self.research_mode = research_mode or contextlib.nullcontext
        self.research_deferred = research_deferred
        self.profile_note = profile_note or (lambda cik: None)
        self.card_spacing = card_spacing
        self._last_card_edit = -card_spacing
        # Release posts and edits go first (see edit_priority)
        self.priority = priority
        # Whether research has re-checked an issuer since the bot started and isn't checking it
        # now (DateDiscovery.settled). An old card shows results, not research in progress.
        self.settled = settled or (lambda _: True)
        self.old_card_spacing = old_card_spacing
        self._next_old_edit = 0.0  # time.monotonic() from which an old card may be edited
        self.pending: dict[int, _Pending] = {}  # one per card: the latest content wins
        self.refusals: dict[int, int] = {}  # refused edits of each old card in a row
        self.author_id, self.planned, self.now, self.busy = author_id, planned, now, busy
        self.audit = audit
        self.date_phase = date_phase or (lambda _: None)
        self.research = research or (lambda _: None)
        self.ir_note = ir_note or (lambda _: None)
        self.cache_notes = cache_notes
        self.cache_caveats = cache_caveats or (lambda _: None)
        self.changed = asyncio.Event()
        self.last_sent: dict[int, object] = {}
        self.removed: dict[int, str] = {}
        self.next_attempt: dict[int, datetime] = {}
        self.audit_failed: set[int] = set()
        self.active: tuple[int, asyncio.Task[None]] | None = None

    def _key(self, cik: int) -> str:
        return f"track:{self.calendar.channel}:{cik}"

    async def _pace(self) -> None:
        """Wait until the last card edit is CARD_EDIT_SPACING old."""
        wait = self._last_card_edit + self.card_spacing - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_card_edit = time.monotonic()

    def follow_calendar(self) -> None:
        """After the calendar moved to a new channel, its scorecards keep their messages, so
        they're edited as before instead of posted again. Any process can have moved the
        calendar, such as the update script's window check, so this goes by the records
        themselves: a card kept under another calendar channel's key is this one's."""
        current = f"track:{self.calendar.channel}:"
        rows = self.store.db.execute(
            "SELECT release_key FROM alerts WHERE channel_id = ? AND release_key LIKE 'track:%'",
            (self.store.channel_id,),
        ).fetchall()
        tracked = {str(row.cik) for row in self.calendar.entries()}
        with self.store.db:
            for (key,) in rows:
                _, channel, cik = key.split(":", 2)
                if channel != str(self.calendar.channel) and cik in tracked:
                    self.store.db.execute(
                        "UPDATE OR IGNORE alerts SET release_key = ? "
                        "WHERE channel_id = ? AND release_key = ?",
                        (current + cik, self.store.channel_id, key),
                    )

    async def publish(self, cik: int, embed: discord.Embed, *, old: bool = False) -> bool:
        """Shows the card now. True once Discord shows it; False when the request failed or a
        release step cut in, and the card is tried again later. `old` is for a message over an
        hour old: a refused edit holds back every old card's edits, and a card refused
        REPLACE_AFTER times in a row is posted again."""
        shown = _card_body(embed)
        if self.last_sent.get(cik) == shown:
            return True
        embed.timestamp = self.now()
        poster = CardPoster(
            self.channel, self.store, self._key(cik), self.author_id, pace=self._pace
        )
        replacing = old and self.refusals.get(cik, 0) >= REPLACE_AFTER
        request = poster.replace(embed) if replacing else poster.show(embed)
        try:
            async with asyncio.timeout(CARD_REQUEST_SECONDS):
                outcome = await (self.priority.card(request) if self.priority else request)
        except CardDeferred:
            return False
        except (discord.HTTPException, aiohttp.ClientError, TimeoutError) as error:
            log.warning("Couldn't update the scorecard for %s: %s", cik, error)
            retry_after = _retry_after(error)
            if old and retry_after is not None:
                refused = self.refusals[cik] = self.refusals.get(cik, 0) + 1
                backoff = min(CARD_BACKOFF * 2 ** (refused - 1), CARD_BACKOFF_MAX)
                self._next_old_edit = time.monotonic() + max(retry_after, backoff)
            return False
        self.refusals.pop(cik, None)
        if old and outcome != "unchanged":
            self._next_old_edit = time.monotonic() + self.old_card_spacing
        self.last_sent[cik] = shown
        return True

    def offer(self, cik: int, embed: discord.Embed, *, research: bool = True) -> None:
        """Queues the card's latest content, replacing any not yet shown."""
        if self.last_sent.get(cik) == _card_body(embed):
            self.pending.pop(cik, None)
        else:
            self.pending[cik] = _Pending(embed, research)

    def _researched(self, cik: int) -> bool:
        return self.settled(cik) and not (self.active and self.active[0] == cik)

    async def flush(self) -> None:
        """Shows queued cards, in an order that leaves Discord's edit limits to the alerts:
        nothing while a release step waits; no edits, only new cards, while a window is open;
        and an old card only once its research has settled, OLD_CARD_SPACING after the last."""
        busy = self.busy()
        for cik, item in list(self.pending.items()):
            if self.priority and self.priority.release_waiting:
                return
            saved = self.store.get(self._key(cik))
            posted = saved.message_id if saved else None
            old = False
            if posted is not None:
                if busy:
                    continue
                old = self.now() - discord.utils.snowflake_time(posted) >= OLD_MESSAGE
                if old and (
                    time.monotonic() < self._next_old_edit
                    or (item.research and not self._researched(cik))
                ):
                    continue
            if await self.publish(cik, item.embed, old=old) and self.pending.get(cik) is item:
                del self.pending[cik]

    def _target(self, row: Tracked) -> Target | None:
        now = self.now()
        target = min(
            (
                item
                for item in self.planned().values()
                if item.cik == row.cik and item.window[1] > now
            ),
            key=lambda item: item.expected,
            default=None,
        )
        # A manually pinned window may have been saved since the last directory scan
        if row.target and row.target.window[1] > now and target is None:
            target = row.target
        return target

    def _investigating(self, row: Tracked) -> bool:
        return due(row.ticker, row.company, self.now().astimezone(ET).date())

    def readiness(self, cik: int) -> str | None:
        """The scorecard's color as one dot, so a list and the card never disagree. None for
        an issuer nobody tracked."""
        row = next((item for item in self.calendar.entries() if item.cik == cik), None)
        if row is None:
            return None
        _, review = self.calendar.review(cik)
        checking = bool(self.active and self.active[0] == cik)
        color = self.render(row, review, checking).color
        value = color.value if color is not None else None
        return {GREEN: READY, BLUE: RESEARCHING, RED: PROBLEM}.get(value or 0, NOT_READY)

    def render(self, row: Tracked, review: str | None, checking: bool) -> discord.Embed:
        target = self._target(row)
        speed = Speed.from_json(review)
        # Research waits only while it's paused, or for the company's own window
        busy = self.research_paused() or self.in_window(row.cik)
        catalog = row.ticker in known_companies()
        investigating = self._investigating(row)
        report = self.research(row.cik) if investigating else None
        phase = self.date_phase(row.cik)
        note = self.ir_note(row.cik)
        ir_pending = bool(
            target
            and target.period
            and not target.ir
            and not note
            and issuer_listing(row.ticker, row.company)
        )
        # Looked for and not found, or not announced yet after the last release
        no_date = bool(phase and RECHECKING in phase)
        between = bool(no_date and phase and not phase.startswith("No release date"))

        # The checklist, and the color and headline it adds up to
        form = row.company.sec_form or "8-K"
        steps = [f"{DONE} Found in SEC data: CIK {row.cik}, results on Form {form}"]
        if catalog:
            steps.append(f"{DONE} Sources researched by hand")
        elif report is not None:
            steps.append(f"{WORKING} Investigating sources: {report.step}…")
        elif investigating:
            pause = "waits for an open release window" if busy else "queued"
            steps.append(f"{WAITING if busy else TODO} Investigating sources ({pause})")
        elif looked := row.company.investigated:
            steps.append(f"{DONE} Sources investigated {looked:%b} {looked.day}")
        if target:
            steps.append(f"{DONE} Release date found")
            if phase == VERIFYING_IR:
                steps.append(f"{WORKING} {phase}…")
            elif ir_pending:
                pause = "waits for an open release window" if busy else "queued"
                steps.append(f"{WAITING if busy else TODO} IR verification ({pause})")
        elif phase and (phase.startswith("Looking") or phase == READING_IR):
            steps.append(f"{WORKING} {phase}…")
        elif phase and between:
            steps.append(f"{WAITING} {phase}")
        elif phase and no_date:
            steps.append(f"{FAILED} {phase}")
        elif phase and phase != CHECKING_CACHE:
            steps.append(f"{WAITING} Release date: {phase.lower()}")
        else:
            steps.append(f"{TODO} Release date")
        cache = self.cache_notes(row.cik) if self.cache_notes else None
        cache_pending = self.cache_notes is not None and cache is None
        if phase == CHECKING_CACHE:
            steps.append(f"{WORKING} {phase}…")
        elif cache_pending:
            pause = "waits for an open release window" if busy else "queued"
            steps.append(f"{WAITING if busy else TODO} Cache preflight ({pause})")
        if checking:
            steps.append(f"{WORKING} Checking which source was fastest last time…")
        elif speed is not None:
            steps.append(f"{DONE} Speed history checked")
        elif row.cik in self.audit_failed:
            steps.append(f"{FAILED} Speed history check failed. Retrying within the hour.")
        elif busy:
            steps.append(f"{WAITING} Speed history (waits for an open release window)")
        else:
            steps.append(f"{TODO} Speed history")

        working = any(step.startswith(WORKING) for step in steps)
        complete = (
            speed is not None
            and not investigating
            and not working
            and not ir_pending
            and not cache_pending
            and row.cik not in self.audit_failed
            and (no_date or (target and phase is None))
        )
        # Research can be complete while a watched issuer route still isn't proven fresh
        caveats = self.cache_caveats(row.cik) or []
        if any(step.startswith(FAILED) for step in steps):
            color = RED if row.cik in self.audit_failed else AMBER
        elif working:
            color = BLUE
        elif target and complete and not caveats:
            color = GREEN
        else:
            color = AMBER

        if target:
            evidence = (
                row.evidence if row.target and row.target.expected == target.expected else None
            )
            period = f" · {period_label(target.period)}" if target.period else ""
            description = (
                f"**Next results:** {stamp(target.expected, 'F')} ({stamp(target.expected, 'R')})"
                f"{period}\n-# {evidence_text(evidence or 'From a schedule file')} · "
                f"watching from {clock(target.window[0])}"
            )
        elif working:
            description = "Researching. This message updates as it goes."
        else:
            description = "No release date yet."
        if complete:
            outcome = "watch scheduled" if target else "awaiting a confirmed date"
            headline = f"**✅ Research complete · {outcome}**"
            if caveats:
                named = ", ".join(caveats[:4])
                if len(caveats) > 4:
                    named += f" and {len(caveats) - 4} more"
                headline += (
                    f"\n⚠️ **Not verified fresh:** {named}. These may show the release late; "
                    "SEC filings and newswires are watched too. Checked again daily and before "
                    "the window."
                )
            description = f"{headline}\n{description}"
        embed = discord.Embed(
            title=f"{row.ticker} · {row.company.name}", description=description, color=color
        )
        embed.add_field(name="Progress", value="\n".join(steps), inline=False)

        if speed is not None:
            name, value = _last_release_field(speed)
            embed.add_field(name=name, value=value, inline=False)

        checks = report.checks if report is not None else row.company.checks
        if target:
            sources = target.sources()
            watching = watch_list(sources)
            heading = "👀 Watching for the next release"
        else:
            # What a window would watch, from what's known so far
            preview = to_target(row.ticker, self.now(), self.now().year, 1, row.company)
            watching = watch_list(preview.sources())
            if row.company.ir_news_api:
                watching.append("IR newsroom API *(verified after the date is set)*")
            elif row.company.ir_listing:
                watching.append("IR press release page *(verified after the date is set)*")
            heading = "👀 Will watch once a date is set"
        embed.add_field(name=heading, value=bullets(watching) or "Nothing yet", inline=False)
        if cache:
            embed.add_field(name="Cache preflight", value=bullets(cache), inline=False)
        if profile_note := self.profile_note(row.cik):
            embed.add_field(name="Summary context", value=profile_note, inline=False)

        if listing := row.company.ir_listing:
            status = row.company.ir_http_status
            if target and target.ir and target.ir.http_first and target.ir.listing == listing:
                status = 200
            if status is None:
                access = "Direct HTTP check pending"
            elif status == 0:
                access = "Direct HTTP got no response"
            else:
                access = f"Direct HTTP returned {status}"
            embed.add_field(
                name="IR page access",
                value=f"[Press release listing](<{listing}>) · {access}",
                inline=False,
            )

        rejected = [
            f"**{check.source}**: {check.why}"
            for check in checks
            if not check.watch
            # Verified for this release since, so it's watched after all
            and not (
                target and target.ir and target.ir.listing and "press release page" in check.source
            )
        ]
        if target and target.ir is None and note and issuer_listing(row.ticker, row.company):
            rejected.append(f"**{source_label('browser-ir')}**: {note}")
        if rejected:
            embed.add_field(name="🚫 Checked and rejected", value=bullets(rejected), inline=False)
        elif catalog:
            embed.add_field(
                name="📝 Research", value=truncate(row.company.evidence, 1024), inline=False
            )
        embed.set_footer(text=f"Earnings Bot {release_version()} · Updated")
        return embed

    async def _check(self, row: Tracked) -> None:
        now = self.now()
        self.next_attempt[row.cik] = now + timedelta(hours=1)
        try:
            async with asyncio.timeout(90):
                speed = await self.audit(row.ticker, row.company, now)
        except Exception:
            log.exception("Source audit failed for %s", row.ticker)
            self.audit_failed.add(row.cik)
            self.changed.set()
            return
        if self.research_deferred() or self.research_paused() or self.in_window(row.cik):
            self.next_attempt[row.cik] = now + timedelta(minutes=10)
            self.audit_failed.add(row.cik)
            self.changed.set()
            return
        self.audit_failed.discard(row.cik)
        if any(item.cik == row.cik for item in self.calendar.entries()):
            self.calendar.save_review(row.cik, now, speed.to_json())
        self.changed.set()

    async def run(self) -> None:
        try:
            while True:
                self.changed.clear()
                rows = self.calendar.entries()
                paused = self.research_paused()
                if self.active:
                    cik, task = self.active
                    if paused or self.in_window(cik) or not any(row.cik == cik for row in rows):
                        task.cancel()
                        self.next_attempt.pop(cik, None)
                    if task.done() or task.cancelling():
                        await asyncio.gather(task, return_exceptions=True)
                        self.active = None
                for row in rows:
                    now = self.now()
                    checked, review = self.calendar.review(row.cik)
                    if Speed.from_json(review) is None:
                        checked = None  # never checked, or saved in an older format
                    if (
                        not paused
                        and not self.in_window(row.cik)
                        # Audit the sources the investigation finds, not the ones before it
                        and not self._investigating(row)
                        and self.active is None
                        and (checked is None or now - checked >= REFRESH)
                        and self.next_attempt.get(row.cik, now) <= now
                    ):
                        # Tasks inherit the policy; its callbacks follow window changes.
                        with self.research_mode():
                            self.active = row.cik, asyncio.create_task(self._check(row))
                    checking = bool(self.active and self.active[0] == row.cik)
                    self.offer(row.cik, self.render(row, review, checking))
                for cik, ticker in tuple(self.removed.items()):
                    if self.store.get(self._key(cik)) and not any(row.cik == cik for row in rows):
                        self.offer(
                            cik,
                            discord.Embed(
                                title=f"{ticker} · removed",
                                description="No longer on the earnings calendar.",
                                color=GREY,
                            ),
                            research=False,
                        )
                    self.removed.pop(cik, None)
                await self.flush()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), timeout=5)
        finally:
            if self.active:
                _, task = self.active
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
