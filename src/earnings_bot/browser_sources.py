"""Issuer listings and PDFs read through desktop Chrome."""

import re
import time
from dataclasses import replace
from html.parser import HTMLParser
from typing import Self
from urllib.parse import urldefrag, urljoin, urlsplit

from pydantic import BaseModel, model_validator

from earnings_bot.browser import browser_host
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.sources import (
    GuessedFile,
    RssFeed,
    Sighting,
    Source,
    looks_like_earnings,
    published_time,
)
from earnings_bot.text import MIN_RELEASE_CHARS, html_to_text, verified_report

_TESLA_AGENT = "earnings-bot/0.1 (public earnings release monitor)"
_TESLA_PATH = re.compile(r"/tesla-contents/IR/TSLA-Q[1-4]-\d{4}-Update\.pdf")


def _check_pdf(url: str, warmup: str) -> None:
    if browser_host(url) != browser_host(warmup) or not urlsplit(url).path.lower().endswith(
        (".pdf", "/pdf")
    ):
        raise ValueError("Each browser PDF must point to a PDF on its warmup page's host")


def _check_tesla(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "assets-ir.tesla.com"
        or not _TESLA_PATH.fullmatch(parts.path)
    ):
        raise ValueError("Tesla deck URLs must point to an official quarterly update")


class IRSource(BaseModel):
    """A schedule must explicitly specify the current release or exact file candidates."""

    listing: str | None = None
    rss: str | None = None
    headline: str | None = None
    verify_text: bool = False
    warmup: str | None = None
    pdfs: list[str] = []
    tesla_deck: str | None = None

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        if bool(self.listing or self.rss) != bool(self.headline):
            raise ValueError(
                "An IR listing or feed requires a headline regex for the scheduled quarter"
            )
        if self.listing:
            browser_host(self.listing)
        if self.headline:
            try:
                re.compile(self.headline)
            except re.error as error:
                raise ValueError(f"Invalid earnings headline regex: {error}") from error
        if self.warmup:
            browser_host(self.warmup)
        if self.rss and browser_host(self.rss) != browser_host(self._warmup() or self.rss):
            raise ValueError("The IR feed must stay on its warmup page's host")
        if self.pdfs:
            warmup = self._warmup()
            if not warmup:
                raise ValueError("Browser PDFs require a same-host warmup page")
            for url in self.pdfs:
                _check_pdf(url, warmup)
        if self.tesla_deck:
            _check_tesla(self.tesla_deck)
        if not (self.listing or self.rss or self.pdfs or self.tesla_deck):
            raise ValueError("An IR source must have a listing, feed or at least one PDF")
        return self

    def _warmup(self) -> str | None:
        return self.warmup or self.listing or (urljoin(self.rss, "/") if self.rss else None)

    def sources(self) -> list[Source]:
        sources: list[Source] = []
        if self.listing and self.headline:
            sources.append(
                BrowserRelease(self.listing, self.headline, verify_text=self.verify_text)
            )
        warmup = self._warmup()
        if warmup:
            if self.rss and self.headline:
                sources.append(BrowserRss(self.rss, warmup, self.headline))
                if self.listing and browser_host(self.listing) == browser_host(self.rss):
                    # Both discovery routes share this host's one-request-per-second budget.
                    for source in sources:
                        source.interval = 2.0
            sources.extend(BrowserPDF(url, warmup) for url in self.pdfs)
        if self.tesla_deck:
            sources.append(TeslaDeck(self.tesla_deck))
        return sources


class _ReleaseLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text.clear()

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


class BrowserRelease(Source):
    """Read the result article after a scheduled-quarter link appears on the listing."""

    name = "browser-ir"
    interval = 1.0
    report_initial = True
    stop_when_found = True
    ok_statuses = frozenset({200, 404})

    def __init__(self, listing: str, headline: str, *, verify_text: bool = False) -> None:
        self.listing = listing
        self.verify_text = verify_text
        self._host = browser_host(listing)
        self._headline = re.compile(headline, re.IGNORECASE)
        self._candidate: Sighting | None = None
        self.listing_poll: tuple[Response, float] | None = None

    @property
    def matched_listing(self) -> bool:
        return self._candidate is not None

    def url(self) -> str:
        return self.listing

    async def fetch(self, fetcher: Fetcher) -> Response:
        self._candidate = None
        self.listing_poll = None
        listing = await fetcher.browser_request(self.listing, self.listing, self.timeout)
        self.listing_poll = replace(listing, body=b""), time.monotonic()
        if listing.status != 200:
            return listing
        parser = _ReleaseLinks()
        parser.feed(listing.body.decode(errors="replace"))
        for href, headline in parser.links:
            if not self._headline.fullmatch(headline) or not looks_like_earnings(headline):
                continue
            url = urldefrag(urljoin(self.listing, href)).url
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.netloc != self._host or url == self.listing:
                continue
            self._candidate = Sighting(
                key=url,
                title=headline,
                url=url,
                earnings=True,
                details={"listing_ms": str(round(listing.elapsed * 1000))},
            )
            return await fetcher.browser_request(url, self.listing, self.timeout, urgent=True)
        return listing

    def parse(self, response: Response) -> list[Sighting]:
        candidate = self._candidate
        if candidate is None or response.status != 200 or response.url != candidate.url:
            return []
        if not article_matches(candidate.title, response.body, verify_text=self.verify_text):
            return []
        return [replace(candidate, body=response.body)]


class BrowserRss(Source):
    """Read an issuer's release feed in one Chrome request. The article can follow the alert."""

    name = "browser-rss"
    interval = 1.0
    stop_when_found = True

    def __init__(self, url: str, warmup: str, headline: str) -> None:
        self.feed = RssFeed(self.name, url)
        self.warmup = warmup
        self._host = browser_host(url)
        if browser_host(warmup) != self._host:
            raise ValueError("The IR feed must stay on its warmup page's host")
        self._headline = re.compile(headline, re.IGNORECASE)

    def url(self) -> str:
        return self.feed.feed_url

    async def fetch(self, fetcher: Fetcher) -> Response:
        return await fetcher.browser_request(self.url(), self.warmup, self.timeout)

    def parse(self, response: Response) -> list[Sighting]:
        items = []
        for item in self.feed.parse(response):
            if not (
                item.earnings
                and self._headline.fullmatch(item.title)
                and published_time(item.published)
            ):
                continue
            try:
                if browser_host(item.url) == self._host:
                    items.append(item)
            except ValueError:
                continue
        return items


def article_matches(title: str, body: bytes, *, verify_text: bool) -> bool:
    text = html_to_text(body.decode(errors="replace"))
    return (
        len(text) >= MIN_RELEASE_CHARS
        and (not verify_text or verified_report(text))
        and title.casefold() in " ".join(text.split()).casefold()
    )


def issuer_pdf_url(article: str, body: bytes) -> str | None:
    parser = _ReleaseLinks()
    parser.feed(body.decode(errors="replace"))
    for href, label in parser.links:
        if label.casefold() != "pdf version":
            continue
        try:
            url = urldefrag(urljoin(article, href)).url
            _check_pdf(url, article)
        except ValueError:
            continue
        return url
    return None


class _VerifiedPDF(GuessedFile):
    """A single GET both detects the PDF and provides its attachment bytes."""

    method = "GET"
    report_initial = True

    def __init__(self, url: str, source_prefix: str) -> None:
        super().__init__(url)
        self.name = source_prefix + self.name.removeprefix("guess:")

    def parse(self, response: Response) -> list[Sighting]:
        if response.status != 200 or not response.body.startswith(b"%PDF-"):
            return []
        return [
            Sighting(
                key=self.file_url,
                title=self.name.split(":", 1)[-1],
                url=self.file_url,
                published=response.headers.get("last-modified"),
                earnings=True,
                details={"size": str(len(response.body))},
                body=response.body,
            )
        ]


class BrowserPDF(_VerifiedPDF):
    # A manual Fetch redirect is opaque (status 0). Some sites redirect missing PDFs.
    ok_statuses = frozenset({0, 200, 404})

    def __init__(self, url: str, warmup: str) -> None:
        super().__init__(url, "browser-pdf:")
        _check_pdf(url, warmup)
        self._warmup = warmup

    async def fetch(self, fetcher: Fetcher) -> Response:
        return await fetcher.browser_request(self.url(), self._warmup, self.timeout)


class TeslaDeck(_VerifiedPDF):
    def __init__(self, url: str) -> None:
        super().__init__(url, "tesla-pdf:")
        _check_tesla(url)

    async def fetch(self, fetcher: Fetcher) -> Response:
        return await fetcher.request(
            self.method,
            self.url(),
            {"User-Agent": _TESLA_AGENT},
            give_up_after=self.timeout,
            allow_redirects=False,
        )
