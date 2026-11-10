"""Inspect issuer pages and their public JSON away from active windows."""

import os
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import urljoin, urlsplit

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.browser_sources import (
    BrowserRss,
    HttpRelease,
    IRSource,
    _ReleaseLinks,
    article_matches,
    issuer_pdf_url,
)
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
_CHROME_FEED_HOSTS = frozenset(
    {
        "ir.lucidmotors.com",
        "investors.fico.com",
        "investor.lilly.com",
        "ir.crowdstrike.com",
        "investors.broadcom.com",
    }
)


def chrome_available() -> bool:
    """Desktop Chrome is driven only on Windows."""
    return os.name == "nt"


def issuer_listing(ticker: str, company: KnownCompany) -> str | None:
    """The press release listing Chrome reads for a company: one the bot found itself, or one
    researched by hand."""
    return company.ir_listing or IR_LISTINGS.get(ticker)


def issuer_links(url: str, body: bytes) -> list[tuple[str, str]]:
    links = _ReleaseLinks()
    links.feed(body.decode(errors="replace"))
    origin = urlsplit(url).netloc
    articles = []
    for href, title in [*links.links, *links.structured]:
        try:
            link = urljoin(url, href)
            parts = urlsplit(link)
        except ValueError:
            continue
        if (
            title
            and parts.scheme == "https"
            and parts.netloc == origin
            and not parts.fragment
            and link != url
        ):
            articles.append((link, title))
    return list(dict.fromkeys(articles))


def next_page(listing: str, links: list[tuple[str, str]]) -> str | None:
    """The link to a press release list's next page, such as "Next" to ?page=1."""
    path = urlsplit(listing).path
    return next(
        (
            link
            for link, title in links
            if urlsplit(link).path == path
            and urlsplit(link).query
            and "next" in title.casefold().split()
        ),
        None,
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
    if url is None or not chrome_available():
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


async def verified_http_source(
    fetcher: Fetcher, listing: str | None, period: str
) -> IRSource | None:
    """Promote direct HTTP only if the live poller can read the previous results."""
    if listing is None:
        return None
    year, quarter = int(period[2:6]), int(period[-1])
    prior = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
    try:
        probe = HttpRelease(
            listing, quarter_headline(*prior), verify_text=True, chrome_fallback=False
        )
        response = await probe.fetch(fetcher)
        items = probe.parse(response)
        if not items:
            return None
        item = items[0]
        if (body := item.body) is None:
            return None
        if not verified_report(html_to_text(body.decode(errors="replace"))):
            pdf = issuer_pdf_url(item.url, body, title=item.title)
            if not pdf:
                return None
            file = await fetcher.request("GET", pdf, give_up_after=4, allow_redirects=False)
            if file.status != 200 or not file.body.startswith(b"%PDF-"):
                return None
    except _ERRORS:
        return None
    return IRSource(
        listing=listing, headline=quarter_headline(year, quarter), verify_text=True, http_first=True
    )


async def verified_browser_source(
    fetcher: Fetcher, listing: str | None, period: str
) -> tuple[IRSource | None, str]:
    """A live browser source for `period`, once last quarter's results article verifies on the
    listing, or why there isn't one."""
    if not chrome_available():
        return None, "Chrome isn't available on this computer"
    if listing is None:
        return None, "No press release page found for it"
    year, quarter = int(period[2:6]), int(period[-1])
    prior_year, prior_quarter = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
    pattern = re.compile(quarter_headline(prior_year, prior_quarter), re.IGNORECASE)
    headline = quarter_headline(year, quarter)
    last = f"Q{prior_quarter} {prior_year}"
    problem = f"No {last} results headline on <{listing}> to check the pattern against"
    try:
        async with fetcher.research_browser() as browser:
            if urlsplit(listing).hostname in _CHROME_FEED_HOSTS:
                feed = BrowserRss(
                    urljoin(listing, "/rss/news-releases.xml"), listing, pattern.pattern
                )
                try:
                    reply = await browser.request(feed.url(), listing, 4, fetcher.wait_turn)
                    if reply.status == 200:
                        for item in feed.parse(reply):
                            article = await browser.request(item.url, listing, 4, fetcher.wait_turn)
                            if article.status == 200 and article_matches(
                                item.title, article.body, verify_text=True
                            ):
                                return (
                                    IRSource(
                                        rss=feed.url(),
                                        listing=listing,
                                        headline=headline,
                                        verify_text=True,
                                    ),
                                    f"Verified last quarter's results in RSS, “{item.title}”",
                                )
                except _ERRORS:
                    pass  # The HTML listing remains a candidate.
            url = listing
            seen = set()
            for _ in range(6):
                if url in seen:
                    break
                seen.add(url)
                page = await browser.request(url, listing, 4, fetcher.wait_turn)
                if page.status != 200:
                    return None, f"Chrome got HTTP {page.status} from <{url}>"
                links = issuer_links(url, page.body)
                for link, title in links:
                    if not pattern.fullmatch(title) or not looks_like_earnings(title):
                        continue
                    problem = f"Last quarter's article “{title}” didn't read as a results report"
                    article = await browser.request(link, listing, 4, fetcher.wait_turn)
                    if article.status != 200 or not article_matches(
                        title, article.body, verify_text=True, article=link
                    ):
                        continue
                    if not verified_report(html_to_text(article.body.decode(errors="replace"))):
                        pdf = issuer_pdf_url(link, article.body, title=title)
                        if pdf is None:
                            continue
                        if urlsplit(pdf).netloc == urlsplit(listing).netloc:
                            file = await browser.request(pdf, listing, 4, fetcher.wait_turn)
                        else:
                            file = await fetcher.request(
                                "GET", pdf, give_up_after=4, allow_redirects=False
                            )
                        if file.status != 200 or not file.body.startswith(b"%PDF-"):
                            continue
                    source = IRSource(listing=listing, headline=headline, verify_text=True)
                    return source, f"Last quarter's results, “{title}”, read in Chrome"
                following = next_page(listing, links)
                if following is None:
                    break
                url = following
    except _ERRORS as error:
        return None, f"Chrome couldn't read <{listing}> ({type(error).__name__})"
    return None, problem
