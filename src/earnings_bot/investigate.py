"""Find the sources of a company nobody has researched: its IR site, how that site can be read,
and which newswire carries its releases. The bot runs this itself when /track adds a company
outside the catalog, and again a month later, outside release windows."""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.ir_research import issuer_links
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.recorder import PollError, poll
from earnings_bot.sources import EdgarFilings, Q4PressReleases, RssFeed, looks_like_earnings
from earnings_bot.text import html_to_text, release_text

log = logging.getLogger(__name__)
REINVESTIGATE = timedelta(days=30)
_HOST = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})\b", re.IGNORECASE)
_IR_HOST = re.compile(r"^(?:ir|investors?|investor-?relations)\.", re.IGNORECASE)
# The dateline that opens a wire release: "NEWARK, Calif., Oct. 5, 2026 /PRNewswire/ --"
_WIRES = (
    ("PR Newswire", re.compile(r"/\s*PRNewswire(?:-[A-Z]+)?\s*/", re.IGNORECASE)),
    ("Business Wire", re.compile(r"\(\s*BUSINESS\s+WIRE\s*\)", re.IGNORECASE)),
    ("GlobeNewswire", re.compile(r"\(\s*GLOBE\s+NEWSWIRE\s*\)", re.IGNORECASE)),
)
# Where IR platforms other than Q4 keep their press release feeds
_FEED_PATHS = (
    "/rss/news-releases.xml",
    "/news-releases/rss",
    "/press-releases/rss",
    "/news-events/press-releases/rss",
)
_LISTING = re.compile(r"/(?:press|news)[-_]?releases?/?$", re.IGNORECASE)
_BLOCKED = frozenset({401, 403, 429})
_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError)


class Chrome(Protocol):
    async def request(
        self,
        url: str,
        warmup: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
    ) -> Response: ...


OpenChrome = Callable[[], AbstractAsyncContextManager[Chrome]]


def due(ticker: str, company: KnownCompany, today: date) -> bool:
    """Whether to look for a company's sources: one outside the catalog, not looked at lately."""
    if ticker in known_companies():
        return False
    return company.investigated is None or today - company.investigated >= REINVESTIGATE


def ir_hosts(text: str) -> list[str]:
    """Hosts a release names that look like an IR site, in the order it names them."""
    hosts = dict.fromkeys(host.lower() for host in _HOST.findall(text))
    return [host for host in hosts if _IR_HOST.match(host)]


def wire(text: str) -> str | None:
    """The newswire named in a release's dateline."""
    return next((name for name, dateline in _WIRES if dateline.search(text[:2000])), None)


class _FeedLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        kind = (values.get("type") or "").lower()
        if tag == "link" and values.get("href") and kind.endswith(("rss+xml", "atom+xml")):
            self.feeds.append(values["href"] or "")


def feed_links(url: str, html: str) -> list[str]:
    """The feeds a page advertises, then the usual places, all on the page's own host."""
    parser = _FeedLinks()
    parser.feed(html)
    host = urlsplit(url).netloc
    candidates = [urljoin(url, href) for href in parser.feeds]
    candidates += [f"https://{host}{path}" for path in _FEED_PATHS]
    return [
        feed
        for feed in dict.fromkeys(candidates)
        if urlsplit(feed).scheme == "https" and urlsplit(feed).netloc == host
    ]


def listing(url: str, html: bytes) -> str | None:
    """The press release list a site's page links to."""
    return next(
        (link for link, _ in issuer_links(url, html) if _LISTING.search(urlsplit(link).path)),
        None,
    )


@dataclass
class Findings:
    site: str | None = None
    q4: bool = False
    feeds: list[str] = field(default_factory=list)
    listing: str | None = None  # read through Chrome
    blocked: bool = False
    wire: str | None = None

    def describe(self, today: date) -> str:
        if self.site is None:
            found = "No IR site found in its last results release."
        elif self.q4:
            found = f"Q4 press feed on {self.site}."
        elif self.feeds:
            found = f"Press release feed <{self.feeds[0]}>."
        elif self.listing:
            why = "blocks the bot" if self.blocked else "has no feed"
            found = f"{self.site} {why}, so Chrome reads <{self.listing}>."
        elif self.blocked:
            found = f"{self.site} blocks the bot, and Chrome couldn't read it either."
        else:
            found = f"{self.site} has no feed or press release list the bot could find."
        on = f"Wire: {self.wire}." if self.wire else "Wire not found, so both are watched."
        return f"Investigated {today:%b %d}: {found} {on}"


async def _last_release(fetcher: Fetcher, cik: int) -> str | None:
    """The text of the company's last results release, as filed with its 8-K."""
    sec = EdgarFilings(cik, limit=40)
    try:
        _, filings = await poll(sec, fetcher)
    except PollError:
        return None
    results = [filing for filing in filings if filing.earnings and filing.title.startswith("8-K ")]
    for filing in results[:2]:
        if text := await release_text(fetcher, sec, filing):
            return text
    return None


async def _get(fetcher: Fetcher, url: str) -> Response | None:
    try:
        return await fetcher.request("GET", url, give_up_after=6)
    except (aiohttp.ClientError, TimeoutError):
        return None


async def _q4_wire(fetcher: Fetcher, host: str) -> tuple[bool, str | None]:
    """Whether the host serves Q4's press release JSON, and the wire its latest results name."""
    reply = await _get(fetcher, Q4PressReleases(host).bodies_url(10))
    if reply is None or reply.status != 200:
        return False, None
    try:
        releases = json.loads(reply.body)["GetPressReleaseListResult"]
    except _ERRORS:
        return False, None
    if not isinstance(releases, list):
        return False, None
    bodies = [
        html_to_text(release.get("Body") or "")
        for release in releases
        if isinstance(release, dict) and looks_like_earnings(release.get("Headline") or "")
    ]
    return True, next((name for body in bodies if (name := wire(body))), None)


async def _feed(fetcher: Fetcher, candidates: list[str]) -> tuple[str | None, str | None]:
    """The first candidate that's a feed with items, and the wire its latest results name."""
    for url in candidates:
        reply = await _get(fetcher, url)
        if reply is None or reply.status != 200:
            continue
        try:
            items = RssFeed("probe", url).parse(reply)
        except _ERRORS:
            continue
        if not items:
            continue
        found = None
        for item in [item for item in items if item.earnings][:1]:
            page = await _get(fetcher, item.url)
            if page and page.status == 200:
                found = wire(html_to_text(page.body.decode(errors="replace")))
        return url, found
    return None, None


async def _through_chrome(
    fetcher: Fetcher, chrome: OpenChrome, home: str, findings: Findings
) -> None:
    """Find the press release list, and the wire from the latest results on it."""
    try:
        async with chrome() as browser:
            page = await browser.request(home, home, 8, fetcher.wait_turn)
            if page.status != 200:
                return
            findings.listing = listing(home, page.body)
            if findings.listing is None:
                return
            page = await browser.request(findings.listing, home, 8, fetcher.wait_turn)
            if page.status != 200:
                findings.listing = None
                return
            articles = [
                link
                for link, title in issuer_links(findings.listing, page.body)
                if looks_like_earnings(title)
            ]
            for link in articles[:1]:
                article = await browser.request(link, home, 8, fetcher.wait_turn)
                if article.status == 200 and not findings.wire:
                    findings.wire = wire(html_to_text(article.body.decode(errors="replace")))
    except (BrowserError, *_ERRORS) as error:
        log.info("Chrome couldn't read %s: %r", home, error)


async def investigate(
    fetcher: Fetcher,
    company: KnownCompany,
    today: date,
    chrome: OpenChrome | None = None,
) -> KnownCompany:
    """The company with what the bot found: its IR feed if the site has one, its press release
    list for Chrome if it hasn't, and its wire. Without `chrome`, a site that needs it goes
    unread."""
    findings = Findings()
    text = await _last_release(fetcher, company.cik) or ""
    findings.wire = wire(text)
    for host in ir_hosts(text)[:3]:
        home = f"https://{host}/"
        page = await _get(fetcher, home)
        if page is None or (page.status >= 400 and page.status not in _BLOCKED):
            continue  # not this host
        findings.site = host
        if page.status in _BLOCKED:
            findings.blocked = True
            if chrome is not None:
                await _through_chrome(fetcher, chrome, home, findings)
            break
        findings.q4, q4_wire = await _q4_wire(fetcher, host)
        findings.wire = findings.wire or q4_wire
        if not findings.q4:
            feed, feed_wire = await _feed(
                fetcher, feed_links(home, page.body.decode("utf-8", "replace"))
            )
            findings.feeds = [feed] if feed else []
            findings.wire = findings.wire or feed_wire
        if not findings.q4 and not findings.feeds and chrome is not None:
            await _through_chrome(fetcher, chrome, home, findings)
        break
    found_wire = findings.wire or "unknown"
    trial = company.trial
    if found_wire == "GlobeNewswire":
        # GlobeNewswire's earnings feed is still on trial for every company
        trial = trial.model_copy(update={"globenewswire": company.names})
    return company.model_copy(
        update={
            "platform": "q4" if findings.q4 else "own-site" if findings.site else "unknown",
            "q4_host": findings.site if findings.q4 else None,
            "reachable": findings.q4 or bool(findings.feeds),
            "rss": findings.feeds,
            "ir_listing": findings.listing,  # only found when there was no feed
            "wire": found_wire,
            "trial": trial,
            "evidence": findings.describe(today),
            "investigated": today,
        }
    )
