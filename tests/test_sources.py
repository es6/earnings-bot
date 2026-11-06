import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from earnings_bot.fetch import Response, host_gap
from earnings_bot.sources import (
    GLOBENEWSWIRE_EARNINGS,
    PR_NEWSWIRE_EARNINGS,
    Q4_REFRESH_SECONDS,
    BroadcomNewsroom,
    EdgarFilings,
    EdgarLatest,
    GuessedFile,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    looks_like_earnings,
    published_time,
)


def response(
    body: str | bytes, status: int = 200, headers: dict[str, str] | None = None
) -> Response:
    data = body.encode() if isinstance(body, str) else body
    return Response("https://example.com", status, headers or {}, data, 0.05)


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Uber Announces Results for Second Quarter 2026", True),
        ("FICO Announces Earnings of $3.42 per Share for Third Quarter Fiscal 2026", True),
        ("Palantir Reports Q2 2026 U.S. Comm Revenue Growth of 149% Y/Y", True),
        ("Apple reports fourth quarter results", True),
        ("Palantir Announces Date of Third Quarter 2026 Earnings Release and Webcast", False),
        ("Uber Announces Date of Third Quarter 2026 Results Conference Call", False),
        ("Amazon.com to Webcast Third Quarter 2026 Financial Results Conference Call", False),
        ("Key Food Stores Co-Op Joins Uber Eats", False),
        ("Cloudflare Expands EMEA Operations with New Headquarters in Lisbon", False),
        ("Atlassian Announces First Quarter Fiscal Year 2027 Results", True),
        ("Cloudflare Announces Third Quarter 2026 Financial Results", True),
    ],
)
def test_looks_like_earnings(headline: str, expected: bool) -> None:
    assert looks_like_earnings(headline) is expected


def test_host_gaps() -> None:
    assert host_gap("data.sec.gov") == 0.125
    assert host_gap("s23.q4cdn.com") == 0.2
    assert host_gap("feed.businesswire.com") == 1.0
    assert host_gap("investor.uber.com") == 1.0
    assert host_gap("notsec.gov") == 1.0


def test_q4_press_releases() -> None:
    source = Q4PressReleases("investor.uber.com")
    body = json.dumps(
        {
            "GetPressReleaseListResult": [
                {
                    "PressReleaseId": 1585,
                    "Headline": "Uber Announces Results for Second Quarter 2026",
                    "LinkToDetailPage": "/news-events/news/2026/x/default.aspx",
                    "PressReleaseDate": "08/05/2026 06:55:00",
                }
            ]
        }
    )

    [release] = source.parse(response(body))

    assert release.key == "1585"
    assert release.url == ("https://investor.uber.com/news-events/news/2026/x/default.aspx")
    assert release.published == "08/05/2026 06:55:00"
    assert release.earnings


def test_q4_requests_carry_a_buster_for_palantirs_cdn() -> None:
    source = Q4PressReleases("investor.uber.com")

    assert source.url() != source.url()
    assert "&_=" in source.url()


def page_size(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["pageSize"][0])


def test_q4_polls_take_turns_with_longer_lists_once_their_copies_expire() -> None:
    now = 1000.0
    source = Q4PressReleases("investor.uber.com", clock=lambda: now)
    asked: dict[int, list[float]] = {}
    for _ in range(120):  # two minutes of polls, one a second
        asked.setdefault(page_size(source.url()), []).append(now)
        now += 1.0

    longer = {size: times for size, times in asked.items() if size != 5}
    assert sorted(longer) == [6, 7, 8, 9, 10]
    for times in longer.values():
        # Asking again before the CDN's copy expires would only get the same copy
        assert all(b - a >= Q4_REFRESH_SECONDS for a, b in itertools.pairwise(times))
    turns = sorted(t for times in longer.values() for t in times)
    assert turns[0] == 1000.0
    # A fresh copy every 2 or 3 s, and the normal list gets the polls in between
    assert {b - a for a, b in itertools.pairwise(turns)} <= {2.0, 3.0}
    assert len(asked[5]) == 120 - len(turns)


def test_q4_bodies_come_from_the_normal_list() -> None:
    source = Q4PressReleases("investor.uber.com")

    url = source.url(with_bodies=True)

    assert page_size(url) == 5
    assert "bodyType=1" in url


def test_q4_reads_only_the_normal_lists_length() -> None:
    source = Q4PressReleases("investor.uber.com")
    longer = [
        {
            "PressReleaseId": n,
            "Headline": f"Release {n}",
            "LinkToDetailPage": f"/news/{n}/default.aspx",
            "PressReleaseDate": "08/05/2026 06:55:00",
        }
        for n in range(1700, 1690, -1)
    ]

    releases = source.parse(response(json.dumps({"GetPressReleaseListResult": longer})))

    assert [r.key for r in releases] == ["1700", "1699", "1698", "1697", "1696"]


def test_q4_financial_reports() -> None:
    source = Q4FinancialReports("investor.uber.com")
    pdf = "https://s23.q4cdn.com/407969754/files/doc_earnings/2026/q2/earnings-result/x.pdf"
    body = json.dumps(
        {
            "GetFinancialReportListResult": [
                {
                    "ReportTitle": "Second Quarter 2026",
                    "Documents": [{"DocumentTitle": "Press Release", "DocumentPath": pdf}],
                }
            ]
        }
    )

    [document] = source.parse(response(body))

    assert document.key == pdf
    assert document.title == "Second Quarter 2026: Press Release"
    assert document.earnings


def test_q4_financial_reports_webcast_link_isnt_the_release() -> None:
    source = Q4FinancialReports("investors.atlassian.com")
    letter = "https://s206.q4cdn.com/270053503/files/doc_financials/2027/q1/TEAM-Q1-2027-Shareholder-Letter.pdf"
    webcast = "https://events.q4inc.com/attendee/400181594"
    body = json.dumps(
        {
            "GetFinancialReportListResult": [
                {
                    "ReportTitle": "First Quarter 2027",
                    "Documents": [
                        {
                            "DocumentTitle": "Webcast",
                            "DocumentPath": webcast,
                            "DocumentType": "Online",
                        },
                        {
                            "DocumentTitle": "Shareholder letter",
                            "DocumentPath": letter,
                            "DocumentType": "File",
                        },
                    ],
                }
            ]
        }
    )

    found = {item.url: item.earnings for item in source.parse(response(body))}

    assert found == {webcast: False, letter: True}


EDGAR_LATEST = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>8-K - UBER TECHNOLOGIES, INC (0001543151) (Filer)</title>
<link rel="alternate" type="text/html"
  href="https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040/0001543151-26-000040-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-04
 &lt;b&gt;AccNo:&lt;/b&gt; 0001543151-26-000040
 &lt;br&gt;Item 2.02: Results of Operations and Financial Condition
 &lt;br&gt;Item 9.01: Financial Statements and Exhibits </summary>
<updated>2026-11-04T06:56:02-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0001543151-26-000040</id>
</entry>
<entry>
<title>8-K - UBER TECHNOLOGIES, INC (0001543151) (Filer)</title>
<link rel="alternate" type="text/html"
  href="https://www.sec.gov/Archives/edgar/data/1543151/000154315126000039/0001543151-26-000039-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-02
 &lt;b&gt;AccNo:&lt;/b&gt; 0001543151-26-000039
 &lt;br&gt;Item 8.01: Other Events &lt;br&gt;Item 9.01: Financial Statements and Exhibits </summary>
<updated>2026-11-02T08:00:12-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001543151-26-000039</id>
</entry>
</feed>"""


def test_edgar_latest_reads_one_companys_8ks_from_the_live_feed() -> None:
    source = EdgarLatest(1543151)

    earnings, other = source.parse(response(EDGAR_LATEST))

    assert "CIK=0001543151&type=8-K" in source.url()
    assert source.name == "edgar_latest"
    assert (earnings.key, earnings.title, earnings.earnings) == (
        "0001543151-26-000040",
        "8-K 2.02,9.01",
        True,
    )
    assert earnings.url.endswith("/000154315126000040/0001543151-26-000040-index.htm")
    assert earnings.published == "2026-11-04T06:56:02-05:00"
    assert (other.title, other.earnings) == ("8-K 8.01,9.01", False)
    with pytest.raises(ValueError, match="not valid XML"):
        source.parse(response("<html>"))


def test_edgar_flags_item_2_02_8ks() -> None:
    source = EdgarFilings(1543151)
    body = json.dumps(
        {
            "filings": {
                "recent": {
                    "accessionNumber": ["0001543151-26-000027", "0001543151-26-000020"],
                    "form": ["8-K", "4"],
                    "items": ["2.02,9.01", ""],
                    "acceptanceDateTime": ["2026-08-05T11:00:28.000Z", "2026-08-01T20:00:00.000Z"],
                    "primaryDocument": ["uber-20260805.htm", "xslF345X05/wk-form4.xml"],
                }
            }
        }
    )

    earnings, form4 = source.parse(response(body))

    assert source.url() == "https://data.sec.gov/submissions/CIK0001543151.json"
    assert earnings.title == "8-K 2.02,9.01"
    assert earnings.url == (
        "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000027/uber-20260805.htm"
    )
    assert earnings.published == "2026-08-05T11:00:28.000Z"
    assert earnings.earnings
    assert not form4.earnings


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Business Wire Earnings News</title>
  <lastBuildDate>Wed, 04 Nov 2026 21:15:20 UT</lastBuildDate>
  <item>
    <title>FICO Announces Earnings of $3.42 per Share for Fourth Quarter Fiscal 2026</title>
    <pubDate>Wed, 04 Nov 2026 21:15:00 UT</pubDate>
    <description>BOZEMAN, Mont.--(BUSINESS WIRE)--FICO (NYSE: FICO) today announced</description>
    <link>http://www.businesswire.com/news/home/1/en/</link>
    <guid isPermaLink="false">1en</guid>
  </item>
  <item>
    <title>Third Quarter 2026 Results</title>
    <pubDate>Wed, 04 Nov 2026 21:10:00 UT</pubDate>
    <description>NEW YORK--(BUSINESS WIRE)--Fair Isaac Corporation today reported</description>
    <link>http://www.businesswire.com/news/home/2/en/</link>
    <guid isPermaLink="false">2en</guid>
  </item>
  <item>
    <title>Beach Cities Commercial Bank Announces Third Quarter 2026 Financial Results</title>
    <pubDate>Wed, 04 Nov 2026 21:05:00 UT</pubDate>
    <description>IRVINE, Calif.--(BUSINESS WIRE)--Beach Cities Commercial Bank today</description>
    <link>http://www.businesswire.com/news/home/3/en/</link>
    <guid isPermaLink="false">3en</guid>
  </item>
</channel></rss>"""


def test_rss_keeps_items_naming_the_company() -> None:
    source = RssFeed("businesswire", "https://feed.example.com/rss?id=1", ["FICO", "Fair Isaac"])

    by_headline, by_dateline = source.parse(response(RSS))

    assert by_headline.key == "1en"
    assert by_headline.published == "Wed, 04 Nov 2026 21:15:00 UT"
    assert by_headline.details == {"feed_built": "Wed, 04 Nov 2026 21:15:20 UT"}
    assert by_headline.earnings
    assert by_dateline.key == "2en"


PR_NEWSWIRE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
  <item>
    <title>S&amp;P Global Reports Third Quarter Results</title>
    <link>https://www.prnewswire.com/news-releases/s-p-global-reports-302900001.html</link>
    <guid>https://www.prnewswire.com/news-releases/s-p-global-reports-302900001.html</guid>
    <pubDate>Tue, 27 Oct 2026 11:21:00 +0000</pubDate>
    <description><![CDATA[<p>NEW YORK /PRNewswire/ -- S&P Global]]></description>
    <dc:contributor><![CDATA[S&P Global]]></dc:contributor>
  </item>
  <item>
    <title>McDonald{rsquo}s reports third quarter 2026 results</title>
    <link>https://www.prnewswire.com/news-releases/mcdonalds-reports-302900002.html</link>
    <guid>https://www.prnewswire.com/news-releases/mcdonalds-reports-302900002.html</guid>
    <pubDate>Tue, 03 Nov 2026 12:00:00 +0000</pubDate>
    <description><![CDATA[<p>CHICAGO /PRNewswire/ -- McDonald{rsquo}s]]></description>
    <dc:contributor><![CDATA[McDonald{rsquo}s Corporation]]></dc:contributor>
  </item>
  <item>
    <title>Third-quarter 2026 financial results</title>
    <link>https://www.prnewswire.com/news-releases/third-quarter-302900003.html</link>
    <guid>https://www.prnewswire.com/news-releases/third-quarter-302900003.html</guid>
    <pubDate>Thu, 29 Oct 2026 10:45:00 +0000</pubDate>
    <description><![CDATA[<p>INDIANAPOLIS /PRNewswire/ -- The company]]></description>
    <dc:contributor><![CDATA[Eli Lilly and Company]]></dc:contributor>
  </item>
  <item>
    <title>The Home Depot anuncia resultados del tercer trimestre del año fiscal 2026</title>
    <link>https://www.prnewswire.com/news-releases/the-home-depot-anuncia-302900004.html</link>
    <guid>https://www.prnewswire.com/news-releases/the-home-depot-anuncia-302900004.html</guid>
    <pubDate>Tue, 17 Nov 2026 11:00:00 +0000</pubDate>
    <description><![CDATA[<p>ATLANTA /PRNewswire/ -- The Home Depot]]></description>
    <dc:contributor><![CDATA[The Home Depot]]></dc:contributor>
  </item>
  <item>
    <title>Kansas City Life Announces Third Quarter 2026 Results</title>
    <link>https://www.prnewswire.com/news-releases/kansas-city-life-302900005.html</link>
    <guid>https://www.prnewswire.com/news-releases/kansas-city-life-302900005.html</guid>
    <pubDate>Fri, 30 Oct 2026 16:41:00 +0000</pubDate>
    <description><![CDATA[<p>KANSAS CITY /PRNewswire/ -- Kansas City Life]]></description>
    <dc:contributor><![CDATA[Kansas City Life Insurance Company]]></dc:contributor>
  </item>
</channel></rss>""".replace("{rsquo}", "\N{RIGHT SINGLE QUOTATION MARK}")


def test_pr_newswire_items_match_on_headline_or_contributor() -> None:
    names = ["S&P Global", "McDonald's", "Lilly", "The Home Depot"]
    source = RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, names)

    sightings = source.parse(response(PR_NEWSWIRE))

    # S&P Global despite the &amp;, McDonald's despite the curly apostrophe, and Lilly only
    # through dc:contributor. Kansas City Life names none of them.
    assert [s.url.rsplit("-", 1)[-1] for s in sightings] == [
        "302900001.html",
        "302900002.html",
        "302900003.html",
        "302900004.html",
    ]
    assert sightings[0].title == "S&P Global Reports Third Quarter Results"
    # The Spanish edition matches the name but doesn't read as an earnings headline
    assert [s.earnings for s in sightings] == [True, True, True, False]


def test_rss_without_companies_keeps_everything() -> None:
    assert len(RssFeed("rss", "https://ir.example.com/rss").parse(response(RSS))) == 3


def test_rss_cache_buster_keeps_the_feed_id() -> None:
    source = RssFeed("bw", "https://feed.example.com/rss?id=1", bust_cache=True)

    assert source.url().startswith("https://feed.example.com/rss?id=1&_=")


def test_rss_rejects_html() -> None:
    with pytest.raises(ValueError, match="not valid XML"):
        RssFeed("rss", "https://ir.example.com/rss").parse(response("<html><body>"))


def test_guessed_file() -> None:
    url = "https://s23.q4cdn.com/1/files/Uber-Q3-26-Earnings-Press-Release.pdf"
    source = GuessedFile(url)
    up = response(b"", headers={"last-modified": "Wed, 04 Nov 2026 11:55:20 GMT"})

    assert source.name == "guess:Uber-Q3-26-Earnings-Press-Release.pdf"
    assert source.url().startswith(url + "?_=")
    assert source.parse(response(b"", status=404)) == []
    [file] = source.parse(up)
    assert file.key == url
    assert file.published == "Wed, 04 Nov 2026 11:55:20 GMT"


def test_guessed_file_answered_with_a_web_page_isnt_there_yet() -> None:
    # UnitedHealth answers 200 with a "Page Not Found" page for files that don't exist yet
    pdf = GuessedFile("https://www.unitedhealthgroup.com/content/dam/uhg/pdf/q4-results.pdf")
    soft_404 = response(b"<html>", headers={"content-type": "text/html;charset=utf-8"})
    real = response(b"%PDF", headers={"content-type": "application/pdf"})

    assert pdf.parse(soft_404) == []
    assert len(pdf.parse(real)) == 1
    # A guessed page is expected to be a web page
    page = GuessedFile(
        "https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/"
    )
    assert len(page.parse(soft_404)) == 1


def test_guessed_page_is_named_after_its_last_folder() -> None:
    source = GuessedFile(
        "https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/"
    )

    assert source.name == "guess:apple-reports-fourth-quarter-results"


ATOM = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Apple Newsroom</title><updated>2026-11-02T21:30:01.000Z</updated>
<entry><updated>2026-11-02T21:30:00.000Z</updated>
<title><![CDATA[Apple reports fourth quarter results
]]></title><link href="https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/"/>
<id>https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/</id>
<content>Apple today announced financial results</content>
<link href="https://www.apple.com/newsroom/images/tile.jpg" rel="enclosure" type="image/jpeg"/>
</entry>
</feed>"""


def test_atom_feed() -> None:
    [entry] = RssFeed("rss:www.apple.com", "https://www.apple.com/newsroom/rss-feed.rss").parse(
        response(ATOM)
    )

    assert entry.title == "Apple reports fourth quarter results"
    assert (
        entry.url == "https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/"
    )
    assert entry.published == "2026-11-02T21:30:00.000Z"
    assert entry.details == {"feed_built": "2026-11-02T21:30:01.000Z"}
    assert entry.earnings


CRWD_PAGE = (
    "https://markets.financialcontent.com/stocks/article/bizwire-2026-12-2-crowdstrike-reports-"
    "third-quarter-fiscal-year-2027-financial-results"
)
HTML = {"content-type": "text/html; charset=utf-8"}


def test_guessed_page_isnt_there_until_it_answers_with_a_page() -> None:
    page = GuessedPage(CRWD_PAGE)
    release = b"<html><head><title>\n CrowdStrike Reports Third Quarter </title></head>...</html>"

    assert page.name == "page:markets.financialcontent.com"
    assert page.method == "GET"
    assert page.stop_when_found
    assert page.parse(response(b"<html>Not found</html>", status=404, headers=HTML)) == []
    # Something other than a page, such as a JSON error, isn't the release either
    assert page.parse(response(b"{}", headers={"content-type": "application/json"})) == []
    [found] = page.parse(response(release, headers=HTML))
    assert found.key == found.url == CRWD_PAGE
    assert found.title == "CrowdStrike Reports Third Quarter"
    assert found.earnings
    assert found.body == release  # the poll already has the release, so nobody fetches it again
    assert found.details == {"size": str(len(release))}


def test_guessed_pdf_page_must_be_a_pdf() -> None:
    pdf = GuessedPage("https://example.com/files/Q3-Earnings-Release.pdf")

    assert pdf.parse(response(b"<html>Page Not Found</html>", headers=HTML)) == []
    assert pdf.parse(response(b"", status=404)) == []
    [found] = pdf.parse(response(b"%PDF-1.7 ...", headers={"content-type": "application/pdf"}))
    assert found.body == b"%PDF-1.7 ..."


BROADCOM = [
    {
        "content_id": "64786",
        "title": "Bank of America, Blackstone, Citi and Morgan Stanley Announce Landmark Financing",
        "publish_date": "2026-10-26T20:15:00Z",
        "url": "/company/news/financial-releases/64786",
    },
    {
        "content_id": "64671",
        "title": " Broadcom Inc. Announces Third Quarter Fiscal Year 2026 Financial Results ",
        "publish_date": "2026-09-02T20:15:00Z",
        "url": "/company/news/financial-releases/64671",
    },
    {
        "content_id": "64621",
        "title": "Broadcom Inc. to Announce Third Quarter Fiscal Year 2026 Financial Results",
        "publish_date": "2026-08-03T12:00:00Z",
        "url": "/company/news/financial-releases/64621",
    },
]


def test_broadcom_newsroom() -> None:
    source = BroadcomNewsroom()

    sightings = source.parse(response(json.dumps(BROADCOM)))

    assert [s.earnings for s in sightings] == [False, True, False]
    results = sightings[1]
    assert results.key == "64671"
    assert results.title == BROADCOM[1]["title"].strip()
    assert results.url == "https://www.broadcom.com/company/news/financial-releases/64671"
    assert results.details == {"path": "/company/news/financial-releases/64671"}
    assert published_time(results.published) == datetime(2026, 9, 2, 20, 15, tzinfo=UTC)
    # Cloudflare kept a copy 445 s old until it was busted
    assert "&_=" in source.url()
    with pytest.raises(ValueError, match="not a list"):
        source.parse(response(json.dumps({"error": "busy"})))


@pytest.mark.parametrize(
    ("stamp", "moment"),
    [
        ("2026-09-02T20:15:00Z", datetime(2026, 9, 2, 20, 15, tzinfo=UTC)),  # Broadcom
        ("2026-11-05T21:09:54.000Z", datetime(2026, 11, 5, 21, 9, 54, tzinfo=UTC)),  # EDGAR
        (
            "2026-11-05T16:09:54-05:00",  # EDGAR's latest-filings feed
            datetime(2026, 11, 5, 16, 9, 54, tzinfo=timezone(timedelta(hours=-5))),
        ),
        # Business Wire, then GlobeNewswire
        ("Wed, 04 Nov 2026 21:15:00 UT", datetime(2026, 11, 4, 21, 15, tzinfo=UTC)),
        ("Fri, 06 Nov 2026 22:08 GMT", datetime(2026, 11, 6, 22, 8, tzinfo=UTC)),
        ("08/05/2026 06:55:00", None),  # Q4's site-local time names no zone
        ("2026-11-05T16:09:54", None),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_published_time(stamp: str | None, moment: datetime | None) -> None:
    assert published_time(stamp) == moment


def test_one_feed_watched_for_several_companies_is_polled_once() -> None:
    nvidia = RssFeed("globenewswire", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"])
    amd = RssFeed("globenewswire", GLOBENEWSWIRE_EARNINGS, ["AMD"])
    busted = RssFeed("globenewswire_busted", GLOBENEWSWIRE_EARNINGS, ["AMD"], bust_cache=True)

    assert nvidia.share_key == amd.share_key
    assert busted.share_key != amd.share_key
    assert BroadcomNewsroom().share_key == BroadcomNewsroom().share_key
    assert GuessedPage(CRWD_PAGE).share_key is None
    assert EdgarFilings(1).share_key is None
