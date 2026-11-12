import json

import pytest

from earnings_bot.sources import (
    BroadcomNewsroom,
    GuessedFile,
    GuessedPage,
    Q4PressReleases,
    RssFeed,
    Sighting,
)
from earnings_bot.text import html_to_text, pdf_text, pick_exhibit, release_text
from tests.fakes import RoutedFetcher, simple_pdf

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


async def test_globenewswire_article_is_read_from_the_feed_host() -> None:
    url = "https://www.globenewswire.com/news-release/2026/11/18/3400000/0/en/nvidia-results.html"
    article = url.replace("www.", "rss.", 1)
    feed = RssFeed("globenewswire", "https://rss.globenewswire.com/RssFeed/subjectcode/13")
    fetcher = RoutedFetcher({article: (200, b"<p>Revenue and earnings per share. </p>" * 100)})

    assert await release_text(fetcher, feed, Sighting(url, "Results", url, earnings=True))
    assert fetcher.requests == [("GET", article, None)]


def test_html_to_text_keeps_paragraphs_and_table_columns() -> None:
    html = """<html><head><title>x</title><style>.bw { color: red }</style></head><body>
    <p>SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced
       results &amp; more.</p>
    <script>track()</script>
    <table><tr><td><p>Revenue</p></td><td></td><td><p>$ 12,651</p></td><td>$ 14,211</td></tr>
    <tr><th>Trips</th><td></td><td>3,350</td><td>3,900</td></tr></table>
    </body></html>"""

    assert html_to_text(html).splitlines() == [
        "SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced results & more.",
        "Revenue | $ 12,651 | $ 14,211",
        "Trips | 3,350 | 3,900",
    ]


def costco_row(*texts: str) -> str:
    # Costco's 8-K puts every cell in colspan=3 on a 24-column grid, with an empty cell
    # between each pair of columns
    cells = (
        f'<td colspan="3"><font>{t}</font></td>' if t else '<td colspan="3"></td>' for t in texts
    )
    return "<tr>" + "".join(cells) + "</tr>"


def test_blank_header_cells_keep_labels_over_their_columns() -> None:
    # Costco's Q4 FY2026 8-K (EX-99.1, 2026-09-24). With the blank header cells collapsed,
    # "Adjusted*" moved over the reported comps, and summaries called the 9.4% adjusted.
    rows = [
        "<tr>" + "<td></td>" * 24 + "</tr>",  # sets the column widths
        costco_row("", "16 Weeks", "", "16 Weeks", "", "52 Weeks", "", "52 Weeks"),
        costco_row("", "", "", "Adjusted*", "", "", "", "Adjusted*"),
        costco_row("U.S.", "10.7%", "", "7.2%", "", "8.2%", "", "6.6%"),
        costco_row("", "", "", "", "", "", "", ""),
        costco_row("Total Company", "9.4%", "", "6.7%", "", "8.4%", "", "6.6%"),
    ]
    html = (
        "<p>Comparable sales for the fourth quarter and fiscal year were as follows:</p>"
        f"<table>{''.join(rows)}</table>"
        "<p>*Excluding the impacts from changes in gasoline prices and foreign exchange.</p>"
    )

    lines = html_to_text(html).splitlines()

    assert lines == [
        "Comparable sales for the fourth quarter and fiscal year were as follows:",
        "| 16 Weeks | 16 Weeks | 52 Weeks | 52 Weeks",
        "| | Adjusted* | | Adjusted*",
        "U.S. | 10.7% | 7.2% | 8.2% | 6.6%",
        "Total Company | 9.4% | 6.7% | 8.4% | 6.6%",
        "*Excluding the impacts from changes in gasoline prices and foreign exchange.",
    ]
    header, total = ([cell.strip() for cell in line.split("|")] for line in lines[2:5:2])
    adjusted = [total[i] for i, label in enumerate(header) if label == "Adjusted*"]
    assert adjusted == ["6.7%", "6.6%"]


def test_edgar_spacer_and_dollar_columns_read_as_before() -> None:
    # Workiva-style EDGAR exhibit: an empty spacer column after the label and between the
    # periods, "$" only on the first and total rows, and ")" and "%" in columns of their own.
    # Each line is what html_to_text gave before it lined up columns.
    html = """<table>
    <tr><td colspan="2"></td><td colspan="7">Three Months Ended September 30,</td></tr>
    <tr><td colspan="2"></td><td colspan="3">2026</td><td></td><td colspan="3">2025</td></tr>
    <tr><td>Net sales</td><td></td><td>$</td><td>93,856</td><td></td><td></td>
        <td>$</td><td>84,433</td><td></td></tr>
    <tr><td>Membership fees</td><td></td><td></td><td>1,724</td><td></td><td></td>
        <td></td><td>1,512</td><td></td></tr>
    <tr><td>Other income (expense), net</td><td></td><td></td><td>(42</td><td>)</td><td></td>
        <td></td><td>118</td><td></td></tr>
    <tr><td>Operating margin</td><td></td><td></td><td>3.9</td><td>%</td><td></td>
        <td></td><td>3.7</td><td>%</td></tr>
    <tr><td>Total revenue</td><td></td><td>$</td><td>95,580</td><td></td><td></td>
        <td>$</td><td>85,945</td><td></td></tr>
    </table>"""

    assert html_to_text(html).splitlines() == [
        "| Three Months Ended September 30,",
        "| 2026 | 2025",
        "Net sales | $ | 93,856 | $ | 84,433",
        "Membership fees | 1,724 | 1,512",
        "Other income (expense), net | (42 | ) | 118",
        "Operating margin | 3.9 | % | 3.7 | %",
        "Total revenue | $ | 95,580 | $ | 85,945",
    ]


def test_a_heading_that_spans_two_rows_keeps_the_row_below_in_place() -> None:
    # Header cells with rowspan, as in Goldman Sachs's and Bank of America's exhibits
    html = """<table>
    <tr><td rowspan="2"></td><td colspan="2">Third Quarter</td><td rowspan="2">% Change</td></tr>
    <tr><td>2026</td><td>2025</td></tr>
    <tr><td>Revenue</td><td>15,180</td><td>12,698</td><td>20%</td></tr>
    <tr><td>Return on equity</td><td>15.1%</td><td></td><td>n/m</td></tr>
    </table>"""

    assert html_to_text(html).splitlines() == [
        "| Third Quarter | % Change",
        "| 2026 | 2025",
        "Revenue | 15,180 | 12,698 | 20%",
        "Return on equity | 15.1% | | n/m",
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


@pytest.mark.parametrize(
    "one_item",
    [
        q4_list((1701, "<p>Other news</p>")),
        q4_list((1700, "")),
        q4_list((1700, "<p>Coming soon</p>")),
        b"<html>CDN error</html>",
        b'{"GetPressReleaseListResult": null}',
    ],
    ids=["stale", "empty", "teaser", "malformed", "wrong-shape"],
)
async def test_q4_text_falls_back_when_the_one_item_response_has_no_release(
    one_item: bytes,
) -> None:
    fetcher = RoutedFetcher(
        {
            "pageSize=1&": (200, one_item),
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


def test_a_release_pdf_is_read_as_text_and_a_broken_one_as_nothing() -> None:
    lines = ["Brookfield Corporation Reports Third Quarter 2026 Results"]
    lines += ["Revenues of $20,691 million and net income of $1,914 million over the prior-"]
    lines += ["year quarter."]

    text = pdf_text(simple_pdf(lines))

    assert "Brookfield Corporation Reports Third Quarter 2026 Results" in text
    # A line wrapped at a hyphen reads as the HTML release does
    assert "over the prior-year quarter." in text
    assert pdf_text(b"%PDF-1.4 not really") == ""
    assert pdf_text(b"<html>Missing</html>") == ""
