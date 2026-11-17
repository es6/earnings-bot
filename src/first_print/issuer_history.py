"""Bounded historical issuer listings, including dated Equisolve rows and Chrome feeds."""

from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp

from first_print.browser import BrowserError, browser_host
from first_print.fetch import Fetcher
from first_print.periods import title_period
from first_print.publisher_history import publication
from first_print.sources import (
    RssFeed,
    Sighting,
    is_date_notice,
    looks_like_earnings,
    preliminary_headline,
)


class _Rows(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.rows: list[Sighting] = []
        self.in_article = False
        self.stamp = ""
        self.link = ""
        self.title: list[str] = []
        self.text: list[str] = []
        self.in_link = False
        self.next_url: str | None = None
        self.href = ""
        self.link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "article":
            self.in_article = True
            self.stamp, self.link = "", ""
            self.title, self.text = [], []
        if tag == "a":
            self.href = values.get("href") or ""
            self.link_text = []
            if self.in_article and not self.link:
                self.link = self.href
                self.in_link = True
            if "next" in (values.get("rel") or "").split():
                self.next_url = urljoin(self.base, self.href)
        if tag == "time" and self.in_article:
            self.stamp = values.get("datetime") or ""

    def handle_data(self, data: str) -> None:
        if self.in_article:
            self.text.append(data)
        if self.in_link:
            self.title.append(data)
        if self.href:
            self.link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            if "next" in " ".join(self.link_text).casefold().split():
                self.next_url = urljoin(self.base, self.href)
            self.in_link, self.href = False, ""
        if tag != "article" or not self.in_article:
            return
        self.in_article = False
        try:
            at = datetime.fromisoformat(self.stamp)
            if at.tzinfo is None:
                at = at.replace(tzinfo=ZoneInfo("America/New_York"))
                if at.tzname() not in " ".join(self.text).split():
                    return
            url = urljoin(self.base, self.link)
            if not self.link or browser_host(url) != browser_host(self.base):
                return
            title = " ".join(" ".join(self.title).split())
            if title:
                self.rows.append(Sighting(url, title, url, at.isoformat()))
        except ValueError:
            return


def listing_rows(url: str, body: bytes) -> tuple[list[Sighting], str | None]:
    parser = _Rows(url)
    parser.feed(body.decode("utf-8", errors="replace"))
    next_url = parser.next_url
    if next_url:
        try:
            if browser_host(next_url) != browser_host(url):
                next_url = None
        except ValueError:
            next_url = None
    return parser.rows, next_url


async def listing_history(
    fetcher: Fetcher, listing: str, *, chrome: bool = False
) -> list[Sighting]:
    """Follow at most three public, linked pages; no invented pagination or host changes."""
    browser_host(listing)
    items: dict[str, Sighting] = {}
    pending: dict[str, Sighting] = {}
    seen: set[str] = set()
    url: str | None = listing
    for _ in range(3):
        if not url or url in seen:
            break
        seen.add(url)
        try:
            response = (
                await fetcher.browser_request(url, listing, 8)
                if chrome
                else await fetcher.request("GET", url, give_up_after=8, allow_redirects=False)
            )
            if response.status != 200:
                break
            rows, url = listing_rows(url, response.body)
            items.update((item.key, item) for item in rows)
            if len(pending) < 3:
                from first_print.browser_sources import _ReleaseLinks

                parser = _ReleaseLinks()
                parser.feed(response.body.decode(errors="replace"))
                for href, title in parser.links:
                    article = urljoin(response.url, href)
                    if (
                        len(pending) < 3
                        and title_period(title) is not None
                        and looks_like_earnings(title)
                        and not is_date_notice(title)
                        and not preliminary_headline(title)
                        and browser_host(article) == browser_host(listing)
                    ):
                        pending.setdefault(article, Sighting(article, title, article))
        except (aiohttp.ClientError, BrowserError, TimeoutError, ValueError):
            break
    if len(items) < 3:
        for item in pending.values():
            if item.key in items:
                continue
            try:
                if stamped := await publication(fetcher, item, listing, chrome=chrome):
                    items[stamped.key] = stamped
            except (aiohttp.ClientError, BrowserError, TimeoutError, ValueError):
                continue
    return list(items.values())


async def browser_feed_history(fetcher: Fetcher, feed: str) -> list[Sighting]:
    try:
        response = await fetcher.browser_request(feed, urljoin(feed, "/"), 8)
        if response.status == 200:
            return RssFeed("issuer-history", feed).parse(response)
    except (aiohttp.ClientError, BrowserError, TimeoutError, ValueError):
        return []
    return []
