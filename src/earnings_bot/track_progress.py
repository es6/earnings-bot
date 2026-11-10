"""Edit one durable Discord scorecard for each tracked issuer, as the bot researches it."""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta

import aiohttp
import discord

from earnings_bot.calendar import Calendar, Tracked, evidence_text
from earnings_bot.date_discovery import CHECKING_CACHE, VERIFYING_IR
from earnings_bot.delivery import DeliveryStore
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

DONE, WORKING, WAITING, FAILED, TODO = "✅", "⏳", "⏸️", "⚠️", "▫️"


class CardPoster:
    """One message holding an embed, sent once and then edited. Its ID is kept in the delivery
    store, so a restart edits the same message."""

    def __init__(
        self, channel: discord.abc.Messageable, store: DeliveryStore, key: str, author_id: int
    ) -> None:
        self.channel, self.store, self.key, self.author_id = channel, store, key, author_id

    async def show(self, embed: discord.Embed) -> None:
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
            message = await self.channel.send(embed=embed, nonce=nonce)
        else:
            await message.edit(content=None, embed=embed)
        self.store.delivered(self.key, message.id)


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
    ) -> None:
        self.calendar, self.store, self.channel = calendar, store, channel
        self.author_id, self.planned, self.now, self.busy = author_id, planned, now, busy
        self.audit = audit
        self.date_phase = date_phase or (lambda _: None)
        self.research = research or (lambda _: None)
        self.ir_note = ir_note or (lambda _: None)
        self.cache_notes = cache_notes
        self.changed = asyncio.Event()
        self.last_sent: dict[int, object] = {}
        self.removed: dict[int, str] = {}
        self.next_attempt: dict[int, datetime] = {}
        self.audit_failed: set[int] = set()
        self.active: tuple[int, asyncio.Task[None]] | None = None

    def _key(self, cik: int) -> str:
        return f"track:{self.calendar.channel}:{cik}"

    async def publish(self, cik: int, embed: discord.Embed) -> None:
        shown = embed.to_dict()
        if self.last_sent.get(cik) == shown:
            return
        embed.timestamp = self.now()
        poster = CardPoster(self.channel, self.store, self._key(cik), self.author_id)
        try:
            await poster.show(embed)
        except (discord.HTTPException, aiohttp.ClientError, TimeoutError) as error:
            log.warning("Couldn't update the scorecard for %s: %s", cik, error)
        else:
            self.last_sent[cik] = shown

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

    def render(self, row: Tracked, review: str | None, checking: bool) -> discord.Embed:
        target = self._target(row)
        speed = Speed.from_json(review)
        busy = self.busy()
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
        no_date = bool(phase and phase.startswith("No release date"))

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
        elif phase and phase.startswith("Looking"):
            steps.append(f"{WORKING} {phase}…")
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
        if any(step.startswith(FAILED) for step in steps):
            color = RED if row.cik in self.audit_failed else AMBER
        elif working:
            color = BLUE
        elif target and complete:
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
            description = f"**✅ Research complete · {outcome}**\n{description}"
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
        self.audit_failed.discard(row.cik)
        if any(item.cik == row.cik for item in self.calendar.entries()):
            self.calendar.save_review(row.cik, now, speed.to_json())
        self.changed.set()

    async def run(self) -> None:
        try:
            while True:
                self.changed.clear()
                rows = self.calendar.entries()
                busy = self.busy()
                if self.active:
                    cik, task = self.active
                    if busy or not any(row.cik == cik for row in rows):
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
                        not busy
                        # Audit the sources the investigation finds, not the ones before it
                        and not self._investigating(row)
                        and self.active is None
                        and (checked is None or now - checked >= REFRESH)
                        and self.next_attempt.get(row.cik, now) <= now
                    ):
                        self.active = row.cik, asyncio.create_task(self._check(row))
                    checking = bool(self.active and self.active[0] == row.cik)
                    await self.publish(row.cik, self.render(row, review, checking))
                for cik, ticker in tuple(self.removed.items()):
                    if self.store.get(self._key(cik)) and not any(row.cik == cik for row in rows):
                        await self.publish(
                            cik,
                            discord.Embed(
                                title=f"{ticker} · removed",
                                description="No longer on the earnings calendar.",
                                color=GREY,
                            ),
                        )
                    self.removed.pop(cik, None)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), timeout=5)
        finally:
            if self.active:
                _, task = self.active
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
