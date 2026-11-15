from pathlib import Path

import pytest

from earnings_bot.source_document import SourceDocument
from earnings_bot.sources import RssFeed, Sighting
from earnings_bot.text import release_document
from earnings_bot.wire_mirror import HOST, candidate, candidates
from tests.fakes import RoutedFetcher

URL = "https://www.businesswire.com/news/home/20261112208769/en/"
TITLE = "Nebius reports third quarter 2026 financial results"
ITEM = Sighting("20261112208769", TITLE, URL, earnings=True)
PAGE = Path(__file__).parent / "fixtures/mirrors/nbis-financialcontent.html"
FEED = RssFeed("businesswire", "https://feed.businesswire.com/rss/home/")


async def test_known_wire_release_recovers_tables_with_durable_retrieval_provenance() -> None:
    url = candidate(ITEM)
    assert url == (
        "https://markets.financialcontent.com/stocks/article/"
        "bizwire-2026-11-12-nebius-reports-third-quarter-2026-financial-results?Language=english"
    )
    fetcher = RoutedFetcher({url: (200, PAGE.read_bytes())})
    document = await release_document(fetcher, FEED, ITEM)
    assert document and "Nebius" in document.text and len(document.text) > 10000
    assert document.tables
    assert document.canonical_url == URL and document.retrieval_url == url
    assert SourceDocument.from_json(document.to_json()) == document
    assert fetcher.requests == [("GET", url, None)]


@pytest.mark.parametrize("change", ["id", "period", "issuer", "link", "short", "article"])
async def test_mirror_rejects_wrong_release_or_teaser(change: str) -> None:
    html = PAGE.read_text(encoding="utf-8")
    if change == "id":
        html = html.replace("20261112208769/en/", "20261112208768/en/")
    elif change == "period":
        html = html.replace(TITLE, TITLE.replace("third", "second"))
    elif change == "issuer":
        html = html.replace(TITLE, TITLE.replace("Nebius", "Other"))
    elif change == "link":
        html = html.replace("www.businesswire.com", "www.businesswire.com.example.org")
    elif change == "short":
        html = (
            f'<article class="newsrpm-article"><h1>{TITLE}</h1><a href="{URL}">source</a></article>'
        )
    else:
        html = html.replace("newsrpm-article", "unrelated-story")
    url = candidate(ITEM)
    assert url is not None
    fetcher = RoutedFetcher({url: (200, html.encode())})
    assert await release_document(fetcher, FEED, ITEM) is None


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("/en/", "/fr/"),
        URL.replace(".com/", ".com.evil/"),
        "https://www.businesswire.com/news/home/99999999208769/en/",
    ],
)
def test_no_candidate_for_unrecognized_wire_url(url: str) -> None:
    assert candidate(Sighting("x", TITLE, url, earnings=True)) is None


def test_mirror_date_comes_from_publication_and_uses_unpadded_month_and_day() -> None:
    item = Sighting(
        "20260817790536",
        "Klarna Reports Second Quarter 2026 Results",
        "https://www.businesswire.com/news/home/20260817790536/en/",
        "2026-08-18T11:34:00Z",
        earnings=True,
    )
    assert "bizwire-2026-8-18-klarna" in (candidate(item) or "")


async def test_full_nested_story_survives_a_missing_date_spelling() -> None:
    item = Sighting(
        "20260826812507",
        "CrowdStrike Reports Second Quarter Fiscal Year 2027 Financial Results",
        "https://www.businesswire.com/news/home/20260826812507/en/",
        earnings=True,
    )
    first, second = candidates(item)
    page = (PAGE.parent / "crwd-financialcontent.html").read_bytes()
    fetcher = RoutedFetcher({first: (404, b"gone"), second: (200, page)})
    document = await release_document(fetcher, FEED, item)
    assert document and len(document.text) > 40000 and document.tables
    assert document.retrieval_url == second
    assert len(fetcher.requests) == 2


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_mirror_backoff_is_shared_by_other_releases_and_spelling_fallbacks(
    status: int,
) -> None:
    first = candidate(ITEM)
    assert first
    fetcher = RoutedFetcher({first: (status, b"wait")}, headers={"Retry-After": "120"})
    assert await release_document(fetcher, FEED, ITEM) is None
    assert fetcher.route_pause(HOST).left() > 0
    assert await release_document(fetcher, FEED, ITEM) is None
    assert len(fetcher.requests) == 1
