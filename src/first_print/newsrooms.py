"""Independent official newsrooms, verified by the article rather than a dated slug."""

import re
import time
from dataclasses import replace
from urllib.parse import urldefrag, urljoin, urlsplit

from pydantic import BaseModel, Field, model_validator

from first_print.browser import browser_host
from first_print.browser_sources import HttpRelease, _ReleaseLinks
from first_print.fetch import Fetcher, Response
from first_print.sec_results import announces_results, reported_period
from first_print.sources import Sighting, is_date_notice, issuer_wire_name
from first_print.text import html_to_text, verified_report

_QUARTER = re.compile(r"\b(?:Q[1-4]|[1-4]Q|(?:first|second|third|fourth) quarter)\b", re.IGNORECASE)
_FINANCIAL = re.compile(r"\b(?:revenues?|results|earnings)\b", re.IGNORECASE)


def _named(text: str, names: list[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) for name in names)


def newsroom_story(
    title: str, body: bytes, names: list[str], period: str | None = None
) -> tuple[str, str] | None:
    """Require the official article's own announcement, issuer, year and quarter.

    A listing headline may omit the year or lead with revenue rather than 'results'.
    Navigation before that headline is not the story's introduction.
    """
    if not (_QUARTER.search(title) and _FINANCIAL.search(title)) or is_date_notice(title):
        return None
    lines = html_to_text(body.decode("utf-8", "replace")).splitlines()
    normalized = " ".join(title.split()).casefold()
    start = next(
        (i for i, line in enumerate(lines) if " ".join(line.split()).casefold() == normalized),
        None,
    )
    if start is None:
        return None
    story = "\n".join(lines[start:])
    introduction = "\n".join(lines[start : start + 12])
    named = reported_period(introduction)
    if not named or not _named("\n".join(lines[start : start + 3]), names):
        return None
    actual = f"FY{named[0]}Q{named[1]}"
    if period is not None and actual != period:
        return None
    if not announces_results(story, actual):
        return None
    return story, actual


class Newsroom(BaseModel):
    listing: str
    names: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def official_route(self) -> "Newsroom":
        browser_host(self.listing)
        self.names = list(dict.fromkeys(issuer_wire_name(n).strip() for n in self.names))
        if not all(self.names):
            raise ValueError("An official newsroom needs nonempty issuer names")
        return self

    def source(self, period: str | None) -> "NewsroomRelease":
        return NewsroomRelease(self.listing, self.names, period)


class NewsroomRelease(HttpRelease):
    """Use the normal host budget and refusal backoff; inspect only a bounded set of links."""

    interval = 2.0

    def __init__(self, listing: str, names: list[str], period: str | None) -> None:
        super().__init__(listing, ".*", chrome_fallback=False)
        self.name = f"newsroom:{listing}"
        self.names, self.period = names, period

    async def fetch(self, fetcher: Fetcher) -> Response:
        self._candidate = None
        listing = await self.request_page(fetcher, self.listing)
        self.listing_poll = listing, time.monotonic()
        self.listing_transport = self.transport
        self.listing_diagnostics = self.request_diagnostics.copy()
        if listing.status != 200:
            return listing
        parser = _ReleaseLinks()
        parser.feed(listing.body.decode("utf-8", "replace"))
        seen: set[str] = set()
        for href, title in [*parser.links, *parser.structured]:
            if (
                not (_QUARTER.search(title) and _FINANCIAL.search(title))
                or is_date_notice(title)
                or not _named(title, self.names)
            ):
                continue
            url = urldefrag(urljoin(self.listing, href)).url
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.netloc != self._host or url == self.listing:
                continue
            if url in seen:
                continue
            seen.add(url)
            response = await self.request_page(fetcher, url, urgent=True)
            if response.status in (403, 429):
                return response
            story = (
                newsroom_story(title, response.body, self.names, self.period)
                if response.status == 200 and response.url == url
                else None
            )
            if story:
                self._candidate = Sighting(
                    url,
                    title,
                    url,
                    earnings=True,
                    details={"period": story[1], "transport": self.transport},
                )
                return response
            if len(seen) >= 3:
                break
        return listing

    def parse(self, response: Response) -> list[Sighting]:
        candidate = self._candidate
        if candidate is None or response.status != 200 or response.url != candidate.url:
            return []
        story = newsroom_story(candidate.title, response.body, self.names, self.period)
        if story is None:
            return []
        kind = "report" if verified_report(story[0]) else "highlights"
        return [
            replace(
                candidate, body=response.body, details={**candidate.details, "content_kind": kind}
            )
        ]
