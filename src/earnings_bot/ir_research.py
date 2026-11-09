"""Inspect issuer pages and their public JSON away from active windows."""

import os
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import urljoin, urlsplit

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.browser_sources import IRSource, _ReleaseLinks
from earnings_bot.fetch import Fetcher
from earnings_bot.known import NTH, ORD, KnownCompany
from earnings_bot.sources import looks_like_earnings
from earnings_bot.text import html_to_text, verified_report

IR_LISTINGS = {
    "AVGO": "https://investors.broadcom.com/financial-information/financial-news-releases",
    "CRWD": "https://ir.crowdstrike.com/press-releases",
    "FICO": "https://investors.fico.com/news-releases/",
    "HD": "https://ir.homedepot.com/news-releases",
    "HUBS": "https://ir.hubspot.com/news",
    "LLY": "https://investor.lilly.com/",
    "NVDA": "https://investor.nvidia.com/financial-info/financial-reports-and-sec-filings/",
}
_ERRORS = (BrowserError, aiohttp.ClientError, TimeoutError, ValueError, KeyError)
_NEWS = re.compile(r"press|release|earnings|report|financial|news", re.IGNORECASE)


def issuer_listing(ticker: str, company: KnownCompany) -> str | None:
    """The press release listing Chrome reads for a company: one the bot found itself, or one
    researched by hand."""
    return company.ir_listing or IR_LISTINGS.get(ticker)


def issuer_links(url: str, body: bytes) -> list[tuple[str, str]]:
    links = _ReleaseLinks()
    links.feed(body.decode(errors="replace"))
    origin = urlsplit(url).netloc
    return list(
        dict.fromkeys(
            (link, title)
            for href, title in links.links
            if title
            and (link := urljoin(url, href))
            and urlsplit(link).scheme == "https"
            and urlsplit(link).netloc == origin
            and not urlsplit(link).fragment
            and link != url
        )
    )


def _records(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _records(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _records(nested)


def _normalized(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def matches_release(value: object, day: date, titles: set[str]) -> bool:
    normalized = {_normalized(title) for title in titles}
    for row in _records(value):
        headline = next(
            (
                text
                for key, text in row.items()
                if key.lower() in {"headline", "title", "name", "reporttitle"}
                and isinstance(text, str)
                and looks_like_earnings(text)
            ),
            None,
        )
        if headline is None:
            continue
        if _normalized(headline) in normalized:
            return True
        if any(
            isinstance(text, str)
            and key.lower() in {"pressreleasedate", "publish_date", "published", "publisheddate"}
            and text.startswith((day.isoformat(), day.strftime("%m/%d/%Y")))
            for key, text in row.items()
        ):
            return True
    return False


async def inspect_issuer(fetcher: Fetcher, url: str | None, day: date, titles: set[str]) -> str:
    if url is None or os.name != "nt":
        return ""
    try:
        async with fetcher.research_browser() as browser:
            responses = await browser.inspect_json(url)
    except _ERRORS:
        return "Issuer JSON inspection unavailable."
    candidates = {
        f"`{item.method} {item.path}`"
        for item in responses
        if _NEWS.search(item.path) and matches_release(item.value, day, titles)
    }
    if candidates:
        return "Issuer JSON with a matching release listing | " + ", ".join(sorted(candidates)[:4])
    related = {f"`{item.method} {item.path}`" for item in responses if _NEWS.search(item.path)}
    if related:
        return "Issuer JSON leads awaiting release validation | " + ", ".join(sorted(related)[:4])
    return "Issuer browser inspection | no earnings JSON route observed."


def quarter_headline(year: int, quarter: int) -> str:
    label = rf"Q\s*{quarter}|{quarter}\s*Q|(?:{NTH[quarter - 1]}|{ORD[quarter - 1]})[\s-]+quarter"
    return (
        r"^(?!.*\b(?:webcast|conference call|to host|to announce|will report|date|schedule)\b)"
        rf"(?=.*\b(?:{label})\b)(?=.*\b(?:FY\s*{year % 100:02d}|(?:FY\s*)?{year})\b)"
        r"(?=.*\b(?:financial results|earnings|quarterly results|(?:reports?|announces?).*results)"
        r"\b).*"
    )


async def verified_browser_source(
    fetcher: Fetcher, listing: str | None, period: str
) -> IRSource | None:
    """Verify the preceding quarter's article before preparing a live browser source."""
    if os.name != "nt" or listing is None:
        return None
    year, quarter = int(period[2:6]), int(period[-1])
    prior_year, prior_quarter = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
    pattern = re.compile(quarter_headline(prior_year, prior_quarter), re.IGNORECASE)
    try:
        async with fetcher.research_browser() as browser:
            url = listing
            path = urlsplit(listing).path
            seen = set()
            for _ in range(6):
                if url in seen:
                    break
                seen.add(url)
                page = await browser.request(url, listing, 4, fetcher.wait_turn)
                if page.status != 200:
                    return None
                links = issuer_links(url, page.body)
                for link, title in links:
                    if not pattern.fullmatch(title):
                        continue
                    article = await browser.request(link, listing, 4, fetcher.wait_turn)
                    text = html_to_text(article.body.decode(errors="replace"))
                    if (
                        article.status == 200
                        and verified_report(text)
                        and title.casefold() in " ".join(text.split()).casefold()
                    ):
                        return IRSource(
                            listing=listing,
                            headline=quarter_headline(year, quarter),
                            verify_text=True,
                        )
                    break
                next_page = next(
                    (
                        link
                        for link, title in links
                        if urlsplit(link).path == path
                        and urlsplit(link).query
                        and "next" in title.casefold().split()
                    ),
                    None,
                )
                if next_page is None:
                    break
                url = next_page
    except _ERRORS:
        return None
    return None
