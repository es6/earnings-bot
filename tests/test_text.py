import json

from earnings_bot.sources import (
    BroadcomNewsroom,
    GuessedFile,
    GuessedPage,
    Q4PressReleases,
    RssFeed,
    Sighting,
)
from earnings_bot.text import html_to_text, pick_exhibit, release_text
from tests.fakes import RoutedFetcher

RELEASE = "<p>" + "Net revenue was $16.0 billion, up 22% from a year ago. " * 50 + "</p>"
BROADCOM_PATH = "/company/news/financial-releases/64900"


def broadcom_results() -> Sighting:
    url = f"https://www.broadcom.com{BROADCOM_PATH}"
    title = "Broadcom Inc. Announces Fourth Quarter and Fiscal Year 2026 Financial Results"
    return Sighting("64900", title, url, earnings=True, details={"path": BROADCOM_PATH})


async def test_broadcom_text_comes_from_its_metadata_api() -> None:
    fetcher = RoutedFetcher({"getmetadata": (200, json.dumps({"body": RELEASE}).encode())})

    text = await release_text(fetcher, BroadcomNewsroom(), broadcom_results())

    assert text is not None
    assert text.startswith("Net revenue was $16.0 billion")
    # Fetched ahead of the host's polls, and past Cloudflare's copy
    [url] = fetcher.urgent
    assert f"url={BROADCOM_PATH}&locale=en-us&_=" in url


async def test_a_broadcom_release_that_isnt_up_yet_has_no_text() -> None:
    # An ID Broadcom doesn't know answers 200 with a stub that has no body (seen 2026-11-06)
    stub = {
        "content_id": "64900",
        "content_type": "press_release",
        "template": "PressReleaseDetail",
        "page_title": "Financial News",
        "date_time": "11/06/2026, 22:35:20",
    }
    for answer in (stub, {**stub, "body": ""}, [stub]):
        fetcher = RoutedFetcher({"getmetadata": (200, json.dumps(answer).encode())})
        assert await release_text(fetcher, BroadcomNewsroom(), broadcom_results()) is None


async def test_a_page_the_poll_downloaded_isnt_fetched_again() -> None:
    url = "https://mcdonalds.mediaroom.com/2026-11-04-McDONALDS-REPORTS-THIRD-QUARTER-2026-RESULTS"
    page = Sighting(url, "McDonald's reports", url, earnings=True, body=RELEASE.encode())
    pdf_url = "https://example.com/Q3-Earnings-Release.pdf"
    pdf = Sighting(pdf_url, "Release", pdf_url, earnings=True, body=b"%PDF-1.7")
    fetcher = RoutedFetcher({})

    text = await release_text(fetcher, GuessedPage(url), page)

    assert text is not None and text.startswith("Net revenue")
    assert await release_text(fetcher, GuessedPage(pdf_url), pdf) is None
    assert fetcher.requests == []


async def test_a_missing_page_or_file_has_no_text() -> None:
    url = "https://ir.example.com/news/q3-results"
    fetcher = RoutedFetcher({})  # every URL answers 404

    item = Sighting(url, "Results", url, earnings=True)
    assert await release_text(fetcher, RssFeed("rss:ir.example.com", "https://x/rss"), item) is None
    pdf_url = "https://example.com/Q3-Earnings-Release.pdf"
    pdf = Sighting(pdf_url, "Release", pdf_url, earnings=True)
    assert await release_text(fetcher, GuessedFile(pdf_url), pdf) is None
    assert fetcher.urgent == [url]  # PDFs aren't read, since the release's page comes too


async def test_globenewswire_articles_arent_fetched() -> None:
    # Only its RSS host lets us in; www.globenewswire.com answers 403
    url = "https://www.globenewswire.com/news-release/2026/11/18/3400000/0/en/nvidia-results.html"
    feed = RssFeed("globenewswire", "https://rss.globenewswire.com/RssFeed/subjectcode/13")
    fetcher = RoutedFetcher({})

    assert await release_text(fetcher, feed, Sighting(url, "Results", url, earnings=True)) is None
    assert fetcher.requests == []


def test_html_to_text_keeps_paragraphs_and_table_columns() -> None:
    html = """<html><head><title>x</title><style>.bw { color: red }</style></head><body>
    <p>SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced
       results &amp; more.</p>
    <script>track()</script>
    <table><tr><td><p>Revenue</p></td><td></td><td><p>$ 12,651</p></td><td>$ 14,211</td></tr>
    <tr><th>Trips</th><td>3,350</td><td>3,900</td></tr></table>
    </body></html>"""

    assert html_to_text(html).splitlines() == [
        "SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced results & more.",
        "Revenue | $ 12,651 | $ 14,211",
        "Trips | 3,350 | 3,900",
    ]


def test_pick_exhibit_prefers_the_press_release() -> None:
    files = [
        {"name": "0001543151-26-000027-index.html", "size": ""},
        {"name": "R1.htm", "size": "38314"},
        {"name": "uber-20260805.htm", "size": "24475"},
        {"name": "uberq226earningspressrelea.htm", "size": "447114"},
        # Bigger, but not named like a release, so the release still wins
        {"name": "uber-investor-presentation.htm", "size": "912000"},
        {"name": "MetaLinks.json", "size": "19403"},
    ]

    assert pick_exhibit(files, "uber-20260805.htm") == "uberq226earningspressrelea.htm"


def test_pick_exhibit_falls_back_to_the_biggest_document() -> None:
    files = [
        {"name": "fico-20260805.htm", "size": "20000"},
        {"name": "a2026q3exhibit.htm", "size": "350000"},
        {"name": "small.htm", "size": "900"},
    ]

    assert pick_exhibit(files, "fico-20260805.htm") == "a2026q3exhibit.htm"


def q4_list(*releases: tuple[int, str]) -> bytes:
    return json.dumps(
        {
            "GetPressReleaseListResult": [
                {"PressReleaseId": n, "Headline": f"Release {n}", "Body": body}
                for n, body in releases
            ]
        }
    ).encode()


async def test_q4_text_comes_from_the_newest_release_alone() -> None:
    fetcher = RoutedFetcher({"pageSize=1&": (200, q4_list((1700, RELEASE)))})
    sighting = Sighting("1700", "Uber Announces Results", "https://investor.uber.com/news/1700")

    text = await release_text(fetcher, Q4PressReleases("investor.uber.com"), sighting)

    assert text == html_to_text(RELEASE)
    assert [url for _, url, _ in fetcher.requests] == [fetcher.urgent[0]]
    assert "bodyType=1" in fetcher.urgent[0]


async def test_q4_text_falls_back_to_the_normal_list_when_the_newest_isnt_it() -> None:
    # Another release went up at the same moment, or the CDN's copy of the one-item list is a
    # few seconds old
    fetcher = RoutedFetcher(
        {
            "pageSize=1&": (200, q4_list((1701, "<p>Other news</p>"))),
            "pageSize=5&": (200, q4_list((1701, "<p>Other news</p>"), (1700, RELEASE))),
        }
    )
    sighting = Sighting("1700", "Uber Announces Results", "https://investor.uber.com/news/1700")

    text = await release_text(fetcher, Q4PressReleases("investor.uber.com"), sighting)

    assert text == html_to_text(RELEASE)
    assert len(fetcher.urgent) == 2


async def test_a_page_too_short_to_be_a_release_has_no_text() -> None:
    # An error page or a teaser that answered 200
    url = "https://news.airbnb.com/airbnb-q3-2026-financial-results"
    teaser = Sighting(url, "Airbnb", url, earnings=True, body=b"<p>Coming soon</p>")

    assert await release_text(RoutedFetcher({}), GuessedPage(url), teaser) is None
