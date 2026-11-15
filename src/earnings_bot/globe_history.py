"""A company's past releases from GlobeNewswire's public listings, each with the minute it came
out.

GlobeNewswire's keyword search and organization pages on rss.globenewswire.com list 50 releases a
page, each with its publish time in US Eastern to the minute, its organization and its headline.
Two of WeRide's organization pages reached back to July 2025 (measured Nov 14). The company's own
IR feed held only its last ten releases, back to August. The www host answers 403 to every
automated request; this one serves the same pages.
"""

import re
from collections import Counter
from datetime import datetime
from html import unescape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from earnings_bot.fetch import Fetcher
from earnings_bot.sources import Sighting

ET = ZoneInfo("America/New_York")
HOST = "https://rss.globenewswire.com"
PAGES = 2  # organization pages read, newest first
_ROW = re.compile(r'<li class="row">(.*?)</li>', re.DOTALL)
_WHEN = re.compile(r"<span>([A-Z][a-z]+ \d{1,2}, \d{4} \d{2}:\d{2}) ET</span>")
_SOURCE = re.compile(
    r'<a href="(/en/search/organization/[^"?]+)[^"]*"\s+class="sourceLink">([^<]+)</a>'
)
_LINK = re.compile(r'<div class="mainLink">\s*<a href="(/news-release/[^"]+)">(.*?)</a>', re.DOTALL)
_ID = re.compile(r"/news-release/\d{4}/\d{2}/\d{2}/(\d+)/")


def listed(html: str) -> list[tuple[str, str, Sighting]]:
    """(organization page, organization, release) for each release a listing page shows. The
    release's link is on www, as GlobeNewswire's feeds give it, so it's read like a feed item."""
    rows = []
    for row in _ROW.findall(html):
        when, source, link = _WHEN.search(row), _SOURCE.search(row), _LINK.search(row)
        if not (when and source and link) or "/en/" not in link.group(1):
            continue  # also a translation, which GlobeNewswire lists beside the English one
        at = datetime.strptime(when.group(1), "%B %d, %Y %H:%M").replace(tzinfo=ET)
        url = "https://www.globenewswire.com" + unescape(link.group(1))
        key = found.group(1) if (found := _ID.search(url)) else url
        title = " ".join(unescape(re.sub(r"<[^>]+>", "", link.group(2))).split())
        organization = unescape(source.group(1))
        rows.append(
            (
                organization,
                unescape(source.group(2)).strip(),
                Sighting(key, title, url, at.isoformat()),
            )
        )
    return rows


async def _page(fetcher: Fetcher, path: str) -> str | None:
    response = await fetcher.request("GET", HOST + quote(path, safe="/%?=&"), give_up_after=10)
    return response.body.decode(errors="replace") if response.status == 200 else None


# Words that don't tell one company from another: "WeRide Inc." is "weride"
_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "llc",
    "lp",
    "sa",
    "nv",
    "ag",
    "se",
    "the",
}


def _words(name: str) -> list[str]:
    """A name's words without punctuation or SEC's state of incorporation ("BROOKFIELD Corp
    /ON/")."""
    return re.sub(r"[^\w&\s]", " ", re.sub(r"/[A-Z]{2}/", " ", name)).split()


def _significant(name: str) -> list[str]:
    return [word for word in (w.lower() for w in _words(name)) if word not in _SUFFIXES]


async def past_releases(fetcher: Fetcher, name: str) -> list[Sighting]:
    """The company's own releases, newest first: from the organization a keyword search for its
    name lists most among those with the same name, suffixes aside ("WeRide Inc." for WeRide
    Inc.). A search for Brookfield Corporation listed Brookfield Asset Management, a different
    company, which mustn't count."""
    # Punctuation spoils the search: "WeRide Inc." finds nothing, "WeRide Inc" 46 of its releases
    words = _words(name)
    wanted = _significant(name)
    if not wanted:
        return []
    found = await _page(fetcher, f"/en/search/keyword/{quote(' '.join(words))}?pageSize=50")
    if found is None:
        return []
    counts = Counter(
        organization
        for organization, publisher, _ in listed(found)
        if _significant(publisher) == wanted
    )
    if not counts:
        return []
    organization = counts.most_common(1)[0][0]
    releases: list[Sighting] = []
    for page in range(1, PAGES + 1):
        html = await _page(fetcher, f"{organization}?page={page}&pageSize=50")
        if html is None:
            break
        releases += [item for listed_by, _, item in listed(html) if listed_by == organization]
    return releases
