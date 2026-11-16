import json
from datetime import UTC, date, datetime
from pathlib import Path

from earnings_bot.issuers import profile, usual_release_time
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.recorder import ET
from earnings_bot.sec_results import SixKResults
from earnings_bot.source_audit import (
    Speed,
    Timing,
    _archived_sources,
    _measurements,
    audit_sources,
    release_history,
    site_local_time,
)
from tests.brookfield_data import CIK, RELEASE, RESULT, filings, fixtures, folder
from tests.builders import listing, row, sec_filing_detail
from tests.fakes import RoutedFetcher

NOW = datetime(2026, 11, 9, 17, tzinfo=ET)
LATER = "0001171843-26-005999"


def test_last_release_ranks_body_separately_from_headline(tmp_path: Path) -> None:
    def event(kind: str, clock: str, source: str, **fields: object) -> dict:
        stamp = f"2026-11-05T{clock}+00:00"
        return dict(event=kind, at=stamp, seen=stamp, source=source, ticker="NET", **fields)

    events = [
        # When the watch began, so publishers' stamps from before it can count
        event("window", "21:00:00", "", start="2026-11-05T16:00:00-05:00"),
        event("new", "21:15:01", "ir", title="Third Quarter Earnings", earnings=True),
        event("new", "21:15:02", "wire", title="Third Quarter Earnings", earnings=True),
        event(
            "new", "21:14:58", "late", title="Third Quarter Earnings", earnings=True, caught_up=True
        ),
        event("source_body", "21:15:03", "wire"),
        event("source_body", "21:15:05", "ir"),
        event(
            "benchmark",
            "21:30:00",
            "",
            x_post="https://x.com/earnings_guy/status/123",
            x_posted="2026-11-05T21:15:04+00:00",
            discord_alert="2026-11-05T21:15:02+00:00",
        ),
    ]
    (tmp_path / "watch.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    (tmp_path / "replay-NET.jsonl").write_text(
        json.dumps(event("source_body", "21:14:00", "replay")), encoding="utf-8"
    )
    headlines, bodies, comparison, watched = _measurements(tmp_path, "NET", date(2026, 11, 5))
    assert watched == datetime(2026, 11, 5, 21, tzinfo=UTC)
    speed = Speed(date(2026, 11, 5), measured_headlines=headlines, measured_text=bodies)
    basis, order = speed.fastest()
    assert basis == "seen live by the bot"
    assert [(t.source, t.at.second) for t in order] == [("ir", 1), ("wire", 2)]
    assert [t.source for t in sorted(bodies, key=lambda t: t.at)] == ["wire", "ir"]
    assert comparison == (
        "[@earnings_guy posted](https://x.com/earnings_guy/status/123), "
        "and our alert went out 2.00s before it"
    )
    assert not _measurements(tmp_path, "NET", date(2026, 8, 1))[0]


def test_the_archive_reads_one_copy_of_each_feed() -> None:
    now = datetime(2026, 11, 9, 16, tzinfo=ET)
    company = KnownCompany(
        name="Acme Pharma",
        names=["Acme Pharma"],
        cik=1,
        platform="own-site",
        reachable=False,
        wire="PR Newswire",
        fastest="newswire",
        evidence="a made-up issuer",
        checked=date(2026, 11, 1),
    )
    names = [source.name for source in _archived_sources("ACME", company, now)]
    assert names == ["prnewswire"]  # not its cache-busted copy as well


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-05T{clock}-05:00")


def test_live_sightings_beat_publisher_timestamps_and_timestamps_beat_nothing() -> None:
    archive = [Timing("SEC 8-K acceptance", _at("16:08:17")), Timing("q4_press", _at("16:05:00"))]
    live = [Timing("edgar_latest", _at("16:08:53")), Timing("prnewswire", _at("16:06:02"))]

    assert Speed(date(2026, 11, 5), archive=archive).fastest() == (
        "publishers' timestamps",
        [archive[1], archive[0]],
    )
    assert Speed(date(2026, 11, 5), archive=archive, measured_headlines=live).fastest() == (
        "seen live by the bot",
        [live[1], live[0]],
    )
    assert Speed(date(2026, 11, 5)).fastest() == ("", [])
    # On, Nov 10: its IR feed had the release at 5:00 AM, before the watch opened at 5:10, and
    # the bot's first live sighting was SEC's 6-K at 6:07
    q4 = Timing("q4_press", _at("05:00:00"))
    sec = Timing("edgar_latest", _at("06:07:05"))
    early = Speed(
        date(2026, 11, 5), archive=[q4], measured_headlines=[sec], watched_from=_at("05:10:00")
    )
    assert early.fastest() == (
        "seen live by the bot, and publishers' earlier timestamps",
        [q4, sec],
    )
    # A stamp from while the bot was watching may only be when the release was scheduled
    assert Speed(
        date(2026, 11, 5), archive=archive, measured_headlines=live, watched_from=_at("15:05:00")
    ).fastest() == ("seen live by the bot", [live[1], live[0]])


def test_a_saved_speed_check_reads_back_and_an_old_one_doesnt() -> None:
    speed = Speed(
        date(2026, 11, 5),
        confident=True,
        form="6-K",
        measured_text=[Timing("prnewswire", _at("16:06:03"), "https://example.com/a")],
        measured_headlines=[Timing("prnewswire", _at("16:06:02"))],
        archive=[Timing("SEC 6-K acceptance", _at("16:30:00"))],
        verified=["edgar"],
        benchmark="[@earnings_guy posted](https://x.com/a)",
        ir_json="`GET /feed`",
        history=[_at("16:05:00"), datetime(2026, 8, 5, 16, 5, tzinfo=ET)],
    )
    assert Speed.from_json(speed.to_json()) == speed
    # Saved before the audit kept the release history: read back without one
    older = {key: value for key, value in json.loads(speed.to_json()).items() if key != "history"}
    assert Speed.from_json(json.dumps(older)) == Speed(**{**speed.__dict__, "history": None})
    assert Speed.from_json(Speed(None).to_json()) == Speed(None)
    # What the audit saved before it kept its findings as JSON
    assert Speed.from_json("**Last release | Nov 05, 2026**\nMeasured headlines | ...") is None
    assert Speed.from_json(None) is None
    assert Speed.from_json('{"day": null}') is None


ROAD_CIK = 1718227
ROAD_FEED = "https://ir.constructionpartners.net/news/press-releases/rss"
ROAD_NEWS = "https://ir.constructionpartners.net/news/press-releases/detail"
ROAD_BODY = (
    "<p>DOTHAN, Ala. -- Construction Partners, Inc. today reported financial results for the "
    "quarter ended June 30, 2026.</p>"
    + "<p>Revenues of $900 million and net income of $60 million for the quarter. </p>" * 35
    + "<p>Visit www.constructionpartners.net for more information.</p>"
)
ROAD_PRELIMINARY = (
    "Construction Partners, Inc. Announces Preliminary Fiscal 2026 Financial Results and "
    "Introduces Fiscal 2027 Outlook"
)


async def test_the_release_history_leaves_out_preliminary_results(tmp_path: Path) -> None:
    """A company like Construction Partners: full results at 7:00 AM, and a preliminary release
    at 8:30 AM two weeks before its next full results. SEC accepts its 8-Ks an hour or more after
    the release."""
    filings = [
        # accession, items, accepted (UTC), headline of its exhibit
        ("0001718227-26-000050", "2.02,7.01,9.01", "2026-11-05T14:10:00Z", ROAD_PRELIMINARY),
        ("0001718227-26-000040", "5.02", "2026-09-01T20:30:00Z", ""),
        (
            "0001718227-26-000030",
            "2.02,9.01",
            "2026-08-07T12:04:00Z",
            "Construction Partners, Inc. Announces Fiscal 2026 Third Quarter Results",
        ),
        (
            "0001718227-26-000020",
            "2.02,9.01",
            "2026-05-08T13:10:00Z",
            "Construction Partners, Inc. Announces Fiscal 2026 Second Quarter Results",
        ),
        (
            "0001718227-26-000010",
            "2.02,9.01",
            "2026-02-05T14:34:00Z",
            "Construction Partners, Inc. Announces Fiscal 2026 First Quarter Results",
        ),
    ]
    submissions = {
        "fiscalYearEnd": "0930",
        "filings": {
            "recent": {
                "accessionNumber": [row[0] for row in filings],
                "form": ["8-K"] * len(filings),
                "items": [row[1] for row in filings],
                "acceptanceDateTime": [row[2] for row in filings],
                "primaryDocument": ["form8-k.htm"] * len(filings),
            }
        },
    }
    routes = {f"CIK{ROAD_CIK:010d}.json": (200, json.dumps(submissions).encode())}
    for accession, _, _, headline in filings:
        folder = f"/Archives/edgar/data/{ROAD_CIK}/{accession.replace('-', '')}"
        listing = {"directory": {"item": [{"name": "form8-k.htm"}, {"name": "ex991.htm"}]}}
        routes[f"{folder}/index.json"] = (200, json.dumps(listing).encode())
        routes[f"{folder}/ex991.htm"] = (200, f"<h1>{headline}</h1>{ROAD_BODY}".encode())
    items = [
        # title, published, its page
        (
            "Construction Partners, Inc. to Participate in a Global Industrial Conference",
            "Fri, 06 Nov 2026 08:30:00 -0500",
            "171",
        ),
        (ROAD_PRELIMINARY, "Thu, 05 Nov 2026 08:30:00 -0500", "170"),
        # A results headline on a day with no results 8-K, whose page is gone, isn't a release
        (
            "Construction Partners, Inc. Announces Fiscal 2025 Fourth Quarter Results",
            "Tue, 15 Sep 2026 07:00:00 -0400",
            "165",
        ),
        (
            "Construction Partners, Inc. Announces Fiscal 2026 Third Quarter Results",
            "Fri, 07 Aug 2026 07:00:00 -0400",
            "163",
        ),
        (
            "Construction Partners, Inc. Announces Fiscal 2026 Second Quarter Results",
            "Fri, 08 May 2026 07:00:00 -0400",
            "160",
        ),
        # Posted on the IR site after SEC had it, so SEC's time stands
        (
            "Construction Partners, Inc. Announces Fiscal 2026 First Quarter Results",
            "Thu, 05 Feb 2026 09:40:00 -0500",
            "156",
        ),
    ]
    rss = "".join(
        f"<item><title>{title}</title><link>{ROAD_NEWS}/{page}/release</link>"
        f"<pubDate>{published}</pubDate></item>"
        for title, published, page in items
    )
    routes[ROAD_FEED] = (200, f"<rss><channel>{rss}</channel></rss>".encode())
    for title, _, page in items:
        if page != "165":
            routes[f"{ROAD_NEWS}/{page}/"] = (200, f"<h1>{title}</h1>{ROAD_BODY}".encode())
    company = profile(
        "ROAD", ROAD_CIK, "Construction Partners, Inc.", submissions, date(2026, 11, 14)
    ).model_copy(update={"rss": [ROAD_FEED]})
    now = datetime(2026, 11, 14, 12, tzinfo=ET)

    speed = await audit_sources(RoutedFetcher(routes), tmp_path, "ROAD", company, now, None)

    assert speed.history == [
        datetime(2026, 8, 7, 7, tzinfo=ET),
        datetime(2026, 5, 8, 7, tzinfo=ET),
        datetime(2026, 2, 5, 9, 34, tzinfo=ET),
    ]
    assert usual_release_time(speed.history or []) == "07:00"
    # The speed check is of the last full results too
    assert speed.day == date(2026, 8, 7)
    assert ("rss:ir.constructionpartners.net", datetime(2026, 8, 7, 7, tzinfo=ET)) in [
        (timing.source, timing.at) for timing in speed.archive
    ]


async def test_a_q4_time_counts_as_eastern_only_near_the_results_8k() -> None:
    """A Q4 site's stamps name no zone. Alphabet's are Pacific: 1:01 PM for a release SEC had
    as an 8-K at 4:04 PM Eastern."""
    q4 = {
        "GetPressReleaseListResult": [
            {
                "PressReleaseId": 2,
                "Headline": "Alphabet Announces Third Quarter 2026 Results",
                "LinkToDetailPage": "/news/q3",
                "PressReleaseDate": "10/28/2026 13:01:00",
            },
            {
                "PressReleaseId": 1,
                "Headline": "Alphabet Announces Second Quarter 2026 Results",
                "LinkToDetailPage": "/news/q2",
                "PressReleaseDate": "07/22/2026 13:01:00",
            },
        ]
    }
    fetcher = RoutedFetcher({"GetPressReleaseList": (200, json.dumps(q4).encode())})
    company = known_companies()["GOOGL"].model_copy(
        update={"q4_host": "abc.example.com", "reachable": True, "sec_form": "8-K"}
    )
    pacific = [datetime(2026, 10, 28, 16, 4, tzinfo=ET), datetime(2026, 7, 22, 16, 3, tzinfo=ET)]
    eastern = [datetime(2026, 10, 28, 13, 4, tzinfo=ET), datetime(2026, 7, 22, 12, 58, tzinfo=ET)]

    assert await release_history(fetcher, company, [], pacific) == []
    # Read as Eastern when the 8-K agrees, and timed by SEC when SEC had it first
    assert await release_history(fetcher, company, [], eastern) == [
        datetime(2026, 10, 28, 13, 1, tzinfo=ET),
        datetime(2026, 7, 22, 12, 58, tzinfo=ET),
    ]


def globe_page(story: str) -> bytes:
    """A GlobeNewswire article page: the site's navigation, then the story."""
    navigation = "".join(f"<p>Industry News {n}</p>" for n in range(46))
    body = "<p>The company reported growth across its fleet and markets this quarter.</p>" * 30
    return f"<html><body>{navigation}{story}{body}</body></html>".encode()


async def test_a_globenewswire_issuers_history_reads_releases_without_results_headlines() -> None:
    """WeRide: its IR feed reaches back only to August, and two of its last four results releases
    had headlines like "Global Expansion Accelerates: Landmark UAE Driverless Robotaxi...". It
    files a 6-K the same day or the next."""
    releases = listing(
        row(
            "November 09, 2026 00:21",
            "WeRide Inc.",
            "2026/11/09/3398038",
            "WeRide to Report Third Quarter 2026 Financial Results on Friday, November 20, 2026",
        ),
        row(
            "October 27, 2026 23:27",
            "WeRide Inc.",
            "2026/10/27/3389839",
            "WeRide Insurance Partnership Program to Lift Fleet Revenue",
        ),
        row(
            "October 23, 2026 08:00",
            "WeRide Inc.",
            "2026/10/23/3387169",
            "WeRide Named Among Top 10 Automotive AI Companies",
        ),
        row(
            "August 12, 2026 07:00",
            "WeRide Inc.",
            "2026/08/12/3343512",
            "Accelerating European Expansion; 2Q2026 Total Revenue Reached RMB231.7 Million, Up "
            "82.2% Year over Year",
        ),
        row(
            "May 13, 2026 05:40",
            "WeRide Inc.",
            "2026/05/13/3293762",
            "WRD 3.0 Becomes Champion, WeRide Reports Record Q1 2026 Revenue, up 58%",
        ),
        row(
            "March 23, 2026 07:10",
            "WeRide Inc.",
            "2026/03/23/3260264",
            "WeRide Reports Record Full Year 2025 Revenue of RMB684.6 Million, Up 90%",
        ),
        row(
            "November 24, 2025 04:00",
            "WeRide Inc.",
            "2025/11/24/3193234",
            "Global Expansion Accelerates: UAE Permit, Record YoY Revenue Growth of 144.3%",
        ),
    )
    routes = {
        "/en/search/keyword/WeRide": (200, releases),
        "WeRide%2520Inc%C2%A7?page=1": (200, releases),
        "news-release/2025/11/24/3193234": (
            200,
            globe_page(
                "<p>WeRide Announces Unaudited Financial Results for Q3 2025</p><p>NEW YORK, Nov. "
                "24, 2025 (GLOBE NEWSWIRE) -- WeRide Inc. today announced its unaudited financial "
                "results for the three months ended September 30, 2025.</p><p>Total revenue grew "
                "144.3% year-over-year to RMB171.0 million.</p>"
            ),
        ),
        "news-release/2026/03/23/3260264": (
            200,
            globe_page(
                "<p>NEW YORK, March 23, 2026 (GLOBE NEWSWIRE) -- WeRide Inc. today announced its "
                "unaudited financial results for the fourth quarter and full year 2025.</p>"
                "<p>Total revenue grew 90% to RMB684.6 million for the full year.</p>"
            ),
        ),
        "news-release/2026/10/27/3389839": (
            200,
            globe_page(
                "<p>NEW YORK, Oct. 27, 2026 (GLOBE NEWSWIRE) -- WeRide today announced an "
                "insurance partnership program. Revenue from partners grew 40%.</p>"
            ),
        ),
    }
    fetcher = RoutedFetcher(routes)
    company = known_companies()["NVDA"].model_copy(
        update={
            "name": "WeRide Inc.",
            "names": ["NASDAQ: WRD"],
            "wire": "GlobeNewswire",
            "sec_form": "6-K",
            "q4_host": None,
        }
    )
    sixks = [
        datetime(2025, 11, 24, 6, tzinfo=ET),
        datetime(2026, 3, 23, 8, tzinfo=ET),
        datetime(2026, 5, 13, 7, tzinfo=ET),
        datetime(2026, 8, 12, 8, tzinfo=ET),
        datetime(2026, 10, 23, 9, tzinfo=ET),
        datetime(2026, 10, 28, 9, tzinfo=ET),
    ]

    history = await release_history(fetcher, company, [], sixks)

    assert history == [
        datetime(2026, 8, 12, 7, tzinfo=ET),
        datetime(2026, 5, 13, 5, 40, tzinfo=ET),
        datetime(2026, 3, 23, 7, 10, tzinfo=ET),
        datetime(2025, 11, 24, 4, tzinfo=ET),
    ]
    read = [url for _, url, _ in fetcher.requests if "/news-release/" in url]
    # The partnership with "Revenue" in its headline was read and turned down; the award, the
    # date notice and the results headlines weren't read
    assert len(read) == 3 and any("3389839" in url for url in read)
    assert not any("3387169" in url for url in read)


def test_a_check_saved_by_an_older_release_history_reads_back_without_it() -> None:
    """So the bot redoes it (TrackProgress): WeRide's saved history held one release of four."""
    history = [datetime(2026, 8, 12, 7, tzinfo=ET)]
    saved = json.loads(Speed(None, form="6-K", history=history).to_json())
    assert Speed.from_json(json.dumps(saved)).history == history  # type: ignore[union-attr]
    del saved["history_version"]
    assert Speed.from_json(json.dumps(saved)).history is None  # type: ignore[union-attr]


async def test_source_audit_anchors_on_verified_6k(tmp_path: Path) -> None:
    data = filings()
    company = profile("BN", CIK, "Brookfield Corporation", data, NOW.date())
    fetcher = RoutedFetcher(fixtures(data))
    speed = await audit_sources(fetcher, tmp_path, "BN", company, NOW, None, SixKResults(fetcher))
    assert speed.day == date(2026, 8, 13) and speed.confident and speed.form == "6-K"
    assert [timing.source for timing in speed.archive] == ["SEC 6-K acceptance"]


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


def test_a_q4_time_counts_as_eastern_only_when_sec_confirms_it() -> None:
    sec = [Timing("SEC 8-K acceptance", datetime(2026, 8, 13, 16, 3, 36, tzinfo=ET))]
    assert site_local_time("08/13/2026 16:01:00", sec) == datetime(2026, 8, 13, 16, 1, tzinfo=ET)
    # In another zone, or on another day, it doesn't line up with SEC's time
    assert site_local_time("08/13/2026 22:01:00", sec) is None
    assert site_local_time("08/13/2026 06:01:00", sec) is None
    assert site_local_time("08/13/2026 16:01:00", []) is None
