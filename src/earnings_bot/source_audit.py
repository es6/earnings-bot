"""Rank sources on the most recent earnings release using comparable evidence."""

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from earnings_bot.benchmark import EarningsBenchmark
from earnings_bot.fetch import Fetcher
from earnings_bot.formatting import clock
from earnings_bot.ir_research import inspect_issuer, issuer_listing
from earnings_bot.known import KnownCompany, wires
from earnings_bot.recorder import ET, PollError, Target, poll
from earnings_bot.sec_results import SixKResults, SixKUnavailable
from earnings_bot.sources import (
    EdgarFilings,
    RssFeed,
    Sighting,
    Source,
    looks_like_earnings,
    published_time,
)
from earnings_bot.text import release_text, verified_report

_ARCHIVE = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl")


@dataclass(frozen=True)
class Timing:
    source: str
    at: datetime
    url: str = ""
    resolution_seconds: int = 0


def _timings(rows: object) -> list[Timing]:
    if not isinstance(rows, list):
        raise TypeError("timings must be a list")
    return [
        Timing(
            row["source"],
            datetime.fromisoformat(row["at"]),
            row.get("url", ""),
            row.get("resolution_seconds", 0),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class Speed:
    """How fast each source had a company's most recent results."""

    day: date | None  # None when no release could be dated
    confident: bool = False  # the release's text was verified as a results report
    form: str = "8-K"
    measured_text: list[Timing] = field(default_factory=list)  # when the bot read its text live
    measured_headlines: list[Timing] = field(default_factory=list)  # when the bot saw it live
    archive: list[Timing] = field(default_factory=list)  # publishers' own timestamps
    verified: list[str] = field(default_factory=list)  # sources whose archive copy verified
    benchmark: str = ""  # markdown about @earnings_guy's post, if one was found
    ir_json: str = ""  # JSON routes the IR site's pages called, for finding faster sources

    def fastest(self) -> tuple[str, list[Timing]]:
        """The best evidence of which source was first, and its timings, earliest first."""
        for basis, timings in (
            ("seen live by the bot", self.measured_headlines),
            ("publishers' timestamps", self.archive),
        ):
            if timings:
                return basis, sorted(timings, key=lambda item: (item.at, item.source))
        return "", []

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=lambda value: value.isoformat())

    @classmethod
    def from_json(cls, text: str | None) -> "Speed | None":
        """A saved audit, or None for one saved in an older format or not at all."""
        try:
            data = json.loads(text or "")
            return cls(
                day=date.fromisoformat(data["day"]) if data["day"] else None,
                confident=bool(data["confident"]),
                form=str(data["form"]),
                measured_text=_timings(data["measured_text"]),
                measured_headlines=_timings(data["measured_headlines"]),
                archive=_timings(data["archive"]),
                verified=[str(name) for name in data["verified"]],
                benchmark=str(data["benchmark"]),
                ir_json=str(data.get("ir_json", "")),
            )
        except (ValueError, KeyError, TypeError):
            return None


def published_first(speed: Speed) -> datetime | None:
    """When the IR site or a wire had the last release, if that was before SEC had it. A 6-K
    can come an hour or more after the release: On's IR feed had its Q2 results at 5:00 AM, its
    6-K came at 6:10."""
    timings = [*speed.measured_headlines, *speed.archive]
    sec = [t.at for t in timings if t.source.startswith(("SEC ", "edgar"))]
    others = [t.at for t in timings if not t.source.startswith(("SEC ", "edgar"))]
    if not sec or not others or min(others) >= min(sec):
        return None
    return min(others)


def local_day(stamp: str | None) -> date | None:
    if moment := published_time(stamp):
        return moment.astimezone(ET).date()
    if stamp:
        try:
            return datetime.strptime(stamp, "%m/%d/%Y %H:%M:%S").date()
        except ValueError:
            return None
    return None


def site_local_time(stamp: str | None, timings: list[Timing]) -> datetime | None:
    """A Q4 site's "08/13/2026 16:01:00", which names no time zone, read as US Eastern, the zone
    US-listed issuers' IR sites keep. Only when SEC's acceptance that day confirms it: from six
    hours before to an hour after, as a press release and its filing go."""
    try:
        at = datetime.strptime(stamp or "", "%m/%d/%Y %H:%M:%S").replace(tzinfo=ET)
    except ValueError:
        return None
    sec = [timing.at for timing in timings if timing.source.startswith("SEC ")]
    if any(timedelta(hours=-1) <= filed - at <= timedelta(hours=6) for filed in sec):
        return at
    return None


def _events(directory: Path, ticker: str) -> Iterator[dict[str, Any]]:
    for path in sorted(directory.glob("*.jsonl")):
        if path.name != "watch.jsonl" and not _ARCHIVE.fullmatch(path.name):
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as file:
                for line in file:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # includes a trailing line still being written
                    if isinstance(event, dict) and event.get("ticker") == ticker:
                        yield event
        except OSError:
            continue


def _measurements(
    directory: Path, ticker: str, day: date
) -> tuple[list[Timing], list[Timing], str]:
    headlines: dict[str, Timing] = {}
    bodies: dict[str, Timing] = {}
    benchmark = ""
    for event in _events(directory, ticker):
        if event.get("caught_up"):
            continue
        kind = event.get("event")
        if (
            kind == "benchmark"
            and local_day(event.get("at")) == day
            and (post := event.get("x_post"))
        ):
            x_at = published_time(event.get("x_posted"))
            discord_at = published_time(event.get("discord_alert"))
            benchmark = f"[@earnings_guy posted]({post})"
            if x_at and discord_at:
                delta = (discord_at - x_at).total_seconds()
                direction = "after" if delta > 0 else "before"
                benchmark += f", and our alert went out {abs(delta):.2f}s {direction} it"
        if kind == "new" and event.get("earnings"):
            if not looks_like_earnings(event.get("title", "")) and not event["source"].startswith(
                "edgar"
            ):
                continue
            at = published_time(event.get("seen"))
            table = headlines
        elif kind == "source_body":
            at = published_time(event.get("at"))
            table = bodies
        else:
            continue
        if at is None or at.astimezone(ET).date() != day:
            continue
        source = event["source"]
        if source not in table or at < table[source].at:
            table[source] = Timing(source, at, event.get("url", ""))
    return list(headlines.values()), list(bodies.values()), benchmark


async def _list(fetcher: Fetcher, source: Source) -> list[Sighting]:
    try:
        _, items = await poll(source, fetcher)
        return items
    except (PollError, ValueError, AttributeError):
        return []


def _archived_sources(ticker: str, company: KnownCompany, now: datetime) -> list[Source]:
    business_wire, pr_newswire = wires(company)
    target = Target(
        ticker=ticker,
        cik=company.cik,
        expected=now,
        q4_host=company.q4_host if company.reachable else None,
        newswire=business_wire,
        prnewswire=pr_newswire,
        rss=company.rss,
        trial=company.trial.model_copy(update={"pages": []}),
    )
    return [
        source
        for source in target.sources()
        if source.name in {"q4_press", "broadcom_newsroom"}
        # One copy of each feed is enough for the archive
        or (isinstance(source, RssFeed) and not source.name.endswith("_busted"))
    ]


async def audit_sources(
    fetcher: Fetcher,
    directory: Path,
    ticker: str,
    company: KnownCompany,
    now: datetime,
    benchmark: EarningsBenchmark | None,
    sixk: SixKResults | None = None,
) -> Speed:
    """Use the latest dated results. Publisher times are estimates of availability."""
    form = company.sec_form or "8-K"
    sec = EdgarFilings(company.cik, limit=500 if form == "6-K" else 150, form=form)
    recent = await _list(fetcher, sec)
    if sec.form == "6-K":
        try:
            # The latest release can have two results 6-Ks, as when a Hong Kong listing files
            # its own announcement hours after the press release
            recent = await (sixk or SixKResults(fetcher)).history(
                company.cik, now, count=2, filings=recent
            )
        except SixKUnavailable:
            recent = []
    filings = [
        (at, item)
        for item in recent
        if item.earnings
        and item.title.startswith(f"{sec.form} ")
        and (at := published_time(item.published))
        and at <= now
    ]
    snapshots = [
        (source, await _list(fetcher, source)) for source in _archived_sources(ticker, company, now)
    ]
    estimates: list[Timing] = []
    verified: list[str] = []
    titles: set[str] = set()
    anchor: date | None = None
    confidence = False
    for candidate in sorted(filings, key=lambda pair: pair[0], reverse=True)[:4]:
        accepted, filing = candidate
        exhibit = await release_text(fetcher, sec, filing)
        if exhibit is None or verified_report(exhibit):
            anchor = accepted.astimezone(ET).date()
            if exhibit is not None:
                confidence = True
                if sec.form == "6-K":
                    # SEC first had the release with the day's earliest verified results 6-K
                    accepted, filing = min(
                        (pair for pair in filings if pair[0].astimezone(ET).date() == anchor),
                        key=lambda pair: pair[0],
                    )
                estimates.append(Timing(f"SEC {sec.form} acceptance", accepted, filing.url))
                verified.append(sec.name)
            break

    archives = sorted(
        (
            (day, source, item)
            for source, items in snapshots
            for item in items
            if item.earnings
            and (day := local_day(item.published))
            and day <= now.astimezone(ET).date()
            and (anchor is None or day >= anchor)
        ),
        key=lambda row: row[0],
        reverse=True,
    )
    checked: set[tuple[str, date]] = set()
    for day, source, item in archives[:30]:
        if anchor and day < anchor:
            break
        token = (source.name, day)
        if token in checked:
            continue
        checked.add(token)
        if not verified_report(await release_text(fetcher, source, item)):
            continue
        if anchor != day:
            anchor = day
            estimates.clear()
            verified.clear()
        confidence = True
        verified.append(source.name)
        titles.add(item.title)
        if at := published_time(item.published):
            estimates.append(Timing(source.name, at, item.url))
        elif at := site_local_time(item.published, estimates):
            # Shown to the minute: Q4's seconds are usually :00
            estimates.append(Timing(source.name, at, item.url, resolution_seconds=60))

    if anchor is None:
        return Speed(day=None, form=sec.form)
    day = anchor
    posted = company.ir_posted
    if posted and posted.astimezone(ET).date() == day:
        # The IR site's own time for the release, which Chrome read when investigating
        estimates.append(
            Timing("browser-ir", posted, company.ir_release or "", resolution_seconds=60)
        )
    heads, bodies, comparison = await asyncio.to_thread(_measurements, directory, ticker, day)

    if benchmark and not comparison:
        candidates = [
            post
            for post in (await benchmark.snapshot() or ())
            if post.ticker == ticker and post.posted.astimezone(ET).date() == day
        ]
        for post in sorted(candidates, key=lambda item: item.posted):
            if await benchmark.verify(post):
                when = clock(post.posted, seconds=True)
                comparison = f"[@earnings_guy posted]({post.url}) at {when}"
                break
    browser = await inspect_issuer(fetcher, issuer_listing(ticker, company), day, titles)
    return Speed(
        day=day,
        confident=confidence,
        form=sec.form,
        measured_text=bodies,
        measured_headlines=heads,
        archive=estimates,
        verified=list(dict.fromkeys(verified)),
        benchmark=comparison,
        ir_json=browser,
    )
