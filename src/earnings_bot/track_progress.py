"""Edit one durable Discord progress message for each tracked issuer."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta

import aiohttp
import discord

from earnings_bot.calendar import Calendar, Tracked, describe
from earnings_bot.delivery import DeliveryStore
from earnings_bot.investigate import due
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.pipeline import DurableDiscordPoster
from earnings_bot.recorder import ET, Target

log = logging.getLogger(__name__)
REFRESH = timedelta(days=1)


class _ProgressPoster(DurableDiscordPoster):
    def _merge_content(self, existing: str, content: str) -> str:
        return content


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
        audit: Callable[[str, KnownCompany, datetime], Awaitable[str]],
        date_phase: Callable[[int], str | None] | None = None,
    ) -> None:
        self.calendar, self.store, self.channel = calendar, store, channel
        self.author_id, self.planned, self.now, self.busy = author_id, planned, now, busy
        self.audit = audit
        self.date_phase = date_phase or (lambda _: None)
        self.changed = asyncio.Event()
        self.last_sent: dict[int, str] = {}
        self.removed: dict[int, str] = {}
        self.next_attempt: dict[int, datetime] = {}
        self.audit_failed: set[int] = set()
        self.active: tuple[int, asyncio.Task[None]] | None = None

    def _key(self, cik: int) -> str:
        return f"track:{self.calendar.channel}:{cik}"

    async def publish(self, cik: int, content: str) -> None:
        if self.last_sent.get(cik) == content:
            return
        poster = _ProgressPoster(self.channel, self.store, self._key(cik), self.author_id)
        try:
            await poster.send(content)
        except (discord.HTTPException, aiohttp.ClientError, TimeoutError) as error:
            log.warning("Couldn't update tracking progress for %s: %s", cik, error)
        else:
            self.last_sent[cik] = content

    def _render(self, row: Tracked, result: str | None, checking: bool) -> str:
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
        # A manually pinned window may have been saved since the last directory scan.
        if row.target and row.target.window[1] > now and target is None:
            target = row.target
        paused = self.busy() and (not target or self._investigating(row) or not result)
        lines = [f"**Tracking {row.ticker}**"]
        if target:
            evidence = (
                row.evidence if row.target and row.target.expected == target.expected else None
            )
            lines.append(describe(target, evidence or "Schedule file"))
            names = [
                source.name + (" (trial)" if source.trial else "") for source in target.sources()
            ]
            lines.append("Sources prepared for the window | " + ", ".join(names))
        elif not paused:
            lines.append(self.date_phase(row.cik) or "Waiting for issuer date research to start.")
        if row.company.sec_form == "6-K":
            lines.append("Files results on SEC Form 6-K.")
        if row.ticker not in known_companies():
            lines.append(row.company.evidence)
        if paused:
            lines.append("Research is waiting for release monitoring to finish.")
        elif self._investigating(row):
            if not self.date_phase(row.cik):
                lines.append("IR site, feed and newswire investigation queued.")
        elif checking:
            lines.append("Checking the last release, issuer endpoints and @earnings_guy.")
        elif row.cik in self.audit_failed:
            lines.append("Source audit failed. Retrying at the next check.")
        elif not result:
            lines.append("Source audit queued. Active release windows take priority.")
        if result:
            lines.append(result)
        return "\n".join(lines)[:1950]

    def _investigating(self, row: Tracked) -> bool:
        return due(row.ticker, row.company, self.now().astimezone(ET).date())

    async def _check(self, row: Tracked) -> None:
        now = self.now()
        self.next_attempt[row.cik] = now + timedelta(hours=1)
        try:
            async with asyncio.timeout(90):
                result = await self.audit(row.ticker, row.company, now)
        except Exception:
            log.exception("Source audit failed for %s", row.ticker)
            self.audit_failed.add(row.cik)
            self.changed.set()
            return
        self.audit_failed.discard(row.cik)
        if any(item.cik == row.cik for item in self.calendar.entries()):
            self.calendar.save_review(row.cik, now, result)
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
                    checked, result = self.calendar.review(row.cik)
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
                    await self.publish(row.cik, self._render(row, result, checking))
                for cik, ticker in tuple(self.removed.items()):
                    if self.store.get(self._key(cik)) and not any(row.cik == cik for row in rows):
                        await self.publish(
                            cik, f"**{ticker}** was removed from the earnings calendar."
                        )
                    self.removed.pop(cik, None)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), timeout=5)
        finally:
            if self.active:
                _, task = self.active
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
