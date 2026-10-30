import json

import pytest

from earnings_bot.fetch import Response, host_gap
from earnings_bot.sources import (
    EdgarFilings,
    GuessedFile,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    looks_like_earnings,
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
        ("Uber Announces Date of Third Quarter 2026 Results Conference Call", False),
        ("Amazon.com to Webcast Third Quarter 2026 Financial Results Conference Call", False),
        ("Key Food Stores Co-Op Joins Uber Eats", False),
    ],
)
def test_looks_like_earnings(headline: str, expected: bool) -> None:
    assert looks_like_earnings(headline) is expected


def test_host_gaps() -> None:
    assert host_gap("data.sec.gov") == 0.125
    assert host_gap("s23.q4cdn.com") == 0.2
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


def test_q4_requests_bust_the_cache() -> None:
    source = Q4PressReleases("investor.uber.com")

    assert source.url() != source.url()
    assert "&_=" in source.url()


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
    assert source.parse(response(b"", status=404)) == []
    [file] = source.parse(up)
    assert file.key == url
    assert file.published == "Wed, 04 Nov 2026 11:55:20 GMT"
