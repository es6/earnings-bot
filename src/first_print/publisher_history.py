"""Explicit publication times from a report or the original publisher it links.

Date-only issuer cards never acquire an invented time zone or midnight timestamp.
"""

import asyncio
import re
from dataclasses import replace
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from first_print.browser import browser_host
from first_print.fetch import Fetcher, Response
from first_print.http_policy import retry_after
from first_print.periods import title_period
from first_print.sec_results import reported_period
from first_print.sources import Sighting, published_time
from first_print.text import html_to_text, verified_report


class _Metadata(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.titles: list[str] = []
        self.times: set[datetime] = set()
        self.links: list[str] = []
        self.in_title = False
        self.title: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content") or ""
            if name in {"date", "article:published_time", "datepublished"} and (
                at := published_time(content)
            ):
                self.times.add(at)
            if name == "og:title":
                self.titles.append(content)
        if tag == "h1":
            self.in_title, self.title = True, []
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self.in_title:
            self.titles.append(" ".join(self.title))
            self.in_title = False


def _headline(text: str) -> str:
    return " ".join(text.casefold().split())


def metadata(html: str, title: str) -> tuple[datetime | None, list[str]]:
    page = _Metadata(html)
    text = html_to_text(html)
    heading = re.search(r"\s+".join(re.escape(word) for word in title.split()), text, re.IGNORECASE)
    text = text[heading.start() :] if heading else ""
    valid = (
        any(_headline(found) == _headline(title) for found in page.titles)
        and verified_report(text)
        and title_period(title) is not None
        and reported_period(text) == title_period(title)
    )
    return (next(iter(page.times)) if valid and len(page.times) == 1 else None), page.links


async def publication(
    fetcher: Fetcher, item: Sighting, listing: str, *, chrome: bool
) -> Sighting | None:
    async def read(url: str, browser: bool) -> Response:
        pause = fetcher.route_pause(f"publisher-history:{browser_host(url)}")
        if pause.left():
            return Response(url, 0, {}, b"", 0)
        response = (
            await fetcher.browser_request(url, listing, 8)
            if browser
            else await fetcher.request("GET", url, give_up_after=8, allow_redirects=False)
        )
        headers = {key.lower(): value for key, value in response.headers.items()}
        wait = headers.get("retry-after", "")
        if response.status == 403:
            pause.refused()
        if wait or response.status == 429 or response.status >= 500:
            pause.hold(retry_after(wait))
        elif response.status == 200:
            pause.answered()
        return response

    response = await read(item.url, chrome)
    if response.status != 200:
        return None
    at, links = await asyncio.to_thread(
        metadata, response.body.decode(errors="replace"), item.title
    )
    if at:
        return replace(item, published=at.isoformat())
    for href in links:
        url = urljoin(item.url, href)
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "www.prnewswire.com"
            or not parts.path.startswith("/news-releases/")
            or not parts.path.endswith(".html")
            or parts.query
            or parts.fragment
        ):
            continue
        response = await read(url, False)  # one explicitly linked original publisher
        if response.status != 200:
            return None
        at, _ = await asyncio.to_thread(
            metadata, response.body.decode(errors="replace"), item.title
        )
        return (
            replace(item, url=url, published=at.isoformat(), details={"issuer_article": item.url})
            if at
            else None
        )
    return None
