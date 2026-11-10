import asyncio
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from earnings_bot import recorder
from earnings_bot.calendar import Calendar, describe
from earnings_bot.date_discovery import CALENDAR_6K_DATE, DateDiscovery, calendar_date
from earnings_bot.delivery import DeliveryStore
from earnings_bot.investigate import INVESTIGATION
from earnings_bot.issuers import profile
from earnings_bot.known import SourceCheck
from earnings_bot.recorder import ET, Journal, Polls, Target, Watch, follow_up
from earnings_bot.sec_results import SixKResults, is_results_release
from earnings_bot.source_audit import Speed, Timing, audit_sources, site_local_time
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
    assert not is_results_release(
        "Company previously reported second quarter 2026 results. Revenue was $1 billion.\n" + text,
        description,
    )


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
        row, _ = calendar.add("BN", old)
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


async def test_klarna_style_results_exhibit_leads_to_issuer_q4_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("earnings_bot.date_discovery.chrome_available", lambda: False)
    cik = 2003292
    accession = "0001628280-26-057573"
    path = f"/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    host = "investors.klarna.com"
    release = (
        "<h1>Klarna Reports Second Quarter 2026 Results</h1>"
        "<p>Klarna Group plc today reported second quarter 2026 results. "
        "Revenue reached $1 billion.</p>"
        + "<p>Revenues and net income. </p>" * 100
        + f"<p>See https://{host}/financials/quarterly-results/.</p>"
    )
    notice = "/News--Events/news/news-details/2026/earnings-notice/default.aspx"
    data = {
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["6-K"],
                "items": [""],
                "acceptanceDateTime": ["2026-08-18T14:37:37Z"],
                "primaryDocument": ["cover.htm"],
            }
        },
    }
    q4 = {
        "GetPressReleaseListResult": [
            {
                "Headline": "Klarna to Publish Q3 2026 Earnings on November 17, 2026",
                "Body": (
                    "<p>Klarna will publish its Q3 2026 earnings on Tuesday, "
                    "November 17, 2026, before market open.</p>"
                ),
                "LinkToDetailPage": notice,
            },
            {
                "Headline": "Klarna Reports Second Quarter 2026 Results",
                "Body": "<p>NEW YORK--(BUSINESS WIRE)-- Klarna reported its results.</p>",
            },
        ]
    }
    fetcher = RoutedFetcher(
        {
            f"CIK{cik:010d}.json": (200, json.dumps(data).encode()),
            f"{path}/{accession}-index.htm": (
                200,
                sec_filing_detail(f"{path}/cover.htm", "results.htm"),
            ),
            f"{path}/results.htm": (200, release.encode()),
            "pageSize=20": (200, json.dumps(q4).encode()),
            "pageSize=10": (
                200,
                json.dumps(
                    {"GetPressReleaseListResult": q4["GetPressReleaseListResult"][:1]}
                ).encode(),
            ),
            f"https://{host}/": (200, b"Investor relations"),
        }
    )
    saved = profile("KLAR", cik, "Klarna Group plc", data, NOW.date()).model_copy(
        update={"investigated": NOW.date(), "investigation": 8}
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("KLAR", saved)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
        await dates.run_once(row)
        [found] = calendar.entries()

    assert found.company.sixk == ["10:37"]
    assert found.company.wire == "Business Wire"
    assert found.target and found.target.period == "FY2026Q3"
    assert found.target.expected == datetime(2026, 11, 17, 7, tzinfo=ET)
    assert found.target.q4_host == host and found.target.sec_form == "6-K"
    assert found.evidence and f"https://{host}{notice}" in found.evidence


async def test_an_issuer_notice_filed_on_6k_can_schedule_but_never_alert(tmp_path: Path) -> None:
    data = filings(accepted="2026-10-09T21:00:03Z")
    routes = fixtures(data)
    routes[f"{folder(ORDINARY)}/exh_991.htm"] = (200, NOTICE_BODY)
    fetcher = RoutedFetcher(routes)
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date()).model_copy(
        update={
            "investigated": NOW.date(),
            "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
            "investigation": INVESTIGATION,
        }
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("BN", company)
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
            "investigation": INVESTIGATION,
        }
    )
    busy = True
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("BN", company)
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
        assert "⚠️ No release date read yet" in score and "`when`" in score and "`period`" in score


async def test_the_ir_sites_own_time_for_the_release_joins_the_comparison(
    tmp_path: Path,
) -> None:
    data = filings()
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date()).model_copy(
        update={
            "ir_posted": datetime(2026, 8, 13, 6, 40, tzinfo=ET),
            "ir_release": "https://bn.brookfield.com/q2",
        }
    )
    fetcher = RoutedFetcher(fixtures(data))
    speed = await audit_sources(fetcher, tmp_path, "BN", company, NOW, None, SixKResults(fetcher))
    _, order = speed.fastest()
    assert next((t.source, t.url) for t in order) == ("browser-ir", "https://bn.brookfield.com/q2")
    assert [t.source for t in order] == ["browser-ir", "SEC 6-K acceptance"]

    # A time for another release doesn't count
    company = company.model_copy(update={"ir_posted": datetime(2026, 5, 14, 6, 40, tzinfo=ET)})
    speed = await audit_sources(fetcher, tmp_path, "BN", company, NOW, None, SixKResults(fetcher))
    assert [t.source for t in speed.archive] == ["SEC 6-K acceptance"]


# WeRide's results 6-Ks of Aug 12, 2026 open with the Hong Kong exchange's disclaimers
HKEX_DISCLAIMER = (
    "Hong Kong Exchanges and Clearing Limited and The Stock Exchange of Hong Kong Limited take "
    "no responsibility for the contents of this announcement, make no representation as to its "
    "accuracy or completeness and expressly disclaim any liability whatsoever for any loss "
    "howsoever arising from or in reliance upon the whole or any part of the contents of this "
    "announcement. "
)
FIGURES = [
    "Total revenue | 231.7 | 127.2",
    "Revenue increased by 82.2% to RMB231.7 million. Net loss per share was RMB0.92.",
] * 30
# The press release itself, as Schedule I after the exchange's announcement
SCHEDULE = "\n".join(
    [
        "Exhibit 99.1",
        HKEX_DISCLAIMER * 4 + "This announcement contains forward-looking statements. " * 20,
        "Schedule I",
        "WeRide Inc. Announces Second Quarter and First Half 2026 Unaudited Financial Results",
        (
            "NEW YORK, August 12, 2026 (GLOBE NEWSWIRE) - WeRide Inc. today announced its "
            "unaudited financial results for the second quarter and first half ended June 30, 2026."
        ),
        *FIGURES,
    ]
)
# The exchange's own interim results announcement, filed the same afternoon
INTERIM = "\n".join(
    [
        "Exhibit 99.1",
        HKEX_DISCLAIMER,
        "INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED JUNE 30, 2026",
        (
            "The Company is pleased to announce the unaudited consolidated quarterly and first "
            "half results of the Company for the three months and first half ended June 30, 2026."
        ),
        *FIGURES,
    ]
)


def test_a_press_release_after_the_exchanges_disclaimers_is_recognized_as_results() -> None:
    assert SCHEDULE.index("today announced") > 2500
    assert is_results_release(SCHEDULE, "EXHIBIT 99.1")


def test_pleased_to_announce_results_is_results_and_not_a_date_notice() -> None:
    assert is_results_release(INTERIM, "EXHIBIT 99.1")
    notice = INTERIM.replace("is pleased to announce the", "will announce its")
    assert not is_results_release(notice, "EXHIBIT 99.1")


LATER = "0001171843-26-005999"


async def test_the_speed_review_times_sec_by_the_days_first_results_6k(tmp_path: Path) -> None:
    # Like WeRide on Aug 12: the press release at 9:22 AM, the Hong Kong filing at 4:03 PM
    data = {
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [LATER, RESULT],
                "form": ["6-K", "6-K"],
                "items": ["", ""],
                "acceptanceDateTime": ["2026-08-13T20:03:22Z", "2026-08-13T10:45:04Z"],
                "primaryDocument": ["cover.htm", "cover.htm"],
            }
        },
    }
    routes = {f"CIK{CIK:010d}.json": (200, json.dumps(data).encode())}
    for key in (LATER, RESULT):
        base = folder(key)
        routes[f"{base}/{key}-index.htm"] = (
            200,
            sec_filing_detail(f"{base}/cover.htm", "exh_991.htm"),
        )
        routes[f"{base}/exh_991.htm"] = (200, RELEASE.encode())
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date())
    fetcher = RoutedFetcher(routes)

    speed = await audit_sources(fetcher, tmp_path, "BN", company, NOW, None, SixKResults(fetcher))

    [sec] = [timing for timing in speed.archive if timing.source == "SEC 6-K acceptance"]
    assert sec.at == datetime(2026, 8, 13, 10, 45, 4, tzinfo=UTC)


def test_reports_third_quarter_results_is_an_announcement() -> None:
    # CGI's press release, the third exhibit of its results 6-K
    release = "\n".join(
        [
            "GLOBAL PRESS RELEASE | Stock Market Symbols | GIB.A (TSX) | GIB (NYSE)",
            "CGI reports third quarter Fiscal 2026 results",
            "Revenue up 2.5% and diluted EPS accretion of 22.5%",
            *FIGURES,
        ]
    )
    assert is_results_release(release, "EX-99.3")
    notice = release.replace("CGI reports third quarter", "CGI to host third quarter")
    assert not is_results_release(notice, "EX-99.3")


def test_a_q4_time_counts_as_eastern_only_when_sec_confirms_it() -> None:
    sec = [Timing("SEC 8-K acceptance", datetime(2026, 8, 13, 16, 3, 36, tzinfo=ET))]
    assert site_local_time("08/13/2026 16:01:00", sec) == datetime(2026, 8, 13, 16, 1, tzinfo=ET)
    # In another zone, or on another day, it doesn't line up with SEC's time
    assert site_local_time("08/13/2026 22:01:00", sec) is None
    assert site_local_time("08/13/2026 06:01:00", sec) is None
    assert site_local_time("08/13/2026 16:01:00", []) is None


async def test_a_results_6k_can_begin_after_a_full_cover_page() -> None:
    cover = "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nFORM 6-K\n"
    cover += "Cover page item\n" * 15
    report = (
        "Company Reports Second Quarter 2026 Financial Results\n"
        "The company announced its financial results for the quarter ended June 30, 2026.\n"
        + "Revenue of $4 billion and net income of $300 million.\n"
        * 55
    )
    url = "https://www.sec.gov" + folder(RESULT)
    filename = f"{url}/cover.htm"
    filing = Sighting(RESULT, "6-K", filename, details={"form": "6-K"})
    fetcher = RoutedFetcher({f"{url}/{RESULT}-index.htm": (200, b"Document Format Files")})
    unexpected = cover + "Yes No (X)\nNon-results disclosure\n" + "Cover page item\n" * 20 + report
    for body, expected in ((unexpected, False), (cover + "Yes No (X)\n" + report, True)):
        fetcher.routes[filename] = (
            200,
            "".join(f"<p>{line}</p>" for line in body.splitlines()).encode(),
        )
        assert bool(await SixKResults(fetcher).verify(filing)) is expected


async def test_followup_verifies_a_results_6k_accepted_after_the_window() -> None:
    journal = Journal(io.StringIO())
    now = journal.now()
    target = Target(
        ticker="BN",
        cik=CIK,
        expected=now - timedelta(minutes=1),
        before_minutes=2,
        after_minutes=0,
        sec_form="6-K",
        period="FY2026Q2",
    )
    fetcher = RoutedFetcher(fixtures(filings(accepted=now.isoformat())))
    # An uninitialized watcher also happens when the bot resumes a delivered alert.
    watch_state = Watch("BN", EdgarFilings(CIK, form="6-K"), target.window, period=target.period)
    seen: list[Sighting] = []

    await follow_up(
        target,
        [watch_state],
        fetcher,
        journal,
        lambda _, __, item: seen.append(item),
        SixKResults(fetcher),
        longest=timedelta(minutes=1, milliseconds=100),
    )

    assert [item.key for item in seen if item.earnings] == [RESULT]


async def test_after_midnight_6k_is_verified_and_catches_up_inside_the_window() -> None:
    start = datetime(2026, 11, 11, 23, 55, tzinfo=ET)
    accepted = start + timedelta(minutes=10)
    source = EdgarFilings(CIK, form="6-K")
    w = Watch("BN", source, (start, start + timedelta(minutes=40)), period="FY2026Q2")
    fetcher = RoutedFetcher(fixtures(filings(accepted=accepted.isoformat())))
    journal = Journal(io.StringIO())
    response = await fetcher.request("GET", source.url())
    items = await recorder._verify_sixks(w, response, SixKResults(fetcher), journal)
    sightings: list[Sighting] = []

    recorder.record_items(
        w, items, response, journal, accepted, lambda _, item: sightings.append(item)
    )

    assert [item.key for item in sightings if item.earnings and item.caught_up] == [RESULT]
