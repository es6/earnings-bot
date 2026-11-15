"""Read a public issuer newsroom API that links dated results PDFs."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from earnings_bot.browser import browser_host
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.periods import title_period
from earnings_bot.sec_results import reported_period
from earnings_bot.sources import (
    Sighting,
    Source,
    Spellings,
    bust_cache,
    looks_like_earnings,
)
from earnings_bot.text import pdf_text, verified_report


@dataclass(frozen=True)
class NewsRecord:
    title: str
    description: str
    url: str
    day: date | None  # None when the record gives no date
    period: tuple[int, int] | None = None  # manifests must verify their PDF's own period


# Keys under which a quarterly results record links its press release
_RELEASE_KEYS = ("press_release", "earnings_release", "press_release_url", "earnings_release_url")
_QUARTER = re.compile(r"^\s*Q?([1-4])(?:st|nd|rd|th)?\s*$", re.IGNORECASE)
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
        docs = node.get("docs")
        release = docs.get("pressRelease") if isinstance(docs, dict) else None
        if quarter and year and isinstance(release, dict):
            link = release.get("link")
            label = release.get("title", "")
            if (
                isinstance(link, str)
                and isinstance(label, str)
                and re.search(r"\b(?:press|earnings|results)\s+release\b", label, re.IGNORECASE)
                and not re.search(r"presentation|supplement|webcast|notice", label, re.IGNORECASE)
                and title_period(label) in {None, (int(year), int(quarter[1]))}
                and (url := _issuer_pdf(api, link, origin))
            ):
                period = (int(year), int(quarter[1]))
                title = f"Q{period[1]} {period[0]} Financial Results Press Release"
                records.append(NewsRecord(title, "", url, None, period))
        for child in node.values():
            if isinstance(child, list | dict):
                walk(child, year)

    walk(value, "")
    # Two different releases for the same manifest quarter are ambiguous, never first-wins.
    periods: dict[tuple[int, int], set[str]] = {}
    for row in records:
        if row.period:
            periods.setdefault(row.period, set()).add(row.url)
    return [row for row in records if not row.period or len(periods[row.period]) == 1]


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


class _ManifestPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feed_type = ""
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if "ir-feed-main" in (values.get("class") or "").split():
            self.feed_type = values.get("data-feedtype") or ""
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"] or "")


async def discover_manifest(fetcher: Fetcher, page: str) -> str | None:
    """Replay a public manifest explicitly declared by a page and its linked same-host JS."""
    host = browser_host(page)
    reply = await read_news(fetcher, page)
    if reply.status != 200:
        return None
    parser = _ManifestPage()
    parser.feed(reply.body.decode("utf-8", errors="replace"))
    if parser.feed_type != "quarterly-earnings":
        return None
    prefix = "/services/json/v1/investor-relations/"
    scripts = sorted(parser.scripts, key=lambda value: "clientlib-site" not in value)
    for script in scripts[:2]:
        url = urljoin(page, script)
        if browser_host(url) != host:
            continue
        code = await read_news(fetcher, url)
        declaration = rf"fetch\([\"']{re.escape(prefix)}[\"']\s*\+"
        if code.status == 200 and re.search(
            declaration, code.body.decode("utf-8", errors="replace")
        ):
            api = urljoin(page, prefix + parser.feed_type + ".json")
            data = await read_news(fetcher, api)
            if data.status == 200 and news_records(api, data.body):
                return api
    return None


class IssuerNews(Source):
    """Verify the current quarter's new PDF and hand its bytes to the alert pipeline."""

    name = "issuer-news"
    interval = 1.0
    report_initial = True
    stop_when_found = True
    ok_statuses = frozenset({200, 404})

    def __init__(
        self,
        api: str,
        headline: str,
        day: date | None = None,
        *,
        cache_bust: bool = False,
        spell: bool = False,
    ) -> None:
        browser_host(api)
        self.api, self.day = api, day
        self.headline = re.compile(headline, re.IGNORECASE)
        self.cache_bust = cache_bust
        self.spellings = Spellings(api) if spell else None
        self.candidate: NewsRecord | None = None
        self.manifest_verified = False

    def url(self) -> str:
        if self.spellings:
            return self.spellings.next()
        return bust_cache(self.api) if self.cache_bust else self.api

    async def fetch(self, fetcher: Fetcher) -> Response:
        self.candidate = None
        self.manifest_verified = False
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
                response = await fetcher.request(
                    "GET", row.url, give_up_after=self.timeout, urgent=True, allow_redirects=False
                )
                if row.period and response.status == 200 and response.body.startswith(b"%PDF-"):
                    text = await asyncio.to_thread(pdf_text, response.body)
                    self.manifest_verified = (
                        verified_report(text)
                        and reported_period(text) == row.period
                        and browser_host(self.api).removeprefix("www.") in text.casefold()
                        and not re.search(
                            r"\b(?:presentation|webcast)\b", text[:600], re.IGNORECASE
                        )
                    )
                return response
        return listing

    def parse(self, response: Response) -> list[Sighting]:
        row = self.candidate
        if (
            row
            and response.url == row.url
            and response.status == 200
            and response.body.startswith(b"%PDF-")
            and (row.period is None or self.manifest_verified)
        ):
            return [Sighting(row.url, row.title, row.url, earnings=True, body=response.body)]
        return []


class IssuerResults(IssuerNews):
    """The independently published results resources, alongside the newsroom."""

    name = "issuer-results"
