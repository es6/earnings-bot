"""Bounded, opt-in profile preparation during ordinary /track research."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import anthropic

from earnings_bot.calendar import Tracked
from earnings_bot.delivery import DeliveryStore
from earnings_bot.driver_profile import parse_profile, profile_request, request_reservation_micros
from earnings_bot.driver_profile_store import DriverProfileStore
from earnings_bot.fetch import Fetcher
from earnings_bot.investigate import _last_releases
from earnings_bot.recorder import ET
from earnings_bot.sec_results import SixKResults, is_results_release, reported_period
from earnings_bot.source_document import document_from_text
from earnings_bot.sources import published_time

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileReply:
    answer: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_created_tokens: int = 0
    stop: str = "end_turn"

    def cost_micros(self) -> int:
        total = self.input_tokens + self.cache_read_tokens + self.cache_created_tokens
        rate, output, cache = (0.1, 0.5, 0.004) if total <= 200_000 else (0.3, 1.5, 0.012)
        return math.ceil(
            self.input_tokens * rate
            + self.output_tokens * output
            + self.cache_read_tokens * cache
            + self.cache_created_tokens * rate * 1.25
        )


class ProfileWriter:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self.client = client.with_options(max_retries=0, timeout=90)

    async def __call__(self, request: dict[str, Any]) -> ProfileReply:
        async with self.client.messages.stream(**request) as stream:
            message = await stream.get_final_message()
        return ProfileReply(
            "".join(block.text for block in message.content if block.type == "text"),
            message.usage.input_tokens,
            message.usage.output_tokens,
            message.usage.cache_read_input_tokens or 0,
            message.usage.cache_creation_input_tokens or 0,
            message.stop_reason or "unknown",
        )


@dataclass(frozen=True)
class ProfileSource:
    text: str
    url: str
    published: datetime


async def latest_profile_source(
    fetcher: Fetcher, sixk: SixKResults, row: Tracked, now: datetime
) -> ProfileSource | None:
    if row.company.sec_form == "6-K":
        releases = [
            (item, (item.body or b"").decode(errors="replace"))
            for item in await sixk.history(row.cik, now, count=1)
        ]
    else:
        releases = await _last_releases(fetcher, row.cik, count=1)
    for filing, text in releases:
        at = published_time(filing.published)
        if at and at <= now and is_results_release(text, "") and reported_period(text):
            return ProfileSource(text, filing.url, at)
    return None


class DriverProfiles:
    def __init__(
        self,
        store: DeliveryStore,
        fetcher: Fetcher,
        sixk: SixKResults,
        write: Callable[[dict[str, Any]], Awaitable[ProfileReply]],
        paused: Callable[[int], bool],
    ) -> None:
        self.profiles = DriverProfileStore(store)
        self.fetcher, self.sixk, self.write, self.paused = fetcher, sixk, write, paused
        self.lock = store.lock("driver-profile-model")  # one background call at a time
        self.checked: dict[int, datetime] = {}
        self.notes: dict[int, str] = {}

    def context(self, cik: int, now: datetime) -> dict[str, Any]:
        profile = self.profiles.get(cik, now.astimezone(ET).date())
        return profile.context() if profile else {}

    def _paused(self, cik: int) -> bool:
        return self.paused(cik) or self.fetcher.research_deferred()

    async def ensure(self, row: Tracked, now: datetime) -> None:
        if self._paused(row.cik) or self.checked.get(row.cik, now) > now:
            return
        today = now.astimezone(ET).date()
        self.checked[row.cik] = now + timedelta(days=1)
        try:
            source = await latest_profile_source(self.fetcher, self.sixk, row, now)
            if self._paused(row.cik):
                self.checked[row.cik] = now + timedelta(minutes=10)
                return
            if source is None:
                self.notes[row.cik] = "Awaiting a recent verified results release"
                return
            period = reported_period(source.text)
            assert period is not None  # latest_profile_source verified it
            document = document_from_text(source.text)
            previous = self.profiles.get(row.cik, today)
            if previous and previous.document_sha256 == document.sha256:
                self.notes[row.cik] = f"Prior-release drivers reviewed {previous.reviewed:%b %d}"
                return
            # Never pay to profile an old release just because it was fetched today.
            if today - source.published.astimezone(ET).date() > timedelta(days=120):
                self.notes[row.cik] = "Awaiting a recent verified results release"
                return
            request = profile_request(row.ticker, source.text)
            reservation = request_reservation_micros(request)
            async with self.lock:
                if self._paused(row.cik):
                    self.checked[row.cik] = now + timedelta(minutes=10)
                    return
                if not self.profiles.claim(row.cik, document.sha256, today, reservation):
                    self.notes[row.cik] = (
                        "Profile preparation is waiting for its next eligible check"
                    )
                    return
                self.notes[row.cik] = "Reading prior-release business drivers"
                reply = await self.write(request)
            profile = None
            if reply.stop == "end_turn":
                with suppress(ValueError):  # keep the previous valid profile, if any
                    profile = parse_profile(
                        reply.answer,
                        source.text,
                        source_url=source.url,
                        document_sha256=document.sha256,
                        source_period=f"FY{period[0]}Q{period[1]}",
                        source_published=source.published.astimezone(ET).date(),
                        today=today,
                    )
            self.profiles.finish(row.cik, document.sha256, reply.cost_micros(), profile)
            self.notes[row.cik] = (
                f"Prior-release drivers reviewed {today:%b %d}"
                if profile
                else "Profile evidence was incomplete; using the release alone"
            )
        except asyncio.CancelledError:
            raise  # an uncertain paid call keeps its reservation, across restart
        except Exception:
            log.exception("Driver profile preparation failed for %s", row.ticker)
            self.notes[row.cik] = "Profile unavailable; using the release alone"
