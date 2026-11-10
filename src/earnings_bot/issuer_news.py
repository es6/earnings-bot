"""Read a public issuer newsroom API that links dated results PDFs."""

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlsplit

from earnings_bot.browser import browser_host
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.sources import Sighting, Source, looks_like_earnings


@dataclass(frozen=True)
class NewsRecord:
    title: str
    description: str
    url: str
    day: date


def news_records(api: str, body: bytes) -> list[NewsRecord]:
    """Recognize records from an issuer's newsroom, with issuer-hosted PDFs only."""
    origin = browser_host(api).removeprefix("www.")
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return []
    rows = value.get("news") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        return []
    records = []
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

    def __init__(self, api: str, headline: str, day: date | None = None) -> None:
        browser_host(api)
        self.api, self.day = api, day
        self.headline = re.compile(headline, re.IGNORECASE)
        self.candidate: NewsRecord | None = None

    def url(self) -> str:
        return self.api

    async def fetch(self, fetcher: Fetcher) -> Response:
        self.candidate = None
        listing = await read_news(fetcher, self.url())
        if listing.status != 200:
            return listing
        for row in news_records(self.api, listing.body):
            if (
                (self.day is None or row.day == self.day)
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
