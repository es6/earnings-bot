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
from earnings_bot.ir_research import inspect_issuer, issuer_listing
from earnings_bot.known import KnownCompany, wires
from earnings_bot.recorder import ET, PollError, Target, poll
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
    company: KnownCompany,
    now: datetime,
    benchmark: EarningsBenchmark | None,
) -> str:
    """Use the latest dated results. Publisher times are estimates of availability."""
    sec = EdgarFilings(company.cik, limit=150)
    filings = [
        (at, item)
        for item in await _list(fetcher, sec)
        if item.earnings
        and item.title.startswith("8-K ")
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
                estimates.append(Timing("SEC acceptance", accepted, filing.url))
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

    if anchor is None:
        return "Most recent release could not be anchored to a dated report."
    day = anchor
    heads, bodies, comparison = await asyncio.to_thread(_measurements, directory, ticker, day)

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
    browser = await inspect_issuer(fetcher, issuer_listing(ticker, company), day, titles)
    heading = "Last release" if confidence else "Likely last release (8-K date)"
    lines = [
        f"**{heading} | {day:%b %d, %Y}**",
        _rank("Measured report text", bodies),
        _rank("Measured headlines", heads),
        _rank("Archive times (estimates)", estimates),
        "Verified archive bodies | " + (", ".join(dict.fromkeys(verified)) or "none found"),
        comparison or "@earnings_guy | no verified post in the available history",
    ]
    if browser:
        lines.append(browser)
    return "\n".join(lines)
