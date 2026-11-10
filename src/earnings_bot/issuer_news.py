"""Read a public issuer newsroom API that links dated results PDFs."""

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

from earnings_bot.browser import browser_host
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.sources import Sighting, Source, bust_cache, looks_like_earnings


@dataclass(frozen=True)
class NewsRecord:
    title: str
    description: str
    url: str
    day: date | None  # None when the record gives no date


# Keys under which a quarterly results record links its press release
_RELEASE_KEYS = ("press_release", "earnings_release", "press_release_url", "earnings_release_url")
_QUARTER = re.compile(r"^\s*Q?([1-4])\s*$", re.IGNORECASE)
_FILE_DAY = re.compile(r"(20\d{2})[.\-_](\d{2})[.\-_](\d{2})")


def _issuer_pdf(api: str, link: str, origin: str) -> str | None:
    try:
        url = urljoin(api, link)
        host = browser_host(url).removeprefix("www.")
    except ValueError:
        return None
    issuer = host == origin or host.endswith(f".{origin}")
    return url if issuer and urlsplit(url).path.lower().endswith(".pdf") else None


def _results_records(api: str, value: object, origin: str) -> list[NewsRecord]:
    """Quarterly results resources, which give a quarter and its press release instead of a
    headline, as Sea's results page's API does: {"year": 2026, "data": [{"quarter": "Q3",
    "press_release": ".../2026.11.10 Sea Third Quarter 2026 Results.pdf"}]}."""
    records: list[NewsRecord] = []

    def walk(node: object, year: str) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, year)
            return
        if not isinstance(node, dict):
            return
        named = node.get("year") or node.get("fiscal_year")
        if isinstance(named, int | str) and re.fullmatch(r"20\d{2}", str(named)):
            year = str(named)
        quarter = _QUARTER.match(str(node.get("quarter", "")))
        link = next((node[key] for key in _RELEASE_KEYS if isinstance(node.get(key), str)), "")
        if quarter and year and link and (url := _issuer_pdf(api, link, origin)):
            stamp = _FILE_DAY.search(unquote(url))
            try:
                day = date(*map(int, stamp.groups())) if stamp else None
            except ValueError:
                day = None
            title = f"Q{quarter[1]} {year} Financial Results Press Release"
            records.append(NewsRecord(title, "", url, day))
        for child in node.values():
            if isinstance(child, list | dict):
                walk(child, year)

    walk(value, "")
    return records


def news_records(api: str, body: bytes) -> list[NewsRecord]:
    """Recognize records from an issuer's newsroom or results page, with issuer-hosted PDFs
    only."""
    origin = browser_host(api).removeprefix("www.")
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return []
    results = _results_records(api, value, origin)
    rows = value.get("news") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        return results
    records = list(results)
    for row in rows:
        if not isinstance(row, dict):
            continue
        title, link, stamp = (row.get(key) for key in ("title", "document_url", "publish_date"))
        if not isinstance(title, str) or not isinstance(link, str) or not isinstance(stamp, str):
            continue
        try:
            day = date.fromisoformat(stamp.replace("/", "-"))
            url = urljoin(api, link)
            host = browser_host(url).removeprefix("www.")
        except ValueError:
            continue
        if (host == origin or host.endswith(f".{origin}")) and urlsplit(url).path.lower().endswith(
            ".pdf"
        ):
            description = row.get("description")
            records.append(
                NewsRecord(title, description if isinstance(description, str) else "", url, day)
            )
    return records


async def read_news(fetcher: Fetcher, url: str) -> Response:
    return await fetcher.request("GET", url, give_up_after=4, allow_redirects=False)


class IssuerNews(Source):
    """Verify the current quarter's new PDF and hand its bytes to the alert pipeline."""

    name = "issuer-news"
    interval = 1.0
    report_initial = True
    stop_when_found = True
    ok_statuses = frozenset({200, 404})

    def __init__(
        self, api: str, headline: str, day: date | None = None, *, cache_bust: bool = False
    ) -> None:
        browser_host(api)
        self.api, self.day = api, day
        self.headline = re.compile(headline, re.IGNORECASE)
        self.cache_bust = cache_bust
        self.candidate: NewsRecord | None = None

    def url(self) -> str:
        return bust_cache(self.api) if self.cache_bust else self.api

    async def fetch(self, fetcher: Fetcher) -> Response:
        self.candidate = None
        listing = await read_news(fetcher, self.url())
        if listing.status != 200:
            return listing
        for row in news_records(self.api, listing.body):
            if (
                (self.day is None or row.day is None or row.day == self.day)
                and self.headline.fullmatch(row.title)
                and looks_like_earnings(row.title)
            ):
                self.candidate = row
                return await fetcher.request(
                    "GET", row.url, give_up_after=self.timeout, urgent=True, allow_redirects=False
                )
        return listing

    def parse(self, response: Response) -> list[Sighting]:
        row = self.candidate
        if (
            row
            and response.url == row.url
            and response.status == 200
            and response.body.startswith(b"%PDF-")
        ):
            return [Sighting(row.url, row.title, row.url, earnings=True, body=response.body)]
        return []
