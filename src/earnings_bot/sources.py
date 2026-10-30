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
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from earnings_bot.fetch import Response

BUSINESS_WIRE_EARNINGS = "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEF9YXA=="

# Matches "Uber Announces Results for Third Quarter 2026" and "Palantir Reports Q2 2026 U.S. Comm
# Revenue Growth of 149%", but not the earlier "Uber Announces Date of Third Quarter 2026 Results
# Conference Call"
_EARNINGS = re.compile(r"results|earnings|quarter|\bQ[1-4]\b|\b[1-4]Q\d{2}\b", re.IGNORECASE)
_NOT_EARNINGS = re.compile(
    r"webcast|conference call|to host|to announce|schedule|\bdate\b", re.IGNORECASE
)


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


class Q4PressReleases(Source):
    """The press release list on an IR site run by Q4 Inc. Its CDN can serve a copy up to 10
    seconds old, so every request busts the cache."""

    name = "q4_press"
    interval = 1.0

    def __init__(self, host: str) -> None:
        self.host = host

    def url(self) -> str:
        return _bust_cache(f"https://{self.host}{_Q4_PRESS}")

    def parse(self, response: Response) -> list[Sighting]:
        return [
            Sighting(
                key=str(release["PressReleaseId"]),
                title=release["Headline"],
                url=urljoin(f"https://{self.host}/", release["LinkToDetailPage"]),
                published=release["PressReleaseDate"],  # site-local time, and not consistently
                earnings=looks_like_earnings(release["Headline"]),
            )
            for release in json.loads(response.body)["GetPressReleaseListResult"]
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
                earnings=looks_like_earnings(report["ReportTitle"]),
            )
            for report in json.loads(response.body)["GetFinancialReportListResult"]
            for document in report["Documents"]
        ]


class EdgarFilings(Source):
    """A company's latest SEC filings. Earnings 8-Ks report item 2.02."""

    name = "edgar"
    interval = 1.0

    def __init__(self, cik: int) -> None:
        self.cik = cik

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
            for accession, form, items, accepted, document in itertools.islice(rows, 20)
        ]


class RssFeed(Source):
    """An RSS feed, optionally narrowed to items that name one of the given companies."""

    interval = 2.0

    def __init__(
        self, name: str, feed_url: str, companies: Sequence[str] = (), bust_cache: bool = False
    ) -> None:
        self.name = name
        self.feed_url = feed_url
        self.bust_cache = bust_cache
        names = "|".join(re.escape(company) for company in companies)
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
            # "IRVINE, Calif.--(BUSINESS WIRE)--Beach Cities Commercial Bank ..."
            if self._match and not self._match.search(f"{entry.title} {entry.lead[:150]}"):
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


@dataclass(frozen=True)
class _Entry:
    key: str
    title: str
    lead: str
    link: str
    published: str | None


def _rss_item(item: ET.Element) -> _Entry:
    link = (item.findtext("link") or "").strip()
    return _Entry(
        key=item.findtext("guid") or link,
        title=(item.findtext("title") or "").strip(),
        lead=item.findtext("description") or "",
        link=link,
        published=item.findtext("pubDate"),
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
