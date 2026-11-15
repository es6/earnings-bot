from pathlib import Path

import pytest

from earnings_bot.issuer_history import listing_history
from earnings_bot.publisher_history import metadata, publication
from earnings_bot.recorder import ET
from earnings_bot.sources import Sighting
from tests.fakes import RoutedFetcher

FIXTURE = Path(__file__).parent / "fixtures/issuer-history/lcid-prn-q2.html"
TITLE = "Lucid Announces Operational Reset and Second Quarter 2026 Results"
LISTING = "https://ir.lucidmotors.com/press-releases"
ARTICLE = "https://ir.lucidmotors.com/news-releases/news-release-details/lucid-announces-q2"
WIRE = "https://www.prnewswire.com/news-releases/lucid-announces-operational-reset-and-second-quarter-2026-results-302842855.html"


def test_original_publisher_time_is_zoned_and_bound_to_its_real_report() -> None:
    at, _ = metadata(FIXTURE.read_text(encoding="utf-8"), TITLE)
    assert at and at.astimezone(ET).strftime("%Y-%m-%d %H:%M") == "2026-08-04 16:05"


@pytest.mark.parametrize("change", ["title", "quarter", "naive", "modified", "conflict"])
def test_wrong_report_or_unsupported_timestamp_cannot_supply_a_release_time(change: str) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    if change == "title":
        html = html.replace(TITLE, TITLE.replace("Lucid", "Other Issuer"))
    elif change == "quarter":
        html = html.replace("Second Quarter 2026", "First Quarter 2026")
    elif change == "naive":
        html = html.replace("2026-08-04T21:05:00+01:00", "2026-08-04T21:05:00")
    elif change == "modified":
        html = html.replace('name="date"', 'name="last-modified"')
    else:
        html += '<meta name="date" content="2026-08-04T22:05:00+01:00">'
    assert metadata(html, TITLE)[0] is None


async def test_date_only_listing_follows_one_linked_original_without_inventing_a_clock() -> None:
    issuer = f'<h1>{TITLE}</h1><a href="{WIRE}">Original release</a>'.encode()
    fetcher = RoutedFetcher(
        {
            WIRE: (200, FIXTURE.read_bytes()),
            ARTICLE: (200, issuer),
            LISTING: (200, f'<div>August 4, 2026 <a href="{ARTICLE}">{TITLE}</a></div>'.encode()),
        }
    )
    [item] = await listing_history(fetcher, LISTING)
    assert item.url == WIRE and item.published == "2026-08-04T21:05:00+01:00"
    assert item.details == {"issuer_article": ARTICLE}
    assert [url for _, url, _ in fetcher.requests] == [LISTING, ARTICLE, WIRE]


@pytest.mark.parametrize("url", [WIRE.replace(".com/", ".com.evil/"), WIRE + "?token=secret"])
async def test_history_never_follows_a_lookalike_or_credentialed_publisher_link(url: str) -> None:
    fetcher = RoutedFetcher(
        {ARTICLE: (200, f'<h1>{TITLE}</h1><a href="{url}">Original</a>'.encode())}
    )
    assert (
        await publication(fetcher, Sighting(ARTICLE, TITLE, ARTICLE), LISTING, chrome=False) is None
    )
    assert len(fetcher.requests) == 1


async def test_historical_article_reads_are_bounded_and_share_publisher_backoff() -> None:
    links = "".join(f'<a href="{ARTICLE}-{n}">{TITLE}</a>' for n in range(4))
    issuer = f'<h1>{TITLE}</h1><a href="{WIRE}">Original</a>'.encode()
    fetcher = RoutedFetcher(
        {
            WIRE: (503, b"wait"),
            ARTICLE: (200, issuer),
            LISTING: (200, links.encode()),
        },
    )
    assert await listing_history(fetcher, LISTING) == []
    urls = [url for _, url, _ in fetcher.requests]
    assert sum(url.startswith(ARTICLE) for url in urls) == 3
    assert urls.count(WIRE) == 1
