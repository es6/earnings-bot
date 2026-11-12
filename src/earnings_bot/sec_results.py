"""Verify a 6-K's results exhibit before it can become an earnings sighting. An 8-K filed under
Other Events (Item 8.01) gets the same check."""

import asyncio
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import aiohttp

from earnings_bot.fetch import Fetcher, Response
from earnings_bot.periods import title_period
from earnings_bot.sources import EdgarFilings, Sighting, needs_exhibit_check, published_time
from earnings_bot.text import filing_exhibits, html_to_text, verified_report

_PERIOD = re.compile(
    r"\b(?:q[1-4]|[1-4]q\s?(?:20)?\d{2}|quarter|full[- ]year|half[- ]year|fiscal year|"
    r"(?:three|six|nine|twelve) months ended|(?:year|half) ended|annual results)\b",
    re.IGNORECASE,
)
_ANNOUNCES_RESULTS = re.compile(
    # "is pleased to announce the unaudited consolidated quarterly and first half results", in
    # the Hong Kong exchange's format that dual-listed issuers such as WeRide also file
    r"\b(?:report(?:s|ed)?|announc(?:e|es|ed)|releas(?:es|ed)|publish(?:es|ed)|post(?:s|ed))"
    r"\b.{0,110}\b(?:financial results|(?:quarterly|interim|annual)(?: and [a-z -]{1,20})? "
    r"results|results for|earnings|net sales)\b"
    # Some releases use "today reported second quarter 2026 results". Require the
    # announcement to be current so a mention of older results doesn't qualify.
    # "CGI reports third quarter Fiscal 2026 results"
    # Present tense only: "previously reported second quarter results" is about older ones
    r"|\b(?:reports?|announces?) (?:its )?(?:unaudited )?"
    r"(?:q[1-4]|[1-4]q|first|second|third|fourth|full[- ]year|half[- ]year|fiscal)\b"
    r"[^.|]{0,60}?\bresults\b"
    r"|\btoday (?:reported|announced|released|published|posted) (?:its )?"
    r"(?:q[1-4]|(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))[- ]quarter|"
    r"(?:full|half)[- ]year)(?: (?:fiscal )?20\d{2})? results\b",
    re.IGNORECASE,
)
_NOTICE = re.compile(
    # "The Company is pleased to announce the ... results" is the results themselves
    r"\b(?:(?<!pleased )(?<!happy )(?<!delighted )to (?:host|report|announce|release|publish)|"
    r"will (?:report|announce|release|publish)|"
    r"(?:announces?|sets?) (?:the )?date|results conference call|earnings webcast)\b",
    re.IGNORECASE,
)
_FIGURE = re.compile(
    r"(?:[$€£¥][\s|]*[(-]?\d|\d[\d.,]*\s*(?:%|thousand\b|million\b|billion\b|crore\b))",
    re.IGNORECASE,
)
_REVENUE_ROW = re.compile(
    r"^\s*(?:total\s+)?(?:revenues?|net sales)\s*\|\s*\d[\d,.]*\s*\|\s*\d[\d,.]*",
    re.IGNORECASE | re.MULTILINE,
)
_ARCHIVE = re.compile(r"https://www\.sec\.gov/Archives/edgar/data/\d+/(\d{18})/[^/?#]+")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_COVER_END = re.compile(r"^Yes\s*(?:[☐☑☒]|\([xX ]\))?\s+No\s*(?:[☐☑☒]|\([xX ]\))?$", re.IGNORECASE)
_RESULTS = re.compile(r"\b(?:results|earnings)\b", re.IGNORECASE)
_HEADLINE = 120  # characters at most in a headline line
# Results still to come: "Acme to Report Third Quarter 2026 Results on November 12"
_FUTURE = re.compile(r"\b(?:to|will)\s+$", re.IGNORECASE)
HISTORY_LIMIT = 36
INTRODUCTION = 5000  # characters in which a results 6-K must announce its results


class SixKUnavailable(Exception):
    """A filing or exhibit could not be read yet, so its verdict must be retried."""


@dataclass(frozen=True)
class SixKNotice:
    headline: str
    text: str
    url: str


@dataclass(frozen=True)
class _Verdict:
    body: bytes | None = None
    notice: SixKNotice | None = None


def is_results_release(text: str, description: str) -> bool:
    """Require a results announcement near the front and actual financial statements."""
    first_lines = text.splitlines()[:12]
    # Room for the disclaimers a Hong Kong exchange announcement opens with, ahead of the
    # press release it carries
    introduction = " ".join(" ".join(first_lines).split())[:INTRODUCTION]
    if _NOTICE.search(description) or any(_NOTICE.search(line) for line in first_lines[:4]):
        return False
    for announcement in _ANNOUNCES_RESULTS.finditer(introduction):
        context = introduction[max(0, announcement.start() - 75) : announcement.end() + 160]
        if (
            _PERIOD.search(context)
            and verified_report(text)
            and (_FIGURE.search(text) or _REVENUE_ROW.search(text))
        ):
            return True
    return False


def reported_period(text: str) -> tuple[int, int] | None:
    """The fiscal year and quarter a results release says it reports, from where it announces
    them: "The Home Depot Announces Third Quarter Fiscal 2025 Results", Target's "today announced
    its first quarter 2026 financial results" below a headline that gives no year, or a headline
    line of results alone, as Ollie's "Second Quarter Fiscal 2026 Results" below its name. Only
    the words just after an announcement count, so Target's earlier "compared with Q1 2025" and
    the "second quarter of 2025" at the end of Lowe's dateline don't."""
    for line in text[:INTRODUCTION].splitlines():
        claims = [
            line[announcement.start() : announcement.end() + 80]
            for announcement in _ANNOUNCES_RESULTS.finditer(line)
            if not _FUTURE.search(line[: announcement.start()])
        ]
        if len(line) <= _HEADLINE and _RESULTS.search(line) and not _NOTICE.search(line):
            claims.append(line)
        for claim in claims:
            if period := title_period(claim):
                return period
    return None


def _after_cover(text: str) -> str:
    lines = text.splitlines()
    if any(re.fullmatch(r"FORM\s+6-K", line, re.IGNORECASE) for line in lines[:10]):
        for number, line in enumerate(lines[:40]):
            if _COVER_END.fullmatch(line):
                return "\n".join(lines[number + 1 :])
    return text


class SixKResults:
    """Share positive and ordinary-filing verdicts across both SEC watchers and research."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._verdicts: OrderedDict[str, _Verdict] = OrderedDict()
        self._locks = [asyncio.Lock() for _ in range(64)]

    async def verify(self, filing: Sighting) -> Sighting | None:
        if not needs_exhibit_check(filing.details):
            return None
        if body := (await self._verdict(filing)).body:
            title = next(
                (
                    line
                    for line in body[:INTRODUCTION].decode(errors="replace").splitlines()[:12]
                    if _ANNOUNCES_RESULTS.search(line) and title_period(line)
                ),
                None,
            )
            details = {**filing.details, "verified_exhibit": "true"}
            if title:
                details["results_headline"] = title
            form = filing.details.get("form", "6-K")
            return replace(
                filing,
                title=f"{form} financial results",
                earnings=True,
                details=details,
                body=body,
            )
        return None

    async def _verdict(self, filing: Sighting) -> _Verdict:
        lock = self._locks[hash(filing.key) % len(self._locks)]
        async with lock:
            if filing.key not in self._verdicts:
                self._verdicts[filing.key] = await self._read_filing(filing)
                if len(self._verdicts) > 256:
                    self._verdicts.popitem(last=False)
            self._verdicts.move_to_end(filing.key)
            return self._verdicts[filing.key]

    async def _read_filing(self, filing: Sighting) -> _Verdict:
        archive = _ARCHIVE.fullmatch(filing.url)
        if (
            not archive
            or not _ACCESSION.fullmatch(filing.key)
            or archive[1] != filing.key.replace("-", "")
        ):
            return _Verdict()
        folder = filing.url.rsplit("/", 1)[0]
        index = f"{folder}/{filing.key}-index"
        reply = await self._get(f"{index}.htm")
        if reply.status == 404:
            reply = await self._get(f"{index}.html")
        if reply.status != 200 or b"Document Format Files" not in reply.body:
            raise SixKUnavailable(f"filing index unavailable (HTTP {reply.status})")
        exhibits = filing_exhibits(reply.body.decode(errors="replace"))
        if not exhibits:
            if filing.details.get("form") != "6-K":
                return _Verdict()  # An 8-K's own document is the cover, not the release
            # Some issuers, such as Nu Holdings, put the release in the 6-K document itself
            exhibits = [{"name": filing.url.rsplit("/", 1)[1], "description": "6-K"}]
        # A press release is usually EX-99.1. Check other HTML exhibits when it is elsewhere.
        notice = None
        for exhibit in exhibits:
            description = exhibit["description"]
            url = f"{folder}/{exhibit['name']}"
            result = await self._get(url)
            if result.status != 200:
                raise SixKUnavailable(f"results exhibit unavailable (HTTP {result.status})")
            text = await asyncio.to_thread(html_to_text, result.body.decode(errors="replace"))
            if description == "6-K":
                text = _after_cover(text)
            if is_results_release(text, description):
                return _Verdict(body=text.encode())
            for line in text.splitlines()[:8]:
                if _PERIOD.search(line) and _NOTICE.search(line):
                    notice = SixKNotice(line, text, url)
                    break
        return _Verdict(notice=notice)

    async def _get(self, url: str) -> Response:
        try:
            return await self.fetcher.request("GET", url, urgent=True, give_up_after=4)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise SixKUnavailable(type(error).__name__) from error

    async def history(
        self, cik: int, now: datetime, count: int = 3, filings: list[Sighting] | None = None
    ) -> list[Sighting]:
        """Recent verified results, with bounded reads even for high-volume issuers."""
        candidates = await self._recent(cik, now, 380, filings)
        results: list[Sighting] = []
        for item in candidates[:HISTORY_LIMIT]:
            if result := await self.verify(item):
                results.append(result)
                if len(results) == count:
                    break
        return results

    async def notices(self, cik: int, now: datetime) -> AsyncIterator[SixKNotice]:
        """Look for the issuer's own date announcement in recent SEC exhibits."""
        for filing in (await self._recent(cik, now, 90))[:18]:
            if notice := (await self._verdict(filing)).notice:
                yield notice

    async def _recent(
        self, cik: int, now: datetime, days: int, filings: list[Sighting] | None = None
    ) -> list[Sighting]:
        if filings is None:
            source = EdgarFilings(cik, limit=500, form="6-K")
            reply = await self._get(source.url())
            if reply.status != 200:
                raise SixKUnavailable(f"submissions unavailable (HTTP {reply.status})")
            filings = source.parse(reply)
        oldest = now - timedelta(days=days)
        return [
            item
            for item in filings
            if item.details.get("form") == "6-K"
            and (at := published_time(item.published)) is not None
            and oldest <= at <= now
        ]
