"""Inspect issuer pages and their public JSON away from active windows."""

import os
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import urljoin, urlsplit

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.browser_sources import _ReleaseLinks
from earnings_bot.fetch import Fetcher
from earnings_bot.sources import looks_like_earnings

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
_NEWS = re.compile(r"press|release|earnings|report|financial|news", re.I)


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


async def inspect_issuer(fetcher: Fetcher, ticker: str, day: date, titles: set[str]) -> str:
    url = IR_LISTINGS.get(ticker)
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
