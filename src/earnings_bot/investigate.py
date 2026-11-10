"""Find the sources of a company nobody has researched: its IR site, how that site can be read,
and which newswire carries its releases. The bot runs this itself when /track adds a company
outside the catalog, and again a month later, outside release windows."""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.ir_research import issuer_links
from earnings_bot.issuers import SEC_SUBMISSIONS, filing_form, release_time
from earnings_bot.known import KnownCompany, SourceCheck, known_companies
from earnings_bot.recorder import ET, PollError, poll
from earnings_bot.sec_results import SixKResults, SixKUnavailable
from earnings_bot.sources import (
    EdgarFilings,
    Q4PressReleases,
    RssFeed,
    looks_like_earnings,
    published_time,
)
from earnings_bot.text import html_to_text, release_text

log = logging.getLogger(__name__)
REINVESTIGATE = timedelta(days=30)
_HOST = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})\b", re.IGNORECASE)
_IR_HOST = re.compile(r"^(?:ir|investors?|investor-?relations)\.", re.IGNORECASE)
_IR_LINK = re.compile(
    r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})/"
    r"(?:investor-relations|events-news|news-events|press-releases|reports-filings)(?:[/\s]|$)",
    re.IGNORECASE,
)
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
# Where IR sites usually keep it, for one whose home page doesn't link it plainly
_LISTINGS = ("/press-releases", "/news-releases")
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
    return (
        (company.sec_form is None and not company.eightk)
        or company.investigated is None
        # Investigated before it kept what it checked, which the scorecard shows
        or not company.checks
        or today - company.investigated >= REINVESTIGATE
    )


def ir_hosts(text: str, ticker: str | None = None) -> list[str]:
    """Hosts a release names that look like an IR site, in the order it names them."""
    hosts = dict.fromkeys(host.lower() for host in _HOST.findall(text))
    prefixes = {host.lower() for host in _IR_LINK.findall(text)}
    candidates = []
    for host in hosts:
        without_www = host.removeprefix("www.")
        if ticker and without_www.split(".")[0] == ticker.lower():
            candidates.append(without_www)
        elif _IR_HOST.match(host) or host in prefixes:
            candidates.append(host)
    return list(dict.fromkeys(candidates))


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
class Report:
    """What an investigation is doing, and what it has decided so far, so the scorecard can show
    it as it goes."""

    step: str = "Starting"
    checks: list[SourceCheck] = field(default_factory=list)
    changed: Callable[[], None] = lambda: None

    def doing(self, step: str) -> None:
        self.step = step
        self.changed()

    def watch(self, source: str, why: str) -> None:
        self.checks.append(SourceCheck(source=source, watch=True, why=why))
        self.changed()

    def reject(self, source: str, why: str) -> None:
        self.checks.append(SourceCheck(source=source, watch=False, why=why))
        self.changed()


@dataclass
class Findings:
    site: str | None = None
    q4: bool = False
    feeds: list[str] = field(default_factory=list)
    listing: str | None = None  # read through Chrome
    blocked: bool = False
    wire: str | None = None

    def describe(self, today: date) -> str:
        chrome = f"Chrome reads <{self.listing}>." if self.listing else ""
        if self.site is None:
            found = "No IR site found in its last results release."
        elif self.q4:
            found = f"Q4 press feed on {self.site}."
        elif self.feeds:
            found = f"Press release feed <{self.feeds[0]}>."
        elif self.listing:
            why = "blocks the bot" if self.blocked else "has no feed"
            found, chrome = f"{self.site} {why}, so Chrome reads <{self.listing}>.", ""
        elif self.blocked:
            found = f"{self.site} blocks the bot, and Chrome couldn't read it either."
        else:
            found = f"{self.site} has no feed or press release list the bot could find."
        on = f"Wire: {self.wire}." if self.wire else "Wire not found, so both are watched."
        parts = (f"Investigated {today:%b %d}:", found, chrome, on)
        return " ".join(part for part in parts if part)


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


def _answer(reply: Response | None) -> str:
    return "no answer" if reply is None else f"HTTP {reply.status}"


async def _q4_wire(fetcher: Fetcher, host: str) -> tuple[bool, str | None, str]:
    """Whether the host serves Q4's press release JSON, the wire its latest results name, and
    why it doesn't serve it."""
    reply = await _get(fetcher, Q4PressReleases(host).bodies_url(10))
    if reply is None or reply.status != 200:
        return False, None, f"Not a Q4 site ({_answer(reply)})"
    try:
        releases = json.loads(reply.body)["GetPressReleaseListResult"]
    except _ERRORS:
        releases = None
    if not isinstance(releases, list):
        return False, None, "Not a Q4 site (its answer has no press release list)"
    bodies = [
        html_to_text(release.get("Body") or "")
        for release in releases
        if isinstance(release, dict) and looks_like_earnings(release.get("Headline") or "")
    ]
    return True, next((name for body in bodies if (name := wire(body))), None), ""


async def _feed(fetcher: Fetcher, candidates: list[str]) -> tuple[str | None, str | None, str]:
    """The first candidate that's a feed with items, the wire its latest results name, and what
    the others answered."""
    tried = []
    for url in candidates:
        reply = await _get(fetcher, url)
        if reply is None or reply.status != 200:
            tried.append(f"{urlsplit(url).path} {_answer(reply)}")
            continue
        try:
            items = RssFeed("probe", url).parse(reply)
        except _ERRORS:
            items = []
        if not items:
            tried.append(f"{urlsplit(url).path} isn't a feed of releases")
            continue
        found = None
        for item in [item for item in items if item.earnings][:1]:
            page = await _get(fetcher, item.url)
            if page and page.status == 200:
                found = wire(html_to_text(page.body.decode(errors="replace")))
        return url, found, ""
    return None, None, ", ".join(tried)


@dataclass
class ChromeVisit:
    listing: str | None = None  # the press release list Chrome read
    title: str | None = None  # the latest results it opened from there
    wire: str | None = None
    problem: str | None = None  # why it couldn't read the latest results


async def _through_chrome(fetcher: Fetcher, chrome: OpenChrome, home: str) -> ChromeVisit:
    """Open the IR site in Chrome, find its press release list and read the latest results on
    it."""
    visit = ChromeVisit()
    host = urlsplit(home).netloc
    try:
        async with chrome() as browser:
            page = await browser.request(home, home, 8, fetcher.wait_turn)
            if page.status != 200:
                visit.problem = f"Chrome got HTTP {page.status} from {host}"
                return visit
            linked = listing(home, page.body)
            candidates = [f"https://{host}{path}" for path in _LISTINGS]
            if linked:
                candidates.insert(0, linked)
            links: list[tuple[str, str]] = []
            for url in dict.fromkeys(candidates):
                page = await browser.request(url, home, 8, fetcher.wait_turn)
                if page.status == 200 and (links := issuer_links(url, page.body)):
                    visit.listing = url
                    break
            if visit.listing is None:
                visit.problem = f"Chrome found no press release list on {host}"
                return visit
            articles = [(link, title) for link, title in links if looks_like_earnings(title)]
            if not articles:
                visit.problem = f"<{visit.listing}> shows no results headline"
                return visit
            link, title = articles[0]
            article = await browser.request(link, home, 8, fetcher.wait_turn)
            if article.status != 200:
                visit.problem = f"Chrome got HTTP {article.status} opening “{title}”"
                return visit
            visit.title = title
            visit.wire = wire(html_to_text(article.body.decode(errors="replace")))
    except (BrowserError, *_ERRORS) as error:
        log.info("Chrome couldn't read %s: %r", home, error)
        visit.problem = f"Chrome couldn't read {host} ({type(error).__name__})"
    return visit


def _wire_checks(report: Report, found: str | None) -> None:
    for name in ("PR Newswire", "Business Wire", "GlobeNewswire"):
        source = f"{name} earnings feed"
        if found == name:
            trial = " On trial: recorded, not posted yet." if name == "GlobeNewswire" else ""
            report.watch(source, f"Its latest results release went out on {name}.{trial}")
        elif found:
            report.reject(source, f"Its latest results release went out on {found}")
        elif name == "GlobeNewswire":
            report.reject(source, "Its release names no wire, and this feed is only watched then")
        else:
            report.watch(source, "Its release names no wire, so both main wires are watched")


async def investigate(
    fetcher: Fetcher,
    company: KnownCompany,
    today: date,
    chrome: OpenChrome | None = None,
    *,
    sixk: SixKResults | None = None,
    ticker: str | None = None,
    report: Report | None = None,
) -> KnownCompany:
    """The company with what the bot found: its IR feed if the site has one, its press release
    list read through Chrome, and its wire. Without `chrome`, the IR site's pages go unread.
    `report` follows along."""
    report = report or Report()
    findings = Findings()
    form = company.sec_form
    filings = None
    report.doing("Reading its SEC filings")
    if form is None and not company.eightk:
        reply = await _get(fetcher, SEC_SUBMISSIONS.format(cik=company.cik))
        if reply is None or reply.status != 200:
            raise SixKUnavailable("SEC issuer form lookup unavailable")
        form = filing_form(json.loads(reply.body))
        if form == "6-K":
            filings = EdgarFilings(company.cik, limit=500, form=form).parse(reply)
    company = company.model_copy(update={"sec_form": form or "8-K"})
    report.doing(f"Reading its last results {company.sec_form} on SEC")
    if company.sec_form == "6-K":
        verifier = sixk or SixKResults(fetcher)
        results = await verifier.history(
            company.cik, datetime.combine(today, time.max, ET), filings=filings
        )
        accepted = [
            stamp for result in results if (stamp := published_time(result.published)) is not None
        ]
        if accepted:
            company = company.model_copy(
                update={
                    "release_time": release_time(accepted),
                    "sixk": [f"{stamp.astimezone(ET):%H:%M}" for stamp in accepted],
                }
            )
        text = (results[0].body or b"").decode() if results else ""
    else:
        text = await _last_release(fetcher, company.cik) or ""
    times = company.sixk if company.sec_form == "6-K" else company.eightk
    report.watch(
        f"SEC {company.sec_form} filings",
        f"Its last results {company.sec_form}s were accepted at {', '.join(times)} ET"
        if times
        else f"Every results {company.sec_form} is filed here",
    )
    findings.wire = wire(text)
    hosts = ir_hosts(text, ticker)[:3]
    if not hosts:
        report.reject(
            "IR site",
            "Its last results release names no IR site"
            if text
            else f"Couldn't read its last results {company.sec_form}",
        )
    for host in hosts:
        home = f"https://{host}/"
        report.doing(f"Checking {host}")
        page = await _get(fetcher, home)
        if page is None or (page.status >= 400 and page.status not in _BLOCKED):
            report.reject(host, f"Not its IR site ({_answer(page)})")
            continue
        findings.site = host
        findings.blocked = page.status in _BLOCKED
        if findings.blocked:
            report.reject(
                f"{host} without Chrome", f"Blocks automated requests (HTTP {page.status})"
            )
        else:
            report.doing(f"Looking for a Q4 press release feed on {host}")
            findings.q4, q4_wire, why = await _q4_wire(fetcher, host)
            findings.wire = findings.wire or q4_wire
            if findings.q4:
                report.watch(f"Q4 press release feed on {host}", "Lists its releases and files")
            else:
                report.reject(f"Q4 press release feed on {host}", why)
                report.doing(f"Looking for a press release feed on {host}")
                feed, feed_wire, tried = await _feed(
                    fetcher, feed_links(home, page.body.decode("utf-8", "replace"))
                )
                findings.feeds = [feed] if feed else []
                findings.wire = findings.wire or feed_wire
                if feed:
                    report.watch(f"Press release feed <{feed}>", "Lists its releases")
                else:
                    report.reject(f"Press release feed on {host}", f"None found: {tried}")
        if chrome is None:
            report.reject(f"{host} press release page", "Chrome isn't available on this computer")
        else:
            report.doing(f"Opening {host} in Chrome")
            visit = await _through_chrome(fetcher, chrome, home)
            findings.wire = findings.wire or visit.wire
            # Kept even without readable results: date notices are read from it too
            findings.listing = visit.listing
            if visit.title and visit.listing:
                report.watch(
                    f"Press release page <{visit.listing}>, read in Chrome",
                    f"Chrome read its latest results, “{visit.title}”",
                )
            else:
                report.reject(f"{host} press release page", visit.problem or "Unread")
        if findings.listing is None and not (findings.blocked or findings.q4 or findings.feeds):
            # The list a plain request finds, for date notices
            findings.listing = listing(home, page.body)
        break
    report.doing("Choosing its sources")
    _wire_checks(report, findings.wire)
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
            "ir_listing": findings.listing,
            "wire": found_wire,
            "trial": trial,
            "evidence": findings.describe(today),
            "investigated": today,
            "checks": report.checks,
        }
    )
