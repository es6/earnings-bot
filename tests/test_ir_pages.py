"""Reading an IR site the way Chrome shows it, for its release date."""

import re
from datetime import date, datetime

import pytest

from earnings_bot.browser import BrowserError, RenderedPage
from earnings_bot.date_discovery import EVENT_DATE, event_target, rendered_date, title_period
from earnings_bot.formatting import period_label
from earnings_bot.investigate import ir_hosts
from earnings_bot.ir_pages import (
    IRReader,
    Moment,
    earnings_event,
    headline_moments,
    moments,
    nav_rank,
    upcoming,
)
from earnings_bot.ir_research import prior_period, quarter_headline
from earnings_bot.issuers import fiscal_year_end
from earnings_bot.recorder import ET
from earnings_bot.sources import looks_like_earnings
from tests.fakes import rendered
from tests.test_date_discovery import AMAT

NOW = datetime(2026, 11, 10, 5, tzinfo=ET)
HOME = "https://ir.weride.ai/"
LIST = "https://ir.weride.ai/news-events/news-releases"
EVENTS = "https://ir.weride.ai/news-events/events"
NOTICE = (
    "https://ir.weride.ai/news-releases/news-release-details/"
    "weride-report-third-quarter-2026-financial-results-friday"
)
CALL = "https://ir.weride.ai/events/event-details/weride-inc-2026-q3-earnings-conference-call"
WERIDE = AMAT.model_copy(
    update={"name": "WeRide Inc.", "sec_form": "6-K", "release_time": "06:15", "cik": 1867729}
)

# WeRide's IR site as Chrome showed it on Nov 10, 2026. A plain request for the release list
# got a redirect back to itself, so only a browser could read it.
SITE = {
    HOME: (
        '<a href="/news-releases/news-release-details/accelerating">Accelerating European '
        "Expansion; 2Q2026 Total Revenue Reached RMB231.7 Million, Up 82.2% Year over Year</a>"
        '<a href="/news-releases/news-release-details/weride-report-second-quarter">WeRide to '
        "Report Second Quarter and First Half 2026 Financial Results on Wednesday, August 12, "
        "2026</a>"
        '<a href="/news-events/news-releases">News Releases</a>'
        '<a href="/news-events/events">Events</a>'
        '<a href="/financials-filings/sec-filings">SEC Filings</a>'
        '<a href="/zh-hans/news-events/news-releases">新闻稿</a>'
    ),
    LIST: (
        f'<a href="{NOTICE}">WeRide to Report Third Quarter 2026 Financial Results on Friday, '
        "November 20, 2026</a>"
        '<a href="/news-releases/news-release-details/weride-report-second-quarter">WeRide to '
        "Report Second Quarter and First Half 2026 Financial Results on Wednesday, August 12, "
        "2026</a>"
        '<a href="/news-releases/news-release-details/accelerating">Accelerating European '
        "Expansion; 2Q2026 Total Revenue Reached RMB231.7 Million, Up 82.2% Year over Year</a>"
    ),
    NOTICE: (
        "<h1>WeRide to Report Third Quarter 2026 Financial Results on Friday, November 20, "
        "2026</h1><p>GUANGZHOU, China, Nov. 09, 2026 (GLOBE NEWSWIRE) -- WeRide Inc. will "
        "report its unaudited financial results for the third quarter ended September 30, 2026 "
        "before the U.S. market opens on Friday, November 20, 2026.</p>"
    ),
    EVENTS: (
        f'<div><a href="{CALL}">WeRide Inc. 2026 Q3 Earnings Conference Call</a></div>'
        "<div>2026-11-20 8:00 AM EST</div>"
        '<div><a href="/events/event-details/q2">WeRide Inc. 2026 Q2 &amp; H1 Earnings '
        "Conference Call</a></div><div>2026-08-12 8:00 AM EDT</div>"
    ),
}


class Site:
    """Chrome on a fake IR site: each page as rendered, and which ones were opened."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.opened: list[str] = []

    async def render(self, url: str) -> RenderedPage:
        self.opened.append(url)
        if url not in self.pages:
            return rendered(url, 404, "")
        return rendered(url, 200, self.pages[url])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("WeRide Inc. 2026 Q3 Earnings Conference Call 2026-11-20 8:00 AM EST", (11, 20, 8, 0)),
        ("Thursday, November 19, 2026 at 4:30 p.m. ET", (11, 19, 16, 30)),
        ("Nov. 12, 2026 | 4:30 PM ET", (11, 12, 16, 30)),
        # Hong Kong's evening is New York's morning
        ("20 November 2026, 8:00 PM HKT", (11, 20, 7, 0)),
        ("Nov 18, 2026 1:00 PM PT", (11, 18, 16, 0)),
    ],
)
def test_dates_and_times_in_any_ir_format(text: str, expected: tuple[int, ...]) -> None:
    [moment] = moments(text)
    assert moment.at is not None
    assert (moment.at.month, moment.at.day, moment.at.hour, moment.at.minute) == expected


def test_a_date_without_a_time_is_kept_as_a_day() -> None:
    assert moments("Posted 11/19/2026") == [Moment(date(2026, 11, 19), None)]
    assert upcoming(moments("2026-08-12 and 2026-11-20"), date(2026, 11, 10)) == [
        Moment(date(2026, 11, 20), None)
    ]


@pytest.mark.parametrize(
    ("label", "rank"),
    [
        ("News Releases", 0),
        ("All News Releases", 0),
        ("Press Releases", 0),
        ("News & Events", 0),
        ("Newsroom", 0),
        ("Events", 1),
        ("Events & Presentations", 1),
        ("Quarterly Results", 2),
        ("Investors", 3),
        ("Investor Relations \u203a", 3),
        ("Read More", None),
        ("SEC Filings", None),
    ],
)
def test_navigation_is_recognized_by_what_it_says(label: str, rank: int | None) -> None:
    assert nav_rank(label) == rank


@pytest.mark.parametrize(
    ("title", "period"),
    [
        ("WeRide Inc. 2026 Q3 Earnings Conference Call", (2026, 3)),
        ("Q1 2027 Earnings Webcast", (2027, 1)),
        ("Third Quarter Fiscal 2026 Results Conference Call", (2026, 3)),
        ("Fiscal 2027 Second Quarter Earnings Call", (2027, 2)),
        ("WeRide to Report Second Quarter and First Half 2026 Financial Results", (2026, 2)),
        ("Q4 FY26 Keysight Technologies Inc. Earnings Conference Call", (2026, 4)),
        ("Fourth Quarter and Full Year 2026 Results", (2026, 4)),
        ("2026 Annual General Meeting", None),
    ],
)
def test_the_period_an_event_names(title: str, period: tuple[int, int] | None) -> None:
    assert title_period(title) == period


async def test_the_date_is_read_from_a_notice_found_through_the_sites_own_navigation() -> None:
    site = Site(SITE)
    unread: list[str] = []

    result, reading = await rendered_date(site.render, [HOME], "WRD", WERIDE, NOW, unread)

    assert result is not None
    target, evidence = result
    assert target.expected == datetime(2026, 11, 20, 6, 15, tzinfo=ET)
    assert target.period == "FY2026Q3"
    assert evidence == f"Issuer date confirmed, before market open <{NOTICE}>"
    assert reading.listing == LIST
    # Last quarter's notice, whose date has passed, isn't opened
    assert site.opened == [HOME, LIST, NOTICE]
    assert unread == []


async def test_without_a_notice_the_events_list_gives_the_call_and_so_the_release() -> None:
    pages = {**SITE, LIST: SITE[LIST].replace(NOTICE, "/gone")}
    del pages[NOTICE]
    site = Site(pages)

    result, _ = await rendered_date(site.render, [HOME], "WRD", WERIDE, NOW, [])

    # Last quarter's notice, dated Aug 12 in its headline, isn't worth opening
    assert not any("second-quarter" in url for url in site.opened)
    assert result is not None
    target, evidence = result
    # A 6-K can be filed hours after the release, so the call sets the window: results come
    # out before 8:00 AM, and the watch runs from 5:30 to 8:15
    assert target.expected == datetime(2026, 11, 20, 7, tzinfo=ET)
    assert target.window == (
        datetime(2026, 11, 20, 5, 30, tzinfo=ET),
        datetime(2026, 11, 20, 8, 15, tzinfo=ET),
    )
    assert evidence.startswith(f"{EVENT_DATE}: “WeRide Inc. 2026 Q3 Earnings Conference Call”")


@pytest.mark.parametrize(
    ("form", "history", "call", "expected", "window"),
    [
        # Past 8-Ks came out the evening before a morning call
        ("8-K", "16:05", (11, 20, 8, 30), (11, 19, 16, 5), ((11, 19, 15, 5), (11, 19, 17, 35))),
        # The release on the call's morning, at its usual time
        ("8-K", "06:45", (11, 24, 8, 0), (11, 24, 6, 45), ((11, 24, 5, 45), (11, 24, 8, 15))),
        # An afternoon call after a 4:05 PM release
        ("8-K", "16:05", (11, 18, 16, 30), (11, 18, 16, 5), ((11, 18, 15, 5), (11, 18, 16, 45))),
    ],
)
def test_an_earnings_call_places_the_release_by_the_companys_history(
    form: str,
    history: str,
    call: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
    window: tuple[tuple[int, ...], tuple[int, ...]],
) -> None:
    company = AMAT.model_copy(update={"sec_form": form, "release_time": history})
    month, day, hour, minute = call
    moment = Moment(date(2026, month, day), datetime(2026, month, day, hour, minute, tzinfo=ET))

    result = event_target("ACME", company, "Q3 2026 Earnings Conference Call", moment, NOW)

    assert result is not None
    target, _ = result
    assert target.expected == datetime(2026, *expected, tzinfo=ET)
    assert target.window == tuple(datetime(2026, *edge, tzinfo=ET) for edge in window)


async def test_the_reader_stays_within_its_page_budget() -> None:
    links = "".join(f'<a href="/news-releases?page={i}">News Releases</a>' for i in range(30))
    site = Site({HOME: links})
    reader = IRReader(site.render, [HOME], pages=4)

    async for _ in reader.pages():
        pass

    assert len(site.opened) == 4


async def test_the_reader_keeps_off_translated_copies_of_the_site() -> None:
    site = Site({HOME: '<a href="/zh-hans/news">News</a><a href="/en/news">News</a>'})
    reader = IRReader(site.render, [HOME])

    async for _ in reader.pages():
        pass

    assert site.opened == [HOME, "https://ir.weride.ai/en/news"]


async def test_a_page_chrome_cant_open_is_named_in_the_reading() -> None:
    async def broken(url: str) -> RenderedPage:
        raise BrowserError("net::ERR_CONNECTION_RESET")

    reader = IRReader(broken, [HOME])
    async for _ in reader.pages():
        pass

    assert reader.reading.summary() == (
        f"Chrome couldn't open its IR site: Chrome couldn't open <{HOME}> "
        "(net::ERR_CONNECTION_RESET)"
    )


@pytest.mark.parametrize(
    ("release", "ticker", "name", "hosts"),
    [
        # Best Buy names its IR site with a www. in front
        (
            "Contact mollie.obrien@bestbuy.com. Visit www.investors.bestbuy.com.",
            "BBY",
            "BEST BUY CO INC",
            ["www.investors.bestbuy.com", "bestbuy.com"],
        ),
        # Deere's site is johndeere.com
        ("Learn more at JohnDeere.com", "DE", "DEERE & CO", ["johndeere.com"]),
        # "investor.relations" is the start of an email address, not a site
        (
            "Contact investor.relations@hpe.com or visit www.hpe.com.",
            "HPE",
            "Hewlett Packard Enterprise Co",
            ["hpe.com"],
        ),
    ],
)
def test_ir_hosts_found_in_a_results_release(
    release: str, ticker: str, name: str, hosts: list[str]
) -> None:
    assert ir_hosts(release, ticker, name) == hosts


def test_a_results_headline_can_name_its_quarter_like_2q2026() -> None:
    # WeRide's Q2 2026 results headline names neither "results" nor "quarter"
    assert looks_like_earnings(
        "Accelerating European Expansion Through Proven Asset-Light Model; 2Q2026 Total Revenue "
        "Reached RMB231.7 Million"
    )
    assert looks_like_earnings("3Q 2026 Revenue Up 10%")
    assert not looks_like_earnings("WeRide Named Among Top 10 AI Cases")


ACME = AMAT.model_copy(
    update={"sec_form": "8-K", "release_time": "16:05", "fiscal_year_end": "0930"}
)


async def test_an_events_card_gives_the_date_wherever_it_puts_it() -> None:
    # HPE's IR home: each date comes before its title, and another event comes first
    home = (
        "<div><div>11/18/2026</div><div>HPE Networking Investor Day</div></div>"
        "<div><div>11/23/2026</div><div><a href='/#11-23-2026'>Q4 Fiscal Year 2026 HPE "
        "Earnings Conference Call</a></div><div><a href='/#11-23'>Learn More</a></div></div>"
        "<div><div>12/02/2026</div><div><a href='/#12-02'>UBS Global Technology Conference</a>"
        "</div></div>"
    )
    site = Site({HOME: home})

    result, _ = await rendered_date(site.render, [HOME], "HPE", ACME, NOW, [])

    assert result is not None
    target, _ = result
    # A date without a time: the usual 4:05 PM release that day
    assert target.expected == datetime(2026, 11, 23, 16, 5, tzinfo=ET)
    assert target.period == "FY2026Q4"


async def test_a_tentative_event_is_a_placeholder_not_a_date() -> None:
    # Lowe's IR site, Nov 10: its Q3 call "(tentative)" on Dec 9, while the company had said
    # Nov 18 elsewhere
    home = (
        "<div><a href='/q3'>Q3 2026 Lowe's Companies, Inc. Earnings Conference Call "
        "(tentative)</a> ADD TO CALENDAR 12/09/2026 8:00 AM EST</div>"
    )

    result, _ = await rendered_date(Site({HOME: home}).render, [HOME], "LOW", ACME, NOW, [])

    assert result is None


async def test_a_call_only_notice_places_the_release_before_the_call() -> None:
    # Autodesk's notice says only when the call is
    notice = "https://investors.autodesk.com/news-release/invitation"
    pages = {
        HOME: f"<a href='{notice}'>Autodesk extends invitation to join financial results "
        "conference call</a><a href='/x'>Autodesk named a Leader</a>",
        notice: "<p>SAN FRANCISCO, Nov. 2, 2026 /PRNewswire/ -- Autodesk, Inc. today announced "
        "it will share its third quarter fiscal 2027 financial results conference call via its "
        "website on Tuesday, November 24, 2026, at 2 p.m. Pacific Time (PT). An audio replay "
        "will be available after 5 p.m. PT.</p>",
    }

    result, _ = await rendered_date(Site(pages).render, [HOME], "ADSK", ACME, NOW, [])

    assert result is not None
    target, evidence = result
    # Its 8-Ks come at 4:05 PM ET, before the 5 PM ET call
    assert target.expected == datetime(2026, 11, 24, 16, 5, tzinfo=ET)
    assert target.period == "FY2027Q3"
    assert evidence.startswith(EVENT_DATE)


@pytest.mark.parametrize(
    ("title", "year_end", "period"),
    [
        # Keysight's "FY26"
        ("Q4 FY26 Keysight Technologies Inc. Earnings Conference Call", "1031", "FY2026Q4"),
        # Deere's names no year: the quarter that ended last before Nov 25
        ("John Deere - 4Q Earnings Call", "1031", "FY2026Q4"),
    ],
)
def test_an_events_period_from_its_title_or_the_fiscal_year(
    title: str, year_end: str, period: str
) -> None:
    company = ACME.model_copy(update={"fiscal_year_end": year_end, "release_time": "06:00"})
    moment = Moment(date(2026, 11, 25), datetime(2026, 11, 25, 9, tzinfo=ET))

    result = event_target("X", company, title, moment, NOW)

    assert result is not None
    assert result[0].period == period


async def test_glued_dates_still_show_which_notices_are_old() -> None:
    # Alibaba's home page runs each card's text together: "...on August 20, 2026August 7, 2026"
    old = "".join(
        f"<a href='/old{i}'>Alibaba Group Will Announce June Quarter 2026 Results on August "
        f"{20 - i}, 2026August {7 - i}, 2026</a>"
        for i in range(5)
    )
    current = "https://www.alibabagroup.com/en-US/document-2054756418245885952"
    pages = {
        HOME: old
        + f"<a href='{current}'>Alibaba Group Will Announce September Quarter 2026 Results on "
        "November 19, 2026November 6, 2026</a>",
        current: "<p>Alibaba Group Holding Limited will announce its September quarter 2026 "
        "results before the U.S. market opens on Thursday, November 19, 2026.</p>",
    }
    company = ACME.model_copy(update={"sec_form": "6-K", "fiscal_year_end": "0331"})

    result, _ = await rendered_date(Site(pages).render, [HOME], "BABA", company, NOW, [])

    assert result is not None
    target, _ = result
    assert target.expected.date() == date(2026, 11, 19)
    assert target.period == "FY2027Q2"


def test_a_fiscal_year_end_from_the_latest_annual_report_when_sec_gives_none() -> None:
    # Alibaba: SEC's fiscalYearEnd is empty, its 20-F covers the year to March 31
    submissions = {
        "fiscalYearEnd": None,
        "filings": {
            "recent": {"form": ["6-K", "20-F", "6-K"], "reportDate": ["", "2026-03-31", ""]}
        },
    }
    assert fiscal_year_end(submissions) == "0331"
    assert fiscal_year_end({"fiscalYearEnd": "1231"}) == "1231"


def test_a_year_run_into_the_next_word_is_still_a_date() -> None:
    assert [m.day for m in moments("on November 19, 2026November 6, 2026Alibaba")] == [
        date(2026, 11, 19),
        date(2026, 11, 6),
    ]


async def test_a_notice_dated_ahead_is_read_before_undated_ones() -> None:
    # Older notices whose headlines give no date come first in the list, more than the four
    # the reader opens
    old = "".join(
        f"<a href='/old{i}'>WeRide to Report Second Quarter 2026 Financial Results</a>"
        for i in range(5)
    )
    pages = {HOME: old + SITE[LIST], NOTICE: SITE[NOTICE]}
    for i in range(5):
        pages[f"https://ir.weride.ai/old{i}"] = "<p>WeRide will report its results soon.</p>"

    result, _ = await rendered_date(Site(pages).render, [HOME], "WRD", WERIDE, NOW, [])

    assert result is not None
    assert result[0].expected.date() == date(2026, 11, 20)


def test_a_6k_filers_time_from_its_ir_site_is_trusted_before_its_call() -> None:
    # A 6-K's own time can trail the release by hours; the IR site's time doesn't
    company = ACME.model_copy(
        update={"sec_form": "6-K", "release_time": "06:45", "release_published": True}
    )
    moment = Moment(date(2026, 11, 24), datetime(2026, 11, 24, 8, tzinfo=ET))

    result = event_target("X", company, "Q3 2026 Earnings Conference Call", moment, NOW)

    assert result is not None
    assert result[0].expected == datetime(2026, 11, 24, 6, 45, tzinfo=ET)


def test_an_event_naming_no_year_is_planned_as_the_company_names_its_quarters() -> None:
    # Home Depot, whose fiscal 2026 ends in January 2027, holds its call on November 17
    company = ACME.model_copy(
        update={"fiscal_year_end": "0131", "fiscal_year_offset": -1, "release_time": "06:00"}
    )
    moment = Moment(date(2026, 11, 17), datetime(2026, 11, 17, 9, tzinfo=ET))

    result = event_target("HD", company, "Q3 Earnings Conference Call", moment, NOW)

    assert result is not None
    period = result[0].period
    assert period == "FY2026Q3"
    assert period_label(period) == "Q3 FY26"
    # Its IR listing is verified by last quarter's headline, and watched for this one's
    last = re.compile(quarter_headline(*prior_period(period)), re.IGNORECASE)
    assert last.fullmatch(
        "The Home Depot Announces Second Quarter Fiscal 2026 Results; Reaffirms Fiscal 2026 "
        "Guidance"
    )
    this = re.compile(quarter_headline(2026, 3), re.IGNORECASE)
    assert this.fullmatch("The Home Depot Announces Third Quarter Fiscal 2026 Results")


DELL_TITLE = (
    "Dell Technologies to Hold Conference Call Nov. 24 to Discuss Third Quarter Fiscal 2027 "
    "Financial Results"
)


@pytest.mark.parametrize(
    ("headline", "named"),
    [
        # Dell's link, and the line its IR home shows it on, after the day it was posted
        (f"{DELL_TITLE} Read Press Release", [Moment(date(2026, 11, 24), None)]),
        (f"Nov 10, 2026 {DELL_TITLE} Read Press Release", [Moment(date(2026, 11, 24), None)]),
        # With its time, in Central
        (
            "Acme Conference Call Nov. 24 at 3:30 p.m. CT",
            [Moment(date(2026, 11, 24), datetime(2026, 11, 24, 16, 30, tzinfo=ET))],
        ),
        # WeRide's events page puts the date after the title
        (
            "WeRide Inc. 2026 Q3 Earnings Conference Call 2026-11-20 8:00 AM EST",
            [Moment(date(2026, 11, 20), datetime(2026, 11, 20, 8, tzinfo=ET))],
        ),
        # HPE's puts it first, and the headline names no other day: the caller keeps that one
        ("11/23/2026 Q4 Fiscal Year 2026 HPE Earnings Conference Call", []),
        # A quarter's end isn't the event's day
        ("Acme Conference Call on Results for the Quarter Ended Sept. 30", []),
        # A day that has passed this year is next year's
        ("Acme Conference Call Nov. 9", [Moment(date(2027, 11, 9), None)]),
    ],
)
def test_a_headline_names_its_own_day_with_or_without_a_year(
    headline: str, named: list[Moment]
) -> None:
    assert headline_moments(headline, NOW.date()) == named


DELL_HOME = "https://investors.delltechnologies.com/"
DELL_NOTICE = (
    "https://investors.delltechnologies.com/news-events/press-releases/detail/1661/"
    "dell-technologies-to-hold-conference-call-nov-24-to-discuss-third-quarter-fiscal-2027-"
    "financial-results"
)
# Dell's IR home and its notice as Chrome showed them on Nov 10, 2026. Each news item opens
# with the day it was posted
DELL_SITE = {
    DELL_HOME: (
        "<section><h2>Latest News</h2>"
        f"<p>Nov 10, 2026 <a href='{DELL_NOTICE}'>{DELL_TITLE} Read Press Release</a></p>"
        "<p>Sep 1, 2026 <a href='/news-events/press-releases/detail/1653/q2'>Dell Technologies "
        "Delivers Second Quarter Fiscal 2027 Financial Results Read Press Release</a></p>"
        "<p>Aug 25, 2026 <a href='/news-events/press-releases/detail/1650/conferences'>Dell "
        "Technologies to Present at Investor Conferences in September Read Press Release</a></p>"
        "</section>"
    ),
    DELL_NOTICE: (
        f"<h1>{DELL_TITLE}</h1><p>November 10, 2026 4:05pm EST</p>"
        "<p>ROUND ROCK, Texas--(BUSINESS WIRE)-- Dell Technologies (NYSE: DELL) will conduct a "
        "conference call Tuesday, Nov. 24, 2026, at 3:30 p.m. CST to discuss its fiscal 2027 "
        "third quarter financial results. The conference will be available to the public as a "
        "live, audio-only webcast on Dell Technologies\u2019 website at "
        "investors.delltechnologies.com.</p>"
        "<p>The company will issue the results before the conference call broadcast via a press "
        "release with accompanying financial statements and guidance.</p>"
    ),
}
DELL = ACME.model_copy(update={"fiscal_year_end": "0131", "release_time": "16:10"})


@pytest.mark.parametrize("notice_opens", [True, False], ids=["notice", "list-only"])
async def test_a_notice_posted_today_isnt_an_event_today(notice_opens: bool) -> None:
    # Dell's call is on Nov 24. Nov 10 is only when its notice was posted, both on the IR home
    # and on the notice itself, so a watch on Nov 10 would miss the release
    pages = DELL_SITE if notice_opens else {DELL_HOME: DELL_SITE[DELL_HOME]}

    result, _ = await rendered_date(Site(pages).render, [DELL_HOME], "DELL", DELL, NOW, [])

    assert result is not None
    target, evidence = result
    # At its usual 4:10 PM, before the call
    assert target.expected == datetime(2026, 11, 24, 16, 10, tzinfo=ET)
    assert target.period == "FY2027Q3"
    assert evidence.startswith(EVENT_DATE)


async def test_a_current_event_beats_a_neighboring_past_release() -> None:
    home = "https://investors.fedex.com/home/"
    event = "https://investors.fedex.com/upcoming-events/details/february-call"
    past = "https://investors.fedex.com/news-releases/details/last-earnings"
    pages = {
        home: (
            "<section><h2>Latest News</h2>"
            f"<p>Nov 10, 2026 <a href='{past}'>FedEx Reports Strong Third Quarter Results "
            "Read Press Release</a></p></section>"
            "<section><h2>Upcoming Events</h2>"
            f"<p><a href='{event}'>FedEx Q4 CY26 Earnings Call</a></p>"
            "<p>Tuesday, February 2, 2027 at 04:00 PM CT</p></section>"
        ),
    }
    company = ACME.model_copy(update={"fiscal_year_end": "1231", "release_time": "16:05"})

    result, _ = await rendered_date(Site(pages).render, [home], "FDX", company, NOW, [])

    assert result is not None
    target, evidence = result
    assert target.expected == datetime(2027, 2, 2, 16, 5, tzinfo=ET)
    assert target.period == "FY2026Q4"
    assert "5:00 PM ET" in evidence


async def test_undated_news_notice_does_not_borrow_its_posting_day() -> None:
    url = "https://issuer.example/news-releases/notice"
    home = "https://issuer.example/"
    pages = {
        home: "<section><h2>Latest News</h2>"
        f"<p>November 10, 2026 <a href='{url}'>Acme to Hold Q3 2026 Earnings Conference Call"
        " Read Press Release</a></p></section>",
        url: "<h1>Acme to Hold Q3 2026 Earnings Conference Call</h1>"
        "<p>November 10, 2026 4:05 pm ET</p>",
    }

    result, _ = await rendered_date(Site(pages).render, [home], "X", ACME, NOW, [])

    assert result is None


def test_news_link_text_does_not_make_results_an_event() -> None:
    assert not earnings_event("Company Reports Fourth Quarter Financial Results Read Press Release")
    assert earnings_event("Company Fourth Quarter Earnings Release")
    assert earnings_event("Company to Hold Fourth Quarter Earnings Call Read Press Release")
