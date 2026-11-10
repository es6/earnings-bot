import asyncio
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from earnings_bot.calendar import Calendar, describe
from earnings_bot.date_discovery import CALENDAR_6K_DATE, DateDiscovery, calendar_date
from earnings_bot.delivery import DeliveryStore
from earnings_bot.issuers import profile
from earnings_bot.known import SourceCheck
from earnings_bot.recorder import ET, Journal, Polls, Target, Watch
from earnings_bot.sec_results import SixKResults, is_results_release
from earnings_bot.source_audit import Speed, audit_sources
from earnings_bot.sources import EdgarFilings, EdgarLatest, Sighting
from earnings_bot.track_progress import TrackProgress
from tests.fakes import RoutedFetcher, embed_text, sec_filing_detail
from tests.test_unattended import FakeDiscord

NOW = datetime(2026, 11, 9, 17, tzinfo=ET)
RESULT = "0001171843-26-005474"
ORDINARY = "0001171843-26-005640"
CIK = 1001085
HOST = "https://bn.brookfield.com"
LISTING = f"{HOST}/events-news/press-releases"
NOTICE = f"{HOST}/press-releases/third-quarter-results-conference-call"
TITLE = "Brookfield Corporation to Host Third Quarter 2026 Results Conference Call"
NOTICE_BODY = (
    f"<h1>{TITLE}</h1><p>Brookfield Corporation will host its third quarter 2026 conference call "
    "on Thursday, November 12, 2026, at 10:00 a.m. (ET).</p>"
    "<p>Results will be released that morning at approximately 7:00 a.m. (ET).</p>"
).encode()
RELEASE = (
    "<h1>Brookfield Corporation Reports Increase in Earnings</h1>"
    "<p>BROOKFIELD NEWS, Aug. 13, 2026 (GLOBE NEWSWIRE) -- Brookfield Corporation announced "
    "strong financial results for the quarter ended June 30, 2026.</p>"
    + "<p>Revenues of $5 billion and net income from operations of $300 million. </p>" * 35
    + "<p>Visit www.bn.brookfield.com for more information.</p>"
)
FINANCING = (
    "<h1>Brookfield Announces Closing of Note Offering</h1>"
    + "<p>The notes will mature in 2035 and finance company operations.</p>" * 40
    + "<p>Brookfield reported earnings for the last quarter. Revenues of $5 billion.</p>"
    + "<p>Net income grew.</p>"
)


def filings(*, accepted: str = "2026-08-13T10:45:04Z") -> dict:
    return {
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [ORDINARY, RESULT],
                "form": ["6-K", "6-K"],
                "items": ["", ""],
                "acceptanceDateTime": [accepted, accepted],
                "primaryDocument": ["cover.htm", "cover.htm"],
            }
        },
    }


def folder(key: str) -> str:
    return f"/Archives/edgar/data/{CIK}/{key.replace('-', '')}"


def fixtures(data: dict) -> dict[str, tuple[int, bytes]]:
    routes = {f"CIK{CIK:010d}.json": (200, json.dumps(data).encode())}
    for key, html in [(ORDINARY, FINANCING), (RESULT, RELEASE)]:
        base = folder(key)
        routes[f"{base}/{key}-index.htm"] = (
            200,
            sec_filing_detail(f"{base}/cover.htm", "exh_991.htm"),
        )
        routes[f"{base}/exh_991.htm"] = (200, html.encode())
    return routes


@pytest.mark.parametrize(
    "description",
    ["Press Release", "Exhibit 99.1", "Quarterly Results"],
)
def test_financial_boilerplate_cannot_turn_a_6k_into_results(description: str) -> None:
    text = "Notes financing closed today.\n" + "Revenues and net income. " * 110
    assert not is_results_release(text, description)
    notice = (
        "Company Announces Date of Third Quarter Results\n"
        "The company reported earnings for the last quarter.\n" + text
    )
    assert not is_results_release(notice, description)


async def test_both_sec_watchers_verify_once_and_alert_only_on_results() -> None:
    journal = Journal(io.StringIO())
    now = journal.now()
    data = filings(accepted=now.isoformat())
    entries = "".join(
        f"<entry><id>urn:accno:{key}</id><title>6-K - Brookfield</title>"
        f'<link href="https://www.sec.gov{folder(key)}/{key}-index.htm"/>'
        f"<updated>{now.isoformat()}</updated><summary>Form 6-K</summary></entry>"
        for key in (ORDINARY, RESULT)
    )
    routes = fixtures(data)
    routes["type=6-K"] = (
        200,
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode(),
    )
    fetcher = RoutedFetcher(routes)
    target = Target(ticker="BN", cik=CIK, expected=now, sec_form="6-K")
    submissions, latest = target.sources()
    assert isinstance(submissions, EdgarFilings) and isinstance(latest, EdgarLatest)
    assert "type=6-K" in latest.url()
    end = now + timedelta(seconds=0.35)
    window = (now - timedelta(seconds=1), end)
    polls = Polls(fetcher, journal)
    seen: list[Sighting] = []
    await asyncio.gather(
        *(
            polls.watch(Watch("BN", source, window), end, lambda _, item: seen.append(item))
            for source in (submissions, latest)
        )
    )
    assert [(item.key, item.earnings, item.caught_up) for item in seen] == [
        (RESULT, True, True),
        (RESULT, True, True),
    ]
    urls = [url for _, url, _ in fetcher.requests]
    for key in (ORDINARY, RESULT):
        assert sum(url.endswith(f"{folder(key)}/exh_991.htm") for url in urls) == 1


async def test_saved_bn_profile_is_enriched_and_scheduled_from_issuer_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from earnings_bot import date_discovery

    async def no_trial(*args: object) -> tuple[None, str]:
        return None, "Not checked in tests"

    monkeypatch.setattr(date_discovery, "verified_browser_source", no_trial)
    data = filings()
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date())
    assert company.sec_form == "6-K"
    old = company.model_copy(update={"sec_form": None, "investigated": NOW.date()})
    routes = fixtures(data)
    routes[LISTING] = (200, f'<a href="{NOTICE}">{TITLE}</a>'.encode())
    routes[NOTICE] = (200, NOTICE_BODY)
    routes[f"{HOST}/"] = (200, b'<a href="/events-news/press-releases">Press Releases</a>')
    routes["symbol=BN"] = (200, b'[{"symbol":"BN","date":"2026-11-12","epsActual":null}]')
    fetcher = RoutedFetcher(routes)
    messages: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("BN", 1, NOW, old)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, messages.append)
        await dates._check(row)
        found = calendar.find("BN")
        assert found and found.target
        assert found.target.expected == datetime(2026, 11, 12, 7, tzinfo=ET)
        assert found.target.period == "FY2026Q3" and found.target.sec_form == "6-K"
        assert found.target.before_minutes == 30
        assert found.company.sixk == ["06:45"] and found.company.release_time == "06:45"
        assert found.company.ir_listing == LISTING
        assert found.company.wire == "GlobeNewswire"
        assert messages == [f"📅 {describe(found.target, found.evidence or '')}"]
        assert ("SEC 6-K filings", True) in [(c.source, c.watch) for c in found.company.checks]
        fallback = await calendar_date(fetcher, "test", "BN", found.company, NOW)
        assert fallback and fallback[0].expected.hour == 6 and fallback[1] == CALENDAR_6K_DATE


async def test_source_audit_anchors_on_verified_6k(tmp_path: Path) -> None:
    data = filings()
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date())
    fetcher = RoutedFetcher(fixtures(data))
    speed = await audit_sources(fetcher, tmp_path, "BN", company, NOW, None, SixKResults(fetcher))
    assert speed.day == date(2026, 8, 13) and speed.confident and speed.form == "6-K"
    assert [timing.source for timing in speed.archive] == ["SEC 6-K acceptance"]


async def test_an_issuer_notice_filed_on_6k_can_schedule_but_never_alert(tmp_path: Path) -> None:
    data = filings(accepted="2026-10-09T21:00:03Z")
    routes = fixtures(data)
    routes[f"{folder(ORDINARY)}/exh_991.htm"] = (200, NOTICE_BODY)
    fetcher = RoutedFetcher(routes)
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date()).model_copy(
        update={
            "investigated": NOW.date(),
            "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
        }
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("BN", 1, NOW, company)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, print)
        await dates._check(row)
        found = calendar.find("BN")
        assert found and found.target and found.evidence
        assert found.target.expected == datetime(2026, 11, 12, 7, tzinfo=ET)
        assert "from issuer 6-K exhibit" in found.evidence
        recent = EdgarFilings(CIK, form="6-K")
        ordinary = recent.parse(await fetcher.request("GET", recent.url()))[0]
        assert await dates.sixk.verify(ordinary) is None


async def test_scorecard_explains_busy_and_missing_date_states(tmp_path: Path) -> None:
    async def audit(*args: object) -> Speed:
        return Speed(None)

    company = profile("BN", CIK, "Brookfield Corporation", filings(), NOW.date()).model_copy(
        update={
            "investigated": NOW.date(),
            "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
        }
    )
    busy = True
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("BN", 1, NOW, company)
        dates = DateDiscovery(calendar, RoutedFetcher({}), lambda: NOW, lambda: busy, print)
        progress = TrackProgress(
            calendar,
            store,
            cast(discord.abc.Messageable, FakeDiscord()),
            42,
            dict,
            lambda: NOW,
            lambda: busy,
            audit,
            date_phase=dates.phases.get,
        )
        assert "Speed history (waits for an open release window)" in embed_text(
            progress.render(row, None, False)
        )
        await dates._check(row)
        assert "Release date: waiting for an open release window to close" in embed_text(
            progress.render(row, None, False)
        )
        busy = False
        await dates._check(row)
        score = embed_text(progress.render(row, None, False))
        assert "results on Form 6-K" in score
        assert "⚠️ No release date found yet" in score and "`when`" in score and "`period`" in score
