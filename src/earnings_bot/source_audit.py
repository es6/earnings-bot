"""Rank sources on the most recent earnings release using comparable evidence."""

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from earnings_bot.benchmark import EarningsBenchmark
from earnings_bot.fetch import Fetcher
from earnings_bot.known import known_companies
from earnings_bot.recorder import ET, PollError, Target, poll
from earnings_bot.sources import (
    EdgarFilings,
    RssFeed,
    Sighting,
    Source,
    looks_like_earnings,
    published_time,
)
from earnings_bot.text import release_text

_REVENUE = re.compile(r"\b(?:revenue|net sales)\b", re.I)
_PROFIT = re.compile(r"\b(?:net (?:income|loss)|per (?:diluted )?share|EPS)\b", re.I)
_ARCHIVE = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl")


@dataclass(frozen=True)
class Timing:
    source: str
    at: datetime
    url: str = ""


def verified_report(text: str | None) -> bool:
    return bool(text and _REVENUE.search(text) and _PROFIT.search(text))


def local_day(stamp: str | None) -> date | None:
    if moment := published_time(stamp):
        return moment.astimezone(ET).date()
    if stamp:
        try:
            return datetime.strptime(stamp, "%m/%d/%Y %H:%M:%S").date()
        except ValueError:
            return None
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
            benchmark = f"@earnings_guy <{post}>"
            if x_at and discord_at:
                delta = (discord_at - x_at).total_seconds()
                direction = "later" if delta > 0 else "earlier"
                benchmark += f" | Discord {abs(delta):.2f}s {direction}"
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


def _rank(label: str, items: list[Timing]) -> str:
    ordered = sorted(items, key=lambda item: (item.at, item.source))
    if not ordered:
        return f"{label} | no timing recorded"
    first = ordered[0]
    names = [f"{item.source} +{(item.at - first.at).total_seconds():.2f}s" for item in ordered[:6]]
    return f"{label} | {first.at.astimezone(ET):%H:%M:%S} ET | {', '.join(names)}"


async def _list(fetcher: Fetcher, source: Source) -> list[Sighting]:
    try:
        _, items = await poll(source, fetcher)
        return items
    except (PollError, ValueError, AttributeError):
        return []


def _archived_sources(ticker: str, now: datetime) -> list[Source]:
    company = known_companies()[ticker]
    target = Target(
        ticker=ticker,
        cik=company.cik,
        expected=now,
        q4_host=company.q4_host if company.reachable else None,
        newswire=company.names if company.wire in ("Business Wire", "unknown") else [],
        prnewswire=company.names if company.wire == "PR Newswire" else [],
        rss=company.rss,
        trial=company.trial.model_copy(update={"pages": [], "ir": None}),
    )
    return [
        source
        for source in target.sources()
        if source.name in {"q4_press", "broadcom_newsroom"}
        or (isinstance(source, RssFeed) and source.name != "globenewswire_busted")
    ]


async def audit_sources(
    fetcher: Fetcher,
    directory: Path,
    ticker: str,
    now: datetime,
    benchmark: EarningsBenchmark | None,
) -> str:
    """Use an earnings 8-K to anchor the day. Publisher times are estimates of availability."""
    sec = EdgarFilings(known_companies()[ticker].cik, limit=150)
    filings = [
        (at, item)
        for item in await _list(fetcher, sec)
        if item.earnings
        and item.title.startswith("8-K ")
        and (at := published_time(item.published))
        and at <= now
    ]
    if not filings:
        return "Most recent release could not be anchored to a dated earnings filing."
    accepted, filing = max(filings, key=lambda pair: pair[0])
    day = accepted.astimezone(ET).date()
    heads, bodies, comparison = await asyncio.to_thread(_measurements, directory, ticker, day)
    estimates = []
    verified = []
    if verified_report(await release_text(fetcher, sec, filing)):
        estimates.append(Timing("SEC acceptance", accepted, filing.url))
        verified.append(sec.name)
    for source in _archived_sources(ticker, now):
        for item in await _list(fetcher, source):
            if not item.earnings or local_day(item.published) != day:
                continue
            if not verified_report(await release_text(fetcher, source, item)):
                continue
            verified.append(source.name)
            if at := published_time(item.published):
                estimates.append(Timing(source.name, at, item.url))
            break

    if benchmark and not comparison:
        candidates = [
            post
            for post in (await benchmark.snapshot() or ())
            if post.ticker == ticker and post.posted.astimezone(ET).date() == day
        ]
        for post in sorted(candidates, key=lambda item: item.posted):
            if await benchmark.verify(post):
                comparison = (
                    f"@earnings_guy | {post.posted.astimezone(ET):%H:%M:%S} ET <{post.url}>"
                )
                break
    return "\n".join(
        [
            f"**Last release | {day:%b %d, %Y}**",
            _rank("Measured report text", bodies),
            _rank("Measured headlines", heads),
            _rank("Archive times (estimates)", estimates),
            "Verified archive bodies | " + (", ".join(dict.fromkeys(verified)) or "none found"),
            comparison or "@earnings_guy | no verified post in the available history",
        ]
    )
