"""A client-rendered newsroom can still supply a confirmed date and live PDF."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pytest

from earnings_bot import date_discovery
from earnings_bot.browser import BrowserJSON, DesktopChrome
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.issuer_news import IssuerNews
from earnings_bot.known import KnownCompany
from earnings_bot.recorder import ET, poll
from tests.fakes import RoutedChrome, RoutedFetcher

HOME = "https://www.sea.com/"
LISTING = HOME + "investor/newsroom"
API = HOME + "api/invest/news?page=1&size=12"
PDF = "https://cdn.sea.com/investor/prior.pdf"
TODAY_PDF = "https://cdn.sea.com/investor/current.pdf"
NOW = datetime(2026, 11, 10, 3, tzinfo=ET)
NOTICE = {
    "title": "Sea Limited to Report Third Quarter 2026 Results",
    "description": (
        "Sea Limited plans to announce its third quarter 2026 results before the U.S. market "
        "opens on November 10, 2026, U.S. Eastern Time."
    ),
    "publish_date": "2026/10/27",
    "document_url": "https://cdn.sea.com/investor/notice.pdf",
}
PRIOR = {
    "title": "Sea Limited Reports Second Quarter 2026 Results",
    "publish_date": "2026/08/11",
    "document_url": PDF,
}
RESULTS = {
    "title": "Sea Limited Reports Third Quarter 2026 Results",
    "publish_date": "2026/11/10",
    "document_url": TODAY_PDF,
}


class NewsChrome(RoutedChrome):
    async def inspect_json(self, url: str) -> list[BrowserJSON]:
        return [BrowserJSON("GET", "/api/invest/news", {"news": [NOTICE, PRIOR]}, API)]


class NewsFetcher(RoutedFetcher):
    @asynccontextmanager
    async def research_browser(self) -> AsyncIterator[DesktopChrome]:
        yield cast(DesktopChrome, NewsChrome(self))


async def test_track_reads_the_rendered_newsrooms_api_and_prepares_live_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = NewsFetcher(
        {
            API: (200, json.dumps({"news": [NOTICE, PRIOR]}).encode()),
            PDF: (200, b"%PDF-1.7"),
            HOME: (200, b'<a href="/investor/newsroom">Newsroom</a><a href="/about">About</a>'),
        }
    )
    company = KnownCompany(
        name="Sea Ltd",
        names=["Sea Limited"],
        cik=1703399,
        platform="unknown",
        reachable=False,
        wire="unknown",
        sec_form="8-K",
        release_time="16:05",
        fastest="unknown",
        evidence="SEC data",
        checked=NOW.date(),
    )

    async def last_results(*_: object) -> str:
        return "Sea Limited. See https://www.sea.com/investor/home."

    monkeypatch.setattr("earnings_bot.investigate._last_release", last_results)
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    with DeliveryStore(tmp_path / "calendar.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("SE", 1, NOW, company)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
        await dates.run_once(row)
        [saved] = calendar.entries()

    assert saved.company.ir_news_api == API
    assert saved.target and saved.target.ir and saved.target.ir.news_api == API
    assert saved.target.expected == datetime(2026, 11, 10, 7, tzinfo=ET)
    assert saved.evidence and NOTICE["document_url"] in saved.evidence
    assert "issuer-news" in {source.name for source in saved.target.sources()}


async def test_the_news_poll_requires_the_right_date_title_origin_and_pdf() -> None:
    other_origin = {**RESULTS, "document_url": "https://cdn.sea.com.example.org/report.pdf"}
    stale = {**RESULTS, "publish_date": "2026/10/10", "document_url": PDF}
    notice = {**NOTICE, "publish_date": "2026/11/10"}
    feed = {"news": [notice, other_origin, stale, RESULTS]}
    fetcher = RoutedFetcher(
        {API: (200, json.dumps(feed).encode()), TODAY_PDF: (200, b"<html>stub")}
    )
    source = IssuerNews(API, r".*Third Quarter 2026 Results", date(2026, 11, 10))

    assert (await poll(source, fetcher))[1] == []
    fetcher.routes[TODAY_PDF] = (200, b"%PDF-1.7")
    _, items = await poll(source, fetcher)

    assert [(item.url, item.body) for item in items] == [(TODAY_PDF, b"%PDF-1.7")]
    assert fetcher.urgent == [TODAY_PDF, TODAY_PDF]
    assert fetcher.no_redirect == [API, TODAY_PDF] * 2
