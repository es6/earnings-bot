import json
from datetime import UTC, date, datetime
from pathlib import Path

from earnings_bot.issuers import profile, usual_release_time
from earnings_bot.known import known_companies
from earnings_bot.recorder import ET
from earnings_bot.source_audit import (
    Speed,
    Timing,
    _archived_sources,
    _measurements,
    audit_sources,
    release_history,
)
from tests.fakes import RoutedFetcher


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
    names = [source.name for source in _archived_sources("LLY", known_companies()["LLY"], now)]
    assert names == ["prnewswire"]


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
