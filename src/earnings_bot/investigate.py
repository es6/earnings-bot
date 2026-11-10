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

from earnings_bot.browser import BrowserError, browser_host
from earnings_bot.browser_sources import _ReleaseLinks, article_matches
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.formatting import clock
from earnings_bot.ir_research import issuer_links, next_page
from earnings_bot.issuers import SEC_SUBMISSIONS, filing_form, release_time
from earnings_bot.known import KnownCompany, SourceCheck, known_companies
from earnings_bot.recorder import ET, PollError, poll
from earnings_bot.sec_results import SixKResults, SixKUnavailable
from earnings_bot.sources import (
    EdgarFilings,
    Q4PressReleases,
    RssFeed,
    is_date_notice,
    looks_like_earnings,
    published_time,
)
from earnings_bot.text import html_to_text, release_text, verified_report

log = logging.getLogger(__name__)
REINVESTIGATE = timedelta(days=30)
# Raised when the investigation learns to find more, so companies get looked at again:
# 1 kept what it checked, 2 reads the wire from any release, skips date notices, looks a few
# pages back for the latest results and keeps when the IR site says they were posted,
# 3 follows corporate navigation and embedded listings, 4 reads newsroom cards and archives,
# 5 finds results behind ordinary filings, 6 checks named RSS indexes and listing HTTP,
# 7 checks Q4 feeds independently of the issuer homepage.
INVESTIGATION = 7
_HOST = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})\b", re.IGNORECASE)
_WEB_HOST = re.compile(r"\b(?:https?://|(?=www\.))" + _HOST.pattern, re.IGNORECASE)
_IR_HOST = re.compile(r"^(?:ir|investors?|investor-?relations)\.", re.IGNORECASE)
_IR_LINK = re.compile(
    r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})/"
    r"(?:investor-relations|events-news|news-events|press-releases|reports-filings)(?:[/\s]|$)",
    re.IGNORECASE,
)
_RSS_INDEX = re.compile(
    r"https://[a-z0-9.-]+/(?:[a-z0-9_-]+/)*(?:rss-feeds?|rss)(?:\.html?)?(?=[\s<>,.)]|$)",
    re.IGNORECASE,
)
_RSS_LINK = re.compile(r"(?:\.xml|\.rss|\.atom|rss(?:-?feed)?(?:\.json)?)$", re.IGNORECASE)
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
_LISTING = re.compile(
    r"/(?:newsroom|(?:press|news|financial)[-_]?releases?(?:-footer)?)/?$", re.IGNORECASE
)
_IR_NAV = re.compile(r"investors?(?: relations| hub| center| centre| information)?", re.IGNORECASE)
# Where IR sites usually keep it, for one whose home page doesn't link it plainly
_LISTINGS = ("/press-releases", "/news-releases")
_BLOCKED = frozenset({401, 403, 429})
# When an IR page says it was posted: "Aug 13, 2026 at 4:01 PM EDT"
_POSTED = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(20\d{2})"
    r",?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s+(?:EDT|EST|ET|Eastern)\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    name: i
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
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
        # Investigated by an older version, which missed things this one finds
        or company.investigation < INVESTIGATION
        or today - company.investigated >= REINVESTIGATE
    )


def ir_hosts(text: str, ticker: str | None = None, name: str = "") -> list[str]:
    """IR hosts first, then corporate websites that match the issuer's name."""
    hosts = dict.fromkeys(host.lower() for host in _HOST.findall(text))
    prefixes = {host.lower() for host in _IR_LINK.findall(text)}
    candidates = []
    for host in hosts:
        without_www = host.removeprefix("www.")
        if ticker and without_www.split(".")[0] == ticker.lower():
            candidates.append(without_www)
        elif _IR_HOST.match(host) or host in prefixes:
            candidates.append(host)
    words = re.findall(r"[a-z0-9]+", name.lower())[:3]
    brands = {"".join(words[:end]) for end in range(1, len(words) + 1) if len(words[0]) >= 3}
    candidates.extend(
        host.lower()
        for host in _WEB_HOST.findall(text)
        if host.lower().removeprefix("www.").split(".")[0] in brands
    )
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


class _IssuerNavigation(_ReleaseLinks):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag == "iframe" and (src := dict(attrs).get("src")):
            self.frames.append(src)


def _release_pages(url: str, html: bytes) -> list[str]:
    """The press release pages linked or embedded by the issuer, including hosted IR sites."""
    parser = _IssuerNavigation()
    parser.feed(html.decode(errors="replace"))
    origin = urlsplit(url)
    pages = []
    for href in [*parser.frames, *(href for href, _ in parser.links)]:
        try:
            page = urljoin(url, href)
            parts = urlsplit(page)
            if not _LISTING.search(parts.path) or (
                parts.netloc == origin.netloc and parts.path.rstrip("/") == origin.path.rstrip("/")
            ):
                continue
            browser_host(page)
        except ValueError:
            continue
        # Public GCS listings also work without the token attached to their embed URLs.
        if (
            parts.hostname
            and parts.hostname.endswith(".gcs-web.com")
            and re.fullmatch(r"auth_token=[a-zA-Z0-9-]+", parts.query)
        ):
            page = parts._replace(query="").geturl()
        pages.append(page)
    return list(dict.fromkeys(pages))


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
    ir_http_status: int | None = None
    blocked: bool = False
    wire: str | None = None
    posted: datetime | None = None  # when the IR site says the latest results were posted
    release_url: str | None = None

    def describe(self, today: date) -> str:
        chrome = f"Chrome reads <{self.listing}>." if self.listing else ""
        if self.q4:
            found = f"Q4 press feed on {self.site}."
        elif self.feeds:
            found = f"Press release feed <{self.feeds[0]}>."
        elif self.site is None:
            found = "No issuer press release site could be verified."
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
    # Read more rows from the same submissions response. Only results exhibits get fetched.
    sec = EdgarFilings(cik, limit=500)
    try:
        _, filings = await poll(sec, fetcher)
    except PollError:
        return None
    results = [filing for filing in filings if filing.earnings and filing.title.startswith("8-K ")]
    for filing in results[:2]:
        if text := await release_text(fetcher, sec, filing):
            return text
    return None


async def _get(fetcher: Fetcher, url: str, *, allow_redirects: bool = True) -> Response | None:
    try:
        return await fetcher.request("GET", url, give_up_after=6, allow_redirects=allow_redirects)
    except (aiohttp.ClientError, TimeoutError):
        return None


def _answer(reply: Response | None) -> str:
    if reply is None:
        return "no answer"
    return f"HTTP {reply.status}" if reply.status else "a redirect"


async def _q4_wire(fetcher: Fetcher, host: str) -> tuple[bool, str | None, str]:
    """Whether the host serves Q4's press release JSON, the wire its latest results name, and
    why it doesn't serve it."""
    reply = await _get(fetcher, Q4PressReleases(host).bodies_url(10))
    if reply and reply.status in _BLOCKED:
        return False, None, f"Q4 feed returned HTTP {reply.status}"
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


async def _named_feed(fetcher: Fetcher, text: str, report: Report) -> tuple[str | None, str | None]:
    """Check public feed indexes explicitly named in a verified SEC release."""
    for index in list(dict.fromkeys(_RSS_INDEX.findall(text)))[:2]:
        url = index
        try:
            host = browser_host(url)
        except ValueError:
            continue
        report.doing(f"Reading the RSS index named by the issuer on {host}")
        page = await _get(fetcher, url, allow_redirects=False)
        if page and 300 <= page.status < 400 and (location := page.headers.get("location")):
            try:
                canonical = urljoin(url, location)
                if browser_host(canonical) != host:
                    continue
            except ValueError:
                continue
            url = canonical
            page = await _get(fetcher, url, allow_redirects=False)
        if not page or page.status != 200:
            report.reject(f"RSS index <{url}>", _answer(page))
            continue
        candidates = [
            (link, title)
            for link, title in issuer_links(url, page.body)
            if _RSS_LINK.search(urlsplit(link).path)
        ]
        candidates.sort(key=lambda item: "press" not in " ".join(item).casefold())
        feed, found_wire, tried = await _feed(fetcher, [link for link, _ in candidates[:4]])
        if feed:
            return feed, found_wire
        report.reject(f"RSS index <{url}>", tried or "No same-site feed links found")
    return None, None


@dataclass
class ChromeVisit:
    listing: str | None = None  # the press release list Chrome read
    title: str | None = None  # the latest results it opened from there
    url: str | None = None  # and their page
    posted: datetime | None = None  # when that page says they were posted
    wire: str | None = None
    problem: str | None = None  # why it couldn't read the latest results


async def _read_results(
    get: Callable[[str], Awaitable[Response | None]],
    url: str,
    links: list[tuple[str, str]],
    sec_intro: str,
) -> ChromeVisit:
    visit = ChromeVisit(listing=url)
    notices: list[str] = []
    tried: list[str] = []
    pages = {url}
    opened: set[str] = set()
    # Results can be a page or two back behind date notices and general press releases.
    for page_number in range(3):
        notices.extend(title for _, title in links if is_date_notice(title))
        articles = {
            link: title
            for link, title in links
            if link not in opened and looks_like_earnings(title)
        }
        # An exact match to the SEC release goes ahead of unrelated "results" in a mixed archive.
        ranked = sorted(articles.items(), key=lambda item: item[1].casefold() not in sec_intro)
        for link, title in ranked[:3]:
            opened.add(link)
            article = await get(link)
            if article is None or article.status != 200:
                tried.append(f"“{title}” ({_answer(article)})")
                continue
            text = html_to_text(article.body.decode(errors="replace"))
            if verified_report(text) or article_matches(
                title, article.body, verify_text=True, article=link
            ):
                visit.title, visit.url, visit.posted = title, link, posted_time(text)
                visit.wire = wire(text)
                return visit
            tried.append(f"“{title}” isn't a results report")
        following = next_page(url, links)
        if page_number == 2 or following is None or following in pages:
            break
        pages.add(following)
        page = await get(following)
        if page is None or page.status != 200:
            break
        links = issuer_links(following, page.body)
    if tried:
        visit.problem = "Chrome opened " + ", ".join(tried)
    elif notices:
        visit.problem = (
            f"<{url}> shows a date notice, “{notices[0]}”, but no results release to read"
        )
    else:
        visit.problem = f"<{url}> shows no results headline"
    return visit


async def _through_chrome(
    fetcher: Fetcher, chrome: OpenChrome, home: str, sec_release: str, *, q4: bool = False
) -> ChromeVisit:
    """Follow the issuer's navigation and check alternate listings for a verified report."""
    host = urlsplit(home).netloc
    warmup = urljoin(home, "/home/default.aspx") if q4 else home
    sec_intro = " ".join(sec_release[:2500].split()).casefold()
    fallback = ChromeVisit()
    try:
        async with chrome() as browser:

            async def get(url: str) -> Response | None:
                warm = warmup if urlsplit(url).netloc == host else url
                try:
                    return await browser.request(url, warm, 8, fetcher.wait_turn)
                except (BrowserError, aiohttp.ClientError, TimeoutError) as error:
                    log.info("Chrome couldn't read a page on %s: %r", urlsplit(url).netloc, error)
                    return None

            page = await get(home)
            page_url = home
            if q4 and page is not None and page.status == 0:
                canonical = await get(warmup)
                if canonical and canonical.status == 200:
                    page, page_url = canonical, warmup
            linked: list[str] = []
            if page is not None and page.status == 200:
                linked = _release_pages(page_url, page.body)
                if not linked:
                    investor = next(
                        (
                            link
                            for link, title in issuer_links(page_url, page.body)
                            if _IR_NAV.fullmatch(title.strip())
                        ),
                        None,
                    )
                    if investor:
                        landing = await get(investor)
                        if landing is not None and landing.status == 200:
                            linked = _release_pages(investor, landing.body)

            paths = ("/news/default.aspx", *_LISTINGS) if q4 else _LISTINGS
            candidates = [*linked[:2], *(urljoin(home, path) for path in paths)]
            opened = {home}
            for candidate in dict.fromkeys(candidates):
                if candidate in opened:
                    continue
                opened.add(candidate)
                listing = await get(candidate)
                if listing is None or listing.status != 200:
                    continue
                # Corporate sites may put the actual release list in an external iframe.
                delegated = next(
                    (url for url in _release_pages(candidate, listing.body) if url not in opened),
                    None,
                )
                pages = [(candidate, listing)]
                if delegated:
                    opened.add(delegated)
                    hosted = await get(delegated)
                    if hosted is not None and hosted.status == 200:
                        pages.insert(0, (delegated, hosted))
                for url, reply in pages:
                    links = issuer_links(url, reply.body)
                    if not links:
                        continue
                    visit = await _read_results(get, url, links, sec_intro)
                    if visit.title:
                        return visit
                    if fallback.listing is None:
                        fallback = visit
            if fallback.problem is None:
                fallback.problem = (
                    f"Chrome got {_answer(page)} from {host}"
                    if page is None or page.status != 200
                    else f"Chrome found no press release list on {host}"
                )
    except (BrowserError, *_ERRORS) as error:
        log.info("Chrome couldn't read %s: %r", home, error)
        if fallback.problem is None:
            fallback.problem = f"Chrome couldn't read {host} ({type(error).__name__})"
    return fallback


def posted_time(text: str) -> datetime | None:
    """When a release page says it was posted, from near its top."""
    match = _POSTED.search(text[:1500])
    if match is None:
        return None
    month, day, year, hour, minute, half = match.groups()
    if not 1 <= int(hour) <= 12:
        return None
    try:
        return datetime(
            int(year),
            _MONTH_NUMBERS[month[:3].lower()],
            int(day),
            int(hour) % 12 + (12 if half.upper() == "P" else 0),
            int(minute),
            tzinfo=ET,
        )
    except ValueError:
        return None


def _wire_checks(report: Report, found: str | None) -> None:
    for name in ("PR Newswire", "Business Wire", "GlobeNewswire"):
        source = f"{name} earnings feed"
        if found == name:
            trial = " On trial: recorded, not posted yet." if name == "GlobeNewswire" else ""
            report.watch(source, f"Its releases go out on {name}.{trial}")
        elif found:
            report.reject(source, f"Its releases go out on {found}")
        elif name == "GlobeNewswire":
            report.reject(source, "No release named its wire, and this feed is only watched then")
        else:
            report.watch(source, "No release named its wire, so both main wires are watched")


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
    hosts = ir_hosts(text, ticker, company.name)[:3]
    if not hosts and not _RSS_INDEX.search(text):
        report.reject(
            "IR site",
            "No IR site or issuer website identified in its last results release"
            if text
            else f"Couldn't read its last results {company.sec_form}",
        )
    for host in hosts:
        home = f"https://{host}/"
        try:
            browser_host(home)
        except ValueError:
            continue
        report.doing(f"Checking {host}")
        page = await _get(fetcher, home)
        blocked = page is not None and page.status in _BLOCKED
        if page is None or page.status != 200:
            report.reject(f"{host} homepage over HTTP", f"Returned {_answer(page)}")
        # A site can protect its HTML while its official Q4 feed serves the same releases.
        report.doing(f"Looking for a Q4 press release feed on {host}")
        q4, q4_wire, why = await _q4_wire(fetcher, host)
        if q4:
            report.watch(f"Q4 press release feed on {host}", "Lists its releases and files")
        else:
            report.reject(f"Q4 press release feed on {host}", why)
        if not q4 and not blocked and (page is None or page.status != 200):
            continue
        findings.site = host
        findings.blocked = blocked
        findings.q4 = q4
        findings.wire = findings.wire or q4_wire
        if not q4 and page is not None and page.status == 200:
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
            visit = await _through_chrome(fetcher, chrome, home, text, q4=q4)
            findings.wire = findings.wire or visit.wire
            # Kept even without readable results: date notices are read from it too
            findings.listing = visit.listing
            if visit.title and visit.listing:
                why = f"Chrome read its latest results, “{visit.title}”"
                if when := visit.posted:
                    why += f", posted {when:%b} {when.day} at {clock(when)}"
                report.watch(f"Press release page <{visit.listing}>, read in Chrome", why)
                findings.posted, findings.release_url = visit.posted, visit.url
            else:
                report.reject(f"{host} press release page", visit.problem or "Unread")
        if (
            findings.listing is None
            and page is not None
            and page.status == 200
            and not (findings.q4 or findings.feeds)
        ):
            # The list a plain request finds, for date notices
            findings.listing = next(iter(_release_pages(home, page.body)), None)
        if findings.listing or findings.q4 or findings.feeds:
            break
    if not findings.q4 and not findings.feeds:
        feed, found_wire = await _named_feed(fetcher, text, report)
        if feed:
            findings.feeds = [feed]
            findings.wire = findings.wire or found_wire
            report.watch(f"Press release feed <{feed}>", "Verified from the issuer's RSS index")
    if findings.listing:
        report.doing("Checking direct HTTP access to the press release listing")
        response = await _get(fetcher, findings.listing, allow_redirects=False)
        findings.ir_http_status = response.status if response else 0
    report.doing("Choosing its sources")
    _wire_checks(report, findings.wire)
    found_wire = findings.wire or "unknown"
    trial = company.trial
    if found_wire == "GlobeNewswire":
        # GlobeNewswire's earnings feed is still on trial for every company
        trial = trial.model_copy(update={"globenewswire": company.names})
    return company.model_copy(
        update={
            "platform": "q4"
            if findings.q4
            else "own-site"
            if (findings.site or findings.feeds)
            else "unknown",
            "q4_host": findings.site if findings.q4 else None,
            "reachable": findings.q4 or bool(findings.feeds),
            "rss": findings.feeds,
            "ir_listing": findings.listing,
            "ir_http_status": findings.ir_http_status,
            "ir_posted": findings.posted,
            "ir_release": findings.release_url,
            "wire": found_wire,
            "trial": trial,
            "evidence": findings.describe(today),
            "investigated": today,
            "investigation": INVESTIGATION,
            "checks": report.checks,
        }
    )
