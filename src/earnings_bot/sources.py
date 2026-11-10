"""Places a new earnings release can show up first, and how to read each of them.

A source turns one HTTP response into the items it currently lists. Whoever polls the source
compares those items with the ones it saw before to spot new ones.
"""

import itertools
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from earnings_bot.fetch import Fetcher, Response

BUSINESS_WIRE_EARNINGS = "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEF9YXA=="
# The latest 20 releases PR Newswire files under Earnings. Its CDN keeps a copy for up to 60 s
# and ignores query strings, so FreshPrNewswire uses equivalent encoded paths.
PR_NEWSWIRE_EARNINGS = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/earnings-list.rss"
)
# PR Newswire's Semiconductors category, another place Broadcom's releases are filed. Also
# cached for 60 s.
PR_NEWSWIRE_SEMICONDUCTORS = (
    "https://www.prnewswire.com/rss/business-technology-latest-news/semiconductors-list.rss"
)
# GlobeNewswire's Earnings Releases and Operating Results subject: 20 items, cached for 60 s.
# A cache-buster reaches its server, but on Nov 6, 2026 that server returned the same build
# as the cached copy, so whether busting gets news sooner is still unmeasured.
GLOBENEWSWIRE_EARNINGS = "https://rss.globenewswire.com/RssFeed/subjectcode/13"
# Broadcom's own newsroom, as JSON: every financial release, newest first (196 items and about
# 150 KB on Nov 6, 2026; it takes no paging parameters). Cloudflare served a copy 445 s old
# despite the no-cache headers, and a cache-buster got a fresh one.
BROADCOM_NEWSROOM = (
    "https://www.broadcom.com/api/news/newsroom?parent=bltf3a3dd90a030e144&source_mode=Both"
    "&category=Financial&locale=en-us&microsite=broadcom"
)
BROADCOM_RELEASE = "https://www.broadcom.com/api/getmetadata?url={path}&locale=en-us"

# Matches "Uber Announces Results for Third Quarter 2026" and "Palantir Reports Q2 2026 U.S. Comm
# Revenue Growth of 149%", but not the earlier "Uber Announces Date of Third Quarter 2026 Results
# Conference Call" or "Cloudflare Expands EMEA Operations with New Headquarters"
_EARNINGS = re.compile(r"results|earnings|\bquarter|\bQ[1-4]\b|\b[1-4]Q\d{2}\b", re.IGNORECASE)
_NOT_EARNINGS = re.compile(
    r"webcast|conference call|to host|to announce|schedule|\bdate\b", re.IGNORECASE
)


# Guessed URLs with these endings are files, not pages
_FILE_TYPES = (".pdf", ".xlsx", ".xls")


# A notice of when results come out: "Applied Materials to Report Fourth Quarter and Fiscal
# 2026 Results on Nov. 12, 2026"
_NOTICE = re.compile(
    r"\b(?:(?:sets?|announces?)\s+(?:(?:a|the)\s+)?(?:date|timing)|schedules?|"
    r"(?:will|to)\s+(?:report|release|announce|publish|issue|host)|"
    r"conference call|earnings call|webcast)\b",
    re.IGNORECASE,
)
_RESULTS_WORD = re.compile(r"\b(?:earnings|results)\b", re.IGNORECASE)
_QUARTER_WORD = re.compile(
    r"\b(?:q[1-4]|[1-4]q\d{2}|(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))"
    r"[-\s]+quarter|full[-\s]+year)\b",
    re.IGNORECASE,
)
_CALL_WORD = re.compile(r"\b(?:conference call|webcast)\b", re.IGNORECASE)


def is_date_notice(headline: str) -> bool:
    return bool(
        _NOTICE.search(headline)
        and (
            _RESULTS_WORD.search(headline)
            or (_QUARTER_WORD.search(headline) and _CALL_WORD.search(headline))
        )
    )


def looks_like_earnings(headline: str) -> bool:
    return (
        bool(_EARNINGS.search(headline))
        and not _NOT_EARNINGS.search(headline)
        and not is_date_notice(headline)
    )


def bust_cache(url: str) -> str:
    # A query parameter the CDN hasn't seen before makes it fetch a fresh copy
    return f"{url}{'&' if '?' in url else '?'}_={time.time_ns()}"


def published_time(stamp: str | None) -> datetime | None:
    """A source's timestamp as a moment in time, or None when it doesn't say which time zone
    it's in. RSS and HTTP dates and ISO times with an offset or Z qualify. Q4's site-local
    "08/05/2026 06:55:00" doesn't."""
    # Stamps come straight from feeds' JSON, which could hold a number or null
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.strip())
    except ValueError:
        try:
            moment = parsedate_to_datetime(stamp)
        except (TypeError, ValueError, IndexError):
            return None
    return moment if moment.tzinfo else None


@dataclass(frozen=True)
class Sighting:
    """One item a source lists: a press release, a filing or a file."""

    key: str  # unique within the source
    title: str
    url: str
    published: str | None = None  # the source's own timestamp, exactly as it gives it
    earnings: bool = False  # whether it looks like the earnings release itself
    details: dict[str, str] = field(default_factory=dict)
    # The page or file itself, when the poll already downloaded it, so nobody fetches it again
    body: bytes | None = field(default=None, repr=False, compare=False)
    # Already listed when the bot started watching, but published inside the window
    caught_up: bool = False


class Source(ABC):
    name: str
    interval: float  # seconds between polls
    # Seconds to wait for an answer before giving up and polling again. Every source normally
    # answers in under a second, but SEC's live feed takes 7 to 12 s or more over about 2% of
    # requests, and waiting on one blinds the source for as long as the wait. Asking again is
    # quicker.
    timeout = 3.0
    method = "GET"
    ok_statuses = frozenset({200})
    stop_when_found = False  # stop polling once the source lists anything
    # A source on trial is recorded but doesn't post to Discord unless the schedule asks
    trial = False
    report_initial = False  # the source can verify that this is the scheduled release

    async def fetch(self, fetcher: Fetcher) -> Response:
        return await fetcher.request(self.method, self.url(), give_up_after=self.timeout)

    @property
    def share_key(self) -> Hashable | None:
        """Watches whose sources have the same key share one poll; None never shares."""
        return None

    @abstractmethod
    def url(self) -> str: ...

    @abstractmethod
    def parse(self, response: Response) -> list[Sighting]:
        """Raises ValueError, KeyError or TypeError when the response isn't what we expect."""


_Q4_PRESS = (
    "/feed/PressRelease.svc/GetPressReleaseList?LanguageId=1&bodyType=0&pressReleaseDateFilter=3"
    "&categoryId=00000000-0000-0000-0000-000000000000&pageSize=5&pageNumber=0&tagList="
    "&includeTags=true&year=-1&excludeSelection=1"
)
_Q4_REPORTS = (
    "/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1&reportTypes=&pageSize=2"
    "&pageNumber=0&tagList=&includeTags=true&year=-1&excludeSelection=1"
)

# Q4's CDN keeps each feed URL's answer for 10 s. By Nov 5, 2026 its cache key counted only the
# feed's own query parameters, so a cache-buster gets the same copy as everyone else. A list of
# a different length is a different copy, though, so the press release list also asks for this
# many longer lists in turn, each once its copy has expired. Measured live on Nov 6, that cut
# the average wait for a new release from 5.2 s to 1.0 s. Each turn replaces a normal poll, so
# the IR site gets no more requests from us, and Q4's servers get about one every 2 s.
Q4_LONGER_LISTS = 5
Q4_REFRESH_SECONDS = 10.5  # just over the CDN's max-age=10, so the copy has always expired


class Q4PressReleases(Source):
    """The press release list on an IR site run by Q4 Inc. Q4's CDN can serve a copy up to 10
    seconds old, so polls take turns with longer lists (see Q4_LONGER_LISTS)."""

    name = "q4_press"
    interval = 1.0

    def __init__(
        self, host: str, page_size: int = 5, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.host = host
        self.page_size = page_size
        self.clock = clock
        self._asked: list[float] | None = None  # when each longer list was last asked for

    def url(self) -> str:
        # Q4's CDN ignores the buster, but Palantir's own CDN in front of it caches for an hour
        # and doesn't
        return bust_cache(f"https://{self.host}{self._path(self._next_page_size())}")

    def bodies_url(self, page_size: int) -> str:
        """The newest `page_size` releases, each with its full HTML, for the summary."""
        path = self._path(page_size).replace("bodyType=0", "bodyType=1")
        return bust_cache(f"https://{self.host}{path}")

    def _path(self, page_size: int) -> str:
        return _Q4_PRESS.replace("pageSize=5", f"pageSize={page_size}")

    def _next_page_size(self) -> int:
        """The longer list asked for longest ago, if its copy has expired, or else the normal
        one. Turns end up about Q4_REFRESH_SECONDS / Q4_LONGER_LISTS apart."""
        now = self.clock()
        if self._asked is None:
            # Spread the first turns evenly over one refresh, starting now
            step = Q4_REFRESH_SECONDS / Q4_LONGER_LISTS
            self._asked = [now - Q4_REFRESH_SECONDS + i * step for i in range(Q4_LONGER_LISTS)]
        oldest = min(range(Q4_LONGER_LISTS), key=self._asked.__getitem__)
        if now - self._asked[oldest] < Q4_REFRESH_SECONDS:
            return self.page_size
        self._asked[oldest] = now
        return self.page_size + 1 + oldest

    def parse(self, response: Response) -> list[Sighting]:
        # A longer list's extra items are older ones, which mustn't look new
        releases = json.loads(response.body)["GetPressReleaseListResult"][: self.page_size]
        return [
            Sighting(
                key=str(release["PressReleaseId"]),
                title=release["Headline"],
                url=urljoin(f"https://{self.host}/", release["LinkToDetailPage"]),
                published=release["PressReleaseDate"],  # site-local time, and not consistently
                earnings=looks_like_earnings(release["Headline"]),
            )
            for release in releases
        ]


class Q4FinancialReports(Source):
    """The files a Q4 IR site lists for each quarter: release, slides, supplemental data."""

    name = "q4_files"
    # It shares the IR host's one request a second with the press release list, and each poll
    # takes a turn from it. Q4's CDN keeps this feed for 10 s whatever we send, so asking more
    # often than that only gets the same copy again.
    interval = Q4_REFRESH_SECONDS

    def __init__(self, host: str) -> None:
        self.host = host

    def url(self) -> str:
        return bust_cache(f"https://{self.host}{_Q4_REPORTS}")

    def parse(self, response: Response) -> list[Sighting]:
        return [
            Sighting(
                key=document["DocumentPath"],
                title=f"{report['ReportTitle']}: {document['DocumentTitle']}",
                url=document["DocumentPath"],
                # A quarter's webcast link ("Online", not "File") can be listed before its
                # release, so only a file counts as the release
                earnings=looks_like_earnings(report["ReportTitle"])
                and document.get("DocumentType") != "Online",
            )
            for report in json.loads(response.body)["GetFinancialReportListResult"]
            for document in report["Documents"]
        ]


SecForm = Literal["8-K", "6-K"]


def _filing_type(form: str, items: str) -> tuple[bool, dict[str, str]]:
    if form == "6-K":
        return False, {"form": "6-K"}
    return form.startswith("8-K") and "2.02" in items.split(","), {}


class EdgarFilings(Source):
    """A company's latest SEC filings. Results 6-Ks require exhibit verification."""

    name = "edgar"
    # A backup: it listed every 8-K measured live 0 to 56 s after EDGAR's latest-filings feed
    # (EdgarLatest). All SEC polls share 8 requests a second, so on a busy evening each poll of
    # this one is a poll the faster feed doesn't get. In a replay of 20 releases at once,
    # asking every 3 s instead of every second brought the slowest SEC sighting from 7.4 s to
    # 6.0 s, and the slowest 8-K text from 7.6 s to 5.7 s.
    interval = 3.0

    def __init__(self, cik: int, limit: int = 20, *, form: SecForm = "8-K") -> None:
        self.cik = cik
        self.limit = limit  # newest filings to read; new ones always come first
        self.form = form

    def url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{self.cik:010d}.json"

    def parse(self, response: Response) -> list[Sighting]:
        recent = json.loads(response.body)["filings"]["recent"]
        columns = ("accessionNumber", "form", "items", "acceptanceDateTime", "primaryDocument")
        rows = zip(*(recent[column] for column in columns), strict=True)
        sightings = []
        for accession, form, items, accepted, document in itertools.islice(rows, self.limit):
            earnings, details = _filing_type(form, items)
            sightings.append(
                Sighting(
                    key=accession,
                    title=f"{form} {items}".strip(),
                    url=f"https://www.sec.gov/Archives/edgar/data/{self.cik}/"
                    f"{accession.replace('-', '')}/{document}",
                    published=accepted,  # UTC
                    earnings=earnings,
                    details=details,
                )
            )
        return sightings


class EdgarLatest(EdgarFilings):
    """EDGAR's latest-filings feed, narrowed to one company and form. It lists a filing the moment
    it goes public and is never cached, while the submissions JSON lagged it by 0 to 56 s on
    2026-11-03 (48 s for AMD's earnings 8-K)."""

    name = "edgar_latest"
    interval = 1.0

    def url(self) -> str:
        return (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&CIK={self.cik:010d}&type={self.form}&company=&dateb=&owner=include&start=0&count=10"
            "&output=atom"
        )

    def parse(self, response: Response) -> list[Sighting]:
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as error:
            raise ValueError(f"not valid XML: {error}") from error
        sightings = []
        for entry in root.iter(f"{_ATOM}entry"):
            # "<b>AccNo:</b> 0000002488-26-000188 ... Item 2.02: Results of Operations ..."
            summary = entry.findtext(f"{_ATOM}summary") or ""
            accession = re.search(r"\d{10}-\d{2}-\d{6}", entry.findtext(f"{_ATOM}id") or "")
            link = entry.find(f"{_ATOM}link")
            if accession is None or link is None:
                raise ValueError("an entry without an accession number or link")
            form = (entry.findtext(f"{_ATOM}title") or "").split(" - ", 1)[0]
            items = ",".join(re.findall(r"Item (\d+\.\d+)", summary))
            earnings, details = _filing_type(form, items)
            sightings.append(
                Sighting(
                    key=accession.group(0),
                    title=f"{form} {items}".strip(),
                    url=link.get("href", ""),  # the filing's index page
                    published=entry.findtext(f"{_ATOM}updated"),  # US Eastern, with offset
                    earnings=earnings,
                    details=details,
                )
            )
        return sightings


class RssFeed(Source):
    """An RSS feed, optionally narrowed to items that name one of the given companies."""

    interval = 2.0
    verify_channel = False

    def __init__(
        self, name: str, feed_url: str, companies: Sequence[str] = (), bust_cache: bool = False
    ) -> None:
        self.name = name
        self.feed_url = feed_url
        self.bust_cache = bust_cache
        names = "|".join(re.escape(_straight_quotes(company)) for company in companies)
        self._match = re.compile(rf"\b(?:{names})\b", re.IGNORECASE) if companies else None

    @property
    def share_key(self) -> Hashable:
        # Each watch narrows the feed to its own company when it parses the shared response
        return (type(self).__name__, self.name, self.feed_url, self.bust_cache)

    def url(self) -> str:
        return bust_cache(self.feed_url) if self.bust_cache else self.feed_url

    def parse(self, response: Response) -> list[Sighting]:
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as error:
            raise ValueError(f"not valid XML: {error}") from error
        channel = root.find("channel")
        if self.verify_channel and (channel is None or channel.findtext("link") != self.feed_url):
            raise ValueError("RSS response did not identify the requested category")
        if root.tag == f"{_ATOM}feed":  # Apple's newsroom feed, for one
            built = root.findtext(f"{_ATOM}updated")
            entries = [_atom_entry(entry) for entry in root.iter(f"{_ATOM}entry")]
        elif channel is not None:
            built = channel.findtext("lastBuildDate")
            entries = [_rss_item(item) for item in channel.iter("item")]
        else:
            raise ValueError("not an RSS or Atom feed")
        sightings = []
        for entry in entries:
            # Newswire descriptions open with the dateline and the company's name:
            # "IRVINE, Calif.--(BUSINESS WIRE)--Beach Cities Commercial Bank ...". PR Newswire
            # also names the company in each item's dc:contributor.
            text = _straight_quotes(f"{entry.title} {entry.contributor} {entry.lead[:150]}")
            if self._match and not self._match.search(text):
                continue
            sightings.append(
                Sighting(
                    key=entry.key,
                    title=entry.title,
                    url=entry.link,
                    published=entry.published,
                    earnings=looks_like_earnings(entry.title),
                    details={"feed_built": built} if built else {},
                )
            )
        return sightings


class FreshPrNewswire(RssFeed):
    """Rotate equivalent paths with two percent-encoded letters. PR Newswire decodes the path
    for category lookup, while its CDN caches each spelling separately. Changing letter case
    also misses the CDN but can silently return the general feed instead of the category."""

    verify_channel = True

    def __init__(
        self, name: str, feed_url: str, companies: Sequence[str] = (), *, interval: float = 1.0
    ) -> None:
        super().__init__(name, feed_url, companies)
        self.interval = interval
        self._parts = urlsplit(feed_url)
        # Keep /rss/ and .rss literal. Each of the two category feeds has over 700 spellings.
        path = self._parts.path
        letters = [i for i in range(len("/rss/"), len(path) - len(".rss")) if path[i].isalpha()]
        pairs = list(itertools.combinations(letters, 2))
        start = random.randrange(len(pairs))
        self._spellings = itertools.cycle(pairs[start:] + pairs[:start])

    def url(self) -> str:
        positions = next(self._spellings)
        path = "".join(
            f"%{ord(char):02X}" if i in positions else char
            for i, char in enumerate(self._parts.path)
        )
        return urlunsplit(self._parts._replace(path=path))

    async def fetch(self, fetcher: Fetcher) -> Response:
        return await fetcher.request(
            self.method,
            self.url(),
            give_up_after=self.timeout,
            encoded_url=True,
            allow_redirects=False,
        )


_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def _straight_quotes(text: str) -> str:
    # Headlines spell "McDonald's" with either apostrophe
    return text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")


@dataclass(frozen=True)
class _Entry:
    key: str
    title: str
    lead: str
    link: str
    published: str | None
    contributor: str = ""


def _rss_item(item: ET.Element) -> _Entry:
    link = (item.findtext("link") or "").strip()
    return _Entry(
        key=item.findtext("guid") or link,
        title=(item.findtext("title") or "").strip(),
        lead=item.findtext("description") or "",
        link=link,
        published=item.findtext("pubDate"),
        contributor=item.findtext(f"{_DC}contributor") or "",
    )


def _atom_entry(entry: ET.Element) -> _Entry:
    links = entry.findall(f"{_ATOM}link")
    page = next((ln for ln in links if ln.get("rel", "alternate") == "alternate"), None)
    link = page.get("href", "") if page is not None else ""
    return _Entry(
        key=entry.findtext(f"{_ATOM}id") or link,
        title=(entry.findtext(f"{_ATOM}title") or "").strip(),
        lead=entry.findtext(f"{_ATOM}content") or entry.findtext(f"{_ATOM}summary") or "",
        link=link,
        published=entry.findtext(f"{_ATOM}updated"),
    )


class GuessedFile(Source):
    """A URL we expect the release or one of its files to use, probed until it exists."""

    method = "HEAD"
    interval = 1.0
    ok_statuses = frozenset({200, 404})  # 404 just means it isn't up yet
    stop_when_found = True

    def __init__(self, file_url: str) -> None:
        self.file_url = file_url
        self.name = "guess:" + urlsplit(file_url).path.rstrip("/").rsplit("/", 1)[-1]

    def url(self) -> str:
        # Some sites (Apple's newsroom) cache a 404 for minutes, which would hide the page after
        # it goes up. The file CDN doesn't, but busting costs nothing until the file exists.
        return bust_cache(self.file_url)

    def parse(self, response: Response) -> list[Sighting]:
        if response.status != 200:
            return []
        # Some sites answer 200 with a "Page Not Found" page (UnitedHealth's does), so a file that
        # comes back as a web page isn't there yet
        is_file = urlsplit(self.file_url).path.lower().endswith(_FILE_TYPES)
        if is_file and response.headers.get("content-type", "").startswith("text/html"):
            return []
        return [
            Sighting(
                key=self.file_url,
                title=self.name.removeprefix("guess:"),
                url=self.file_url,
                published=response.headers.get("last-modified"),
                earnings=True,
                details={"size": response.headers.get("content-length", "")},
            )
        ]


class GuessedPage(GuessedFile):
    """A page (or file) the release is expected at, fetched whole on every poll, so that once it
    exists the poll that found it already has the release. For hosts with plain 404s and small
    404 pages: CrowdStrike's copy on FinancialContent, McDonald's MediaRoom, Airbnb's newsroom."""

    method = "GET"

    def __init__(self, page_url: str) -> None:
        super().__init__(page_url)
        self.name = "page:" + (urlsplit(page_url).hostname or "")

    def parse(self, response: Response) -> list[Sighting]:
        if response.status != 200:
            return []
        is_pdf = urlsplit(self.file_url).path.lower().endswith(".pdf")
        content_type = response.headers.get("content-type", "")
        if (is_pdf and not response.body.startswith(b"%PDF-")) or (
            not is_pdf and not content_type.startswith("text/html")
        ):
            return []  # an error page in place of the file, or a file in place of the page
        return [
            Sighting(
                key=self.file_url,
                title=_page_title(response.body) or self.file_url,
                url=self.file_url,
                published=response.headers.get("last-modified"),
                earnings=True,
                details={"size": str(len(response.body))},
                body=response.body,
            )
        ]


def _page_title(body: bytes) -> str:
    match = re.search(rb"<title[^>]*>(.*?)</title>", body[:200_000], re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    return re.sub(r"\s+", " ", match.group(1).decode(errors="replace")).strip()[:200]


class BroadcomNewsroom(Source):
    """Broadcom's newsroom JSON (see BROADCOM_NEWSROOM). Each release's full text comes from a
    second call, BROADCOM_RELEASE."""

    name = "broadcom_newsroom"
    interval = 1.0

    @property
    def share_key(self) -> Hashable:
        return self.name

    def url(self) -> str:
        return bust_cache(BROADCOM_NEWSROOM)

    def parse(self, response: Response) -> list[Sighting]:
        releases = json.loads(response.body)
        if not isinstance(releases, list):
            raise ValueError("not a list of releases")
        sightings = []
        for release in releases:
            title = str(release["title"]).strip()
            sightings.append(
                Sighting(
                    key=str(release["content_id"]),
                    title=title,
                    url=urljoin("https://www.broadcom.com/", release["url"]),
                    # UTC, and the release's real time. The detail's own date is 7 hours off.
                    published=release.get("publish_date"),
                    earnings=looks_like_earnings(title),
                    details={"path": release["url"]},
                )
            )
        return sightings
