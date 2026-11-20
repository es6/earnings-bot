"""Bounded independent official-route inventory; an empty newsroom remains unresolved."""

import re
from collections.abc import Sequence
from datetime import date
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlsplit

import aiohttp
from pydantic import Field

from first_print.browser import browser_host
from first_print.browser_sources import _ReleaseLinks
from first_print.fetch import Fetcher, Response
from first_print.newsrooms import Newsroom, NewsroomRelease

_LISTING = re.compile(
    r"/(?:press|news|newsroom|media|press-releases|news-releases)/?$", re.IGNORECASE
)


class NewsroomFinding(Newsroom):
    kind: Literal["listing", "navigation"] = "listing"
    state: Literal["verified", "unresolved"] = "unresolved"
    enabled: bool = False  # retain an established route through a temporary failed recheck
    reason: str
    discovered_from: str
    checked: date
    article: str | None = None
    period: str | None = Field(default=None, pattern=r"^FY\d{4}Q[1-4]$")

    def route(self) -> Newsroom:
        return Newsroom(listing=self.listing, names=self.names)


def merge_rooms(*groups: list[Newsroom]) -> list[Newsroom]:
    return list({room.listing: room for group in groups for room in group}.values())


async def discover_newsrooms(
    fetcher: Fetcher,
    homes: list[str],
    names: list[str],
    today: date,
    *,
    existing: Sequence[NewsroomFinding] = (),
    primary: Sequence[str] = (),
) -> list[NewsroomFinding]:
    """Read every supplied official host even when another host has a working feed.

    A bounded scan checks at most three homes and two same-host listing links per home.
    It retains previously discovered routes even without a recognized results article.
    A refusal stops this scan; the outer research loop already yields to live windows.
    """
    found = {item.listing: item for item in existing}
    candidates = {item.listing: item.discovered_from for item in existing if item.kind == "listing"}
    stopped = False

    def unresolved(url: str, origin: str, why: str, *, navigation: bool = False) -> None:
        old = found.get(url)
        found[url] = NewsroomFinding(
            listing=url,
            names=names,
            kind="navigation" if navigation else "listing",
            state="unresolved",
            enabled=bool(old and old.enabled),
            reason=why,
            discovered_from=origin,
            checked=today,
            article=old.article if old else None,
            period=old.period if old else None,
        )

    official_homes: dict[str, str] = {}
    for home in homes:
        official_homes.setdefault((urlsplit(home).hostname or "").removeprefix("www."), home)
    for home in list(official_homes.values())[:3]:
        try:
            browser_host(home)
            if stopped:
                unresolved(
                    home,
                    home,
                    "Not checked after an earlier HTTP refusal; retry required",
                    navigation=True,
                )
                continue
            page = await fetcher.request("GET", home, give_up_after=3, allow_redirects=False)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            unresolved(home, home, "Official navigation unread; retry required", navigation=True)
            continue
        if page.status != 200:
            unresolved(
                home,
                home,
                f"Official navigation returned HTTP {page.status}; retry required",
                navigation=True,
            )
            stopped = page.status in (403, 429)
            continue
        parser = _ReleaseLinks()
        parser.feed(page.body.decode("utf-8", "replace"))
        links = []
        for href, _ in [*parser.links, *parser.structured]:
            try:
                url = urldefrag(urljoin(home, href)).url
                if browser_host(url) != browser_host(home) or not _LISTING.search(
                    urlsplit(url).path
                ):
                    continue
            except ValueError:
                continue
            if url not in primary and url not in links:
                links.append(url)
        for url in links[:2]:
            candidates[url] = home
        covered = any(browser_host(url) == browser_host(home) for url in primary)
        if not links and not covered:
            unresolved(
                home, home, "No recognized newsroom link; manual review required", navigation=True
            )
        elif home in found and found[home].kind == "navigation":
            del found[home]
    for index, (url, origin) in enumerate(candidates.items()):
        if url in primary:
            continue
        if stopped or index >= 6:
            unresolved(url, origin, "Bounded scan deferred this route; retry required")
            continue
        source = NewsroomRelease(url, names, None)
        try:
            response: Response = await source.fetch(fetcher)
            items = source.parse(response)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            unresolved(url, origin, "Listing/article could not be read; retry required")
            continue
        if response.status in (403, 429):
            stopped = True
        if not items:
            unresolved(url, origin, f"HTTP {response.status}; no verified results article yet")
            continue
        item = items[0]
        found[url] = NewsroomFinding(
            listing=url,
            names=names,
            state="verified",
            enabled=True,
            reason="Issuer/period article verified; publication latency not measured",
            discovered_from=origin,
            checked=today,
            article=item.url,
            period=item.details["period"],
        )
    return list(found.values())
