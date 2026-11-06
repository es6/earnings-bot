"""Places a new earnings release can show up first, and how to read each of them.

A source turns one HTTP response into the items it currently lists. Whoever polls the source
compares those items with the ones it saw before to spot new ones.
"""

import itertools
import json
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from earnings_bot.fetch import Response

BUSINESS_WIRE_EARNINGS = "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEF9YXA=="
# The latest 20 releases PR Newswire files under Earnings. Its CDN keeps a copy for up to 60 s
# and ignores cache-busting parameters, so there's no busted variant to poll.
PR_NEWSWIRE_EARNINGS = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/earnings-list.rss"
)

# Matches "Uber Announces Results for Third Quarter 2026" and "Palantir Reports Q2 2026 U.S. Comm
# Revenue Growth of 149%", but not the earlier "Uber Announces Date of Third Quarter 2026 Results
# Conference Call" or "Cloudflare Expands EMEA Operations with New Headquarters"
_EARNINGS = re.compile(r"results|earnings|\bquarter|\bQ[1-4]\b|\b[1-4]Q\d{2}\b", re.IGNORECASE)
_NOT_EARNINGS = re.compile(
    r"webcast|conference call|to host|to announce|schedule|\bdate\b", re.IGNORECASE
)


# Guessed URLs with these endings are files, not pages
_FILE_TYPES = (".pdf", ".xlsx", ".xls")


def looks_like_earnings(headline: str) -> bool:
    return bool(_EARNINGS.search(headline)) and not _NOT_EARNINGS.search(headline)


def _bust_cache(url: str) -> str:
    # A query parameter the CDN hasn't seen before makes it fetch a fresh copy
    return f"{url}{'&' if '?' in url else '?'}_={time.time_ns()}"


@dataclass(frozen=True)
class Sighting:
    """One item a source lists: a press release, a filing or a file."""

    key: str  # unique within the source
    title: str
    url: str
    published: str | None = None  # the source's own timestamp, exactly as it gives it
    earnings: bool = False  # whether it looks like the earnings release itself
    details: dict[str, str] = field(default_factory=dict)


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

    def url(self, with_bodies: bool = False) -> str:
        if with_bodies:
            # Each release comes with its full HTML, for the summary
            path = self._path(self.page_size).replace("bodyType=0", "bodyType=1")
        else:
            path = self._path(self._next_page_size())
        # Q4's CDN ignores the buster, but Palantir's own CDN in front of it caches for an hour
        # and doesn't
        return _bust_cache(f"https://{self.host}{path}")

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
    interval = 5.0  # shares the IR host's one request a second with the press release list

    def __init__(self, host: str) -> None:
        self.host = host

    def url(self) -> str:
        return _bust_cache(f"https://{self.host}{_Q4_REPORTS}")

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


class EdgarFilings(Source):
    """A company's latest SEC filings. Earnings 8-Ks report item 2.02."""

    name = "edgar"
    interval = 1.0

    def __init__(self, cik: int, limit: int = 20) -> None:
        self.cik = cik
        self.limit = limit  # newest filings to read; new ones always come first

    def url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{self.cik:010d}.json"

    def parse(self, response: Response) -> list[Sighting]:
        recent = json.loads(response.body)["filings"]["recent"]
        columns = ("accessionNumber", "form", "items", "acceptanceDateTime", "primaryDocument")
        rows = zip(*(recent[column] for column in columns), strict=True)
        return [
            Sighting(
                key=accession,
                title=f"{form} {items}".strip(),
                url=f"https://www.sec.gov/Archives/edgar/data/{self.cik}/"
                f"{accession.replace('-', '')}/{document}",
                published=accepted,  # UTC
                earnings=form.startswith("8-K") and "2.02" in items,
            )
            for accession, form, items, accepted, document in itertools.islice(rows, self.limit)
        ]


class EdgarLatest(EdgarFilings):
    """EDGAR's latest-filings feed, narrowed to one company's 8-Ks. It lists a filing the moment
    it goes public and is never cached, while the submissions JSON lagged it by 0 to 56 s on
    2026-11-03 (48 s for AMD's earnings 8-K)."""

    name = "edgar_latest"

    def url(self) -> str:
        return (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&CIK={self.cik:010d}&type=8-K&company=&dateb=&owner=include&start=0&count=10"
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
            sightings.append(
                Sighting(
                    key=accession.group(0),
                    title=f"{form} {items}".strip(),
                    url=link.get("href", ""),  # the filing's index page
                    published=entry.findtext(f"{_ATOM}updated"),  # US Eastern, with offset
                    earnings=form.startswith("8-K") and "2.02" in items,
                )
            )
        return sightings


class RssFeed(Source):
    """An RSS feed, optionally narrowed to items that name one of the given companies."""

    interval = 2.0

    def __init__(
        self, name: str, feed_url: str, companies: Sequence[str] = (), bust_cache: bool = False
    ) -> None:
        self.name = name
        self.feed_url = feed_url
        self.bust_cache = bust_cache
        names = "|".join(re.escape(_straight_quotes(company)) for company in companies)
        self._match = re.compile(rf"\b(?:{names})\b", re.IGNORECASE) if companies else None

    def url(self) -> str:
        return _bust_cache(self.feed_url) if self.bust_cache else self.feed_url

    def parse(self, response: Response) -> list[Sighting]:
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as error:
            raise ValueError(f"not valid XML: {error}") from error
        if root.tag == f"{_ATOM}feed":  # Apple's newsroom feed, for one
            built = root.findtext(f"{_ATOM}updated")
            entries = [_atom_entry(entry) for entry in root.iter(f"{_ATOM}entry")]
        elif (channel := root.find("channel")) is not None:
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
        return _bust_cache(self.file_url)

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
