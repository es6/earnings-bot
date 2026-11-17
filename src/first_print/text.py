"""Getting the full text of a release, wherever it was first seen, for the summary."""

from __future__ import annotations

import asyncio
import io
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp
from pypdf import PdfReader

from first_print.fetch import Fetcher
from first_print.html_text import html_to_text as html_to_text
from first_print.source_document import SourceDocument, document_from_html, document_from_text
from first_print.sources import (
    BROADCOM_RELEASE,
    BroadcomNewsroom,
    EdgarFilings,
    GuessedFile,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
    bust_cache,
)

# Shorter than any real earnings release, so error pages and teasers don't count
MIN_RELEASE_CHARS = 2000
# Newswires whose main article hosts block the HTTP client.
BLOCKED_ARTICLES = ("businesswire.com", "globenewswire.com")


def verified_report(text: str | None) -> bool:
    return bool(
        text
        and len(text) >= MIN_RELEASE_CHARS
        and re.search(r"\b(?:revenues?|net sales)\b", text, re.IGNORECASE)
        and re.search(
            r"\b(?:net (?:income|loss|earnings)|per (?:diluted )?share|EPS)\b", text, re.IGNORECASE
        )
    )


# A results release runs 10 to 30 pages. More than this is a filing or a deck, not a release.
MAX_PDF_PAGES = 60


def pdf_text(data: bytes) -> str:
    """A release PDF's text, or "" when it can't be read. Takes about 0.3 s for a 20-page
    release, so call it in a thread."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            return ""
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        # pypdf raises its own errors and plain ones (KeyError, ValueError, RecursionError) on a
        # damaged or unusual file. No text is the answer to all of them.
        return ""
    # A line wrapped at a hyphen ("prior-" / "year quarter") reads "prior-year", as the HTML does
    return re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "-", text)


def pick_exhibit(files: list[dict[str, str]], primary: str) -> str | None:
    """The press release among a filing's files: the exhibit named like one, else the biggest
    HTML document that isn't the 8-K itself or an XBRL viewer page."""
    pages = [
        f
        for f in files
        if f["name"].lower().endswith((".htm", ".html"))
        and f["name"] != primary
        and "index" not in f["name"]
        and not re.fullmatch(r"R\d+\.htm", f["name"])
    ]
    named = [
        f for f in pages if re.search(r"ex-?99|99-?1|press|release|earn", f["name"], re.IGNORECASE)
    ]

    def size(f: dict[str, str]) -> int:
        return int(f["size"]) if str(f.get("size", "")).isdigit() else 0

    best = max(named or pages, key=size, default=None)
    return best["name"] if best else None


async def _get_text(fetcher: Fetcher, url: str) -> str | None:
    # The release just turned up, so this goes ahead of the host's polls
    response = await fetcher.request("GET", url, urgent=True)
    if response.status != 200:
        return None
    return response.body.decode(errors="replace")


async def _broadcom_body(fetcher: Fetcher, sighting: Sighting) -> str | None:
    # An ID Broadcom doesn't know yet still answers 200, with a stub that has no "body". This is
    # busted like the listing, in case Cloudflare kept that stub.
    url = bust_cache(BROADCOM_RELEASE.format(path=sighting.details["path"]))
    page = await _get_text(fetcher, url)
    release = json.loads(page) if page else None
    body = release.get("body") if isinstance(release, dict) else None
    return body if isinstance(body, str) and body else None


async def _q4_document(
    fetcher: Fetcher, source: Q4PressReleases, release_id: str
) -> SourceDocument | None:
    # The release that just turned up is nearly always the newest, so ask for it alone first:
    # Uber's list of five came to 459 KB and 0.76 s, its newest alone to 10 KB and 0.2 s. Each
    # length is its own copy on Q4's CDN, so if that copy is a few seconds too old to have the
    # release, the normal list is a second chance.
    for page_size in dict.fromkeys((1, source.page_size)):
        try:
            page = await _get_text(fetcher, source.bodies_url(page_size))
            releases = json.loads(page)["GetPressReleaseListResult"] if page else []
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(releases, list):
            continue
        for release in releases:
            if isinstance(release, dict) and str(release.get("PressReleaseId")) == release_id:
                body = release.get("Body")
                if isinstance(body, str) and (document := await _usable_document(body)):
                    return document
    return None


class _FilingDocuments(HTMLParser):
    """Read the exhibit rows in an EDGAR Filing Detail page's document table."""

    def __init__(self) -> None:
        super().__init__()
        self.exhibits: list[dict[str, str]] = []
        self._in_table = False
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = dict(attrs).get("summary") == "Document Format Files"
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag == "td":
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._in_table = False
        elif tag == "td" and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._in_table and len(self._row) == 5:
            _, description, document, form, size = self._row
            name = document.split(maxsplit=1)[0] if document else ""
            # Use the SEC's exhibit type. The 8-K itself is a separate row in the same table.
            if re.fullmatch(r"EX-99(?:\.\d+)?", form) and re.fullmatch(
                r"[\w.-]+\.html?", name, re.IGNORECASE
            ):
                self.exhibits.append(
                    {
                        "name": name,
                        "size": size.replace(",", ""),
                        "description": description,
                        "form": form,
                    }
                )


def filing_exhibits(html: str) -> list[dict[str, str]]:
    parser = _FilingDocuments()
    parser.feed(html)
    return parser.exhibits


async def _edgar_exhibit(fetcher: Fetcher, sighting: Sighting) -> str | None:
    folder, primary = sighting.url.rsplit("/", 1)
    if urlsplit(sighting.url).path.lower().endswith(("-index.htm", "-index.html")):
        # The latest-filings link is an index, so it doesn't give us the primary filename.
        detail = await _get_text(fetcher, sighting.url)
        if detail is None:
            return None
        exhibits = filing_exhibits(detail)
        results = [
            row
            for row in exhibits
            if re.search(r"\b(?:earnings|results)\b", row["description"], re.IGNORECASE)
        ]
        exhibit_991 = [row for row in exhibits if row["form"] == "EX-99.1"]
        exhibit = pick_exhibit(results or exhibit_991 or exhibits, primary)
    else:
        listing = await _get_text(fetcher, f"{folder}/index.json")
        if listing is None:
            return None
        exhibit = pick_exhibit(json.loads(listing)["directory"]["item"], primary)
    return await _get_text(fetcher, f"{folder}/{exhibit}") if exhibit else None


def globe_article_url(url: str) -> str | None:
    parts = urlsplit(url)
    if (
        parts.scheme == "https"
        and parts.netloc == "www.globenewswire.com"
        and parts.path.startswith("/news-release/")
        and parts.path.endswith(".html")
    ):
        return parts._replace(netloc="rss.globenewswire.com").geturl()
    return None


async def release_text(fetcher: Fetcher, source: Source, sighting: Sighting) -> str | None:
    """Compatibility reader for classifiers/audits that need only the release text."""
    document = await release_document(fetcher, source, sighting)
    return document.text if document else None


async def release_document(
    fetcher: Fetcher, source: Source, sighting: Sighting
) -> SourceDocument | None:
    """The release's text from wherever this sighting points, or None if this source can't
    provide it."""
    try:
        if sighting.body is not None:
            # The poll already downloaded the page. A PDF is read too: the pipeline asks for
            # one only when no page has delivered the text. Brookfield's HTML came 4 minutes
            # after its PDF.
            if sighting.body.startswith(b"%PDF-"):
                text = await asyncio.to_thread(pdf_text, sighting.body)
                return (
                    await asyncio.to_thread(document_from_text, text)
                    if verified_report(text)
                    else None
                )
            html = sighting.body.decode(errors="replace")
            if sighting.details.get("verified_exhibit") == "true":
                # Already parsed by the classifier; do not reinterpret text as HTML.
                return await asyncio.to_thread(document_from_text, html)
        elif isinstance(source, Q4PressReleases):
            return await _q4_document(fetcher, source, sighting.key)
        elif isinstance(source, BroadcomNewsroom):
            html = await _broadcom_body(fetcher, sighting) if sighting.earnings else None
        elif isinstance(source, EdgarFilings):
            html = await _edgar_exhibit(fetcher, sighting) if sighting.earnings else None
        elif isinstance(source, GuessedFile | RssFeed):
            url = urlsplit(sighting.url)
            from first_print.wire_mirror import identity, recover

            if isinstance(source, RssFeed) and identity(sighting.url):
                return await recover(fetcher, sighting)
            if isinstance(source, RssFeed) and (article := globe_article_url(sighting.url)):
                # The publisher's feed hostname serves the same article. Keep the original
                # URL for Discord and skip the main hostname's 403 on the body request.
                html = await _get_text(fetcher, article)
            elif (url.hostname or "").endswith(BLOCKED_ARTICLES) or url.path.lower().endswith(
                ".pdf"
            ):
                return None
            else:
                html = await _get_text(fetcher, sighting.url)
        else:
            return None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    return await _usable_document(html)


async def _usable_document(html: str | None) -> SourceDocument | None:
    # Parsing an 8-K exhibit takes about 30 ms of pure Python. In a thread, the event loop gets
    # the interpreter back every few milliseconds, so other windows' polls don't wait on it.
    if not html:
        return None
    try:
        document = await asyncio.to_thread(document_from_html, html)
    except ValueError:
        return None
    return document if len(document.text) >= MIN_RELEASE_CHARS else None
