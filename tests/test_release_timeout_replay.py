"""Replay a release going live during a run of timeouts, without external services.

The real HTTP queues, issuer matcher, watch loop, and release pipeline run here.
Only server answers, Discord, and the model are fakes. The browser transport fake
does not exercise Chrome's socket, rendering, session, or process lifecycle.
"""

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from earnings_bot import fetch
from earnings_bot.browser_sources import BrowserRelease, HttpRelease
from earnings_bot.fetch import Response
from earnings_bot.pipeline import Poster, Releases
from earnings_bot.recorder import Journal, Target, Watch, watch
from earnings_bot.summary import Summarizer, Summary
from tests.fakes import FakeSession, SessionFetcher

LISTING = "https://ir.example.com/results"
ARTICLE = "https://ir.example.com/q3-2026-results"
TITLE = "Uber Announces Results for Third Quarter 2026"
REPORT = (
    f"<h1>{TITLE}</h1><p>Uber reported revenue of $10 billion for the third quarter of 2026. "
    "Net income was $1 billion compared with $800 million a year earlier.</p>"
    + "<p>Management discussed quarterly demand and its financial outlook.</p>"
    * 50
).encode()
LINK = f'<a href="{ARTICLE}">{TITLE}</a>'.encode()


class RecordingPoster(Poster):
    def __init__(self) -> None:
        self.steps: list[str] = []
        self.sent = asyncio.Event()

    async def send(self, content: str) -> datetime:
        self.steps.append("alert")
        self.sent.set()
        return datetime.now(UTC)

    async def edit(self, content: str) -> datetime:
        self.steps.append("edit")
        return datetime.now(UTC)

    async def attach(self, name: str, data: bytes) -> datetime:
        raise AssertionError("This replay's report has no attachments")

    async def set_summary(self, text: str, footer: str) -> datetime:
        self.steps.append("summary")
        return datetime.now(UTC)


class RecordingWriter(Summarizer):
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def write(
        self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
    ) -> Summary:
        self.texts.append(release)
        return Summary("Revenue $10B", [], 0.0, 100, 10)


class BrowserTransport(SessionFetcher):
    """Simulate browser responses through the same paced fake network as HTTP."""

    async def browser_request(
        self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
    ) -> Response:
        return await self.request("GET", url, give_up_after=give_up_after, urgent=urgent)


def source_for(transport: str, listing: str = LISTING) -> BrowserRelease:
    source = (
        HttpRelease(listing, TITLE, verify_text=True, chrome_fallback=False)
        if transport == "http"
        else BrowserRelease(listing, TITLE, verify_text=True)
    )
    source.interval = 0.001
    source.timeout = 0.02
    return source


def target() -> Target:
    return Target(ticker="UBER", expected=datetime.now(UTC), cik=1543151, period="FY2026Q3")


@pytest.mark.parametrize("transport", ["http", "browser"])
@pytest.mark.parametrize("stage", ["listing", "article"])
async def test_release_survives_repeated_timeouts_and_a_temporary_200_page(
    transport: str, stage: Literal["listing", "article"], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three actual request deadlines expire after an initial successful empty poll.

    A subsequent HTTP 200 still contains only a temporary page. The next response
    contains the report. Neither timeouts nor that 200 may retire the watch, and
    only the verified report may cause an alert and one summary.
    """
    monkeypatch.setattr(fetch, "_DEFAULT_GAP", 0.001)
    poster, writer, log = RecordingPoster(), RecordingWriter(), io.StringIO()
    listing_calls, failures, temporary_pages = 0, 0, 0

    async def answer(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        nonlocal listing_calls, failures, temporary_pages
        if url == LISTING:
            listing_calls += 1
            if listing_calls == 1:
                return 200, {}, b"<h1>Previous quarter</h1>"
        failing_url = LISTING if stage == "listing" else ARTICLE
        if url == failing_url:
            if failures < 3:
                failures += 1
                assert poster.steps == []
                await asyncio.Event().wait()  # FakeSession enforces the real request deadline
            if temporary_pages == 0:
                temporary_pages += 1
                assert poster.steps == []
                return 200, {}, b"<title>Please wait</title><p>Checking your browser...</p>"
        if url == LISTING:
            return 200, {}, LINK
        assert url == ARTICLE
        assert poster.steps == []
        return 200, {}, REPORT

    source, journal = source_for(transport), Journal(log)
    watched = Watch("UBER", source, period="FY2026Q3")
    session = FakeSession(answer)
    async with BrowserTransport(session) as fetcher:
        releases = Releases(lambda: poster, fetcher, journal, writer)
        try:
            await watch(
                watched,
                fetcher,
                journal,
                journal.now() + timedelta(seconds=2),
                lambda source, item: releases.on_new(target(), source, item),
            )
        finally:
            await releases.finish()

    events = [json.loads(line) for line in log.getvalue().splitlines()]
    assert (failures, temporary_pages, watched.timeouts, watched.errors) == (3, 1, 3, 0)
    assert watched.problem is None and watched.stopped is None
    assert watched.done
    assert [e["key"] for e in events if e["event"] == "new"] == [ARTICLE]
    assert len([e for e in events if e["event"] == "error" and e.get("timeout")]) == 3
    assert poster.steps == ["alert", "summary"]
    assert len(writer.texts) == 1 and "revenue of $10 billion" in writer.texts[0]
    assert len([e for e in events if e["event"] == "posted"]) == 1
    # The pipeline reuses the verified poll body; it does not download the article again.
    assert len([url for _, _, url in session.sent if url == ARTICLE]) == (
        1 if stage == "listing" else 5
    )


@pytest.mark.parametrize("transport", ["http", "browser"])
async def test_an_independent_issuer_posts_while_another_request_is_still_stalled(
    transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery cannot serialize unrelated issuers behind a request that never answers."""
    monkeypatch.setattr(fetch, "_DEFAULT_GAP", 0.001)
    stalled = asyncio.Event()
    poster, writer, log = RecordingPoster(), RecordingWriter(), io.StringIO()
    failed_calls = 0
    posted_during_stall = False
    healthy_listing = "https://healthy.example.com/results"
    healthy_article = "https://healthy.example.com/q3-2026-results"

    async def answer(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        nonlocal failed_calls, posted_during_stall
        if url == LISTING:
            failed_calls += 1
            if failed_calls <= 3:
                stalled.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    posted_during_stall |= poster.sent.is_set()
            return 200, {}, b"<h1>No matching report</h1>"
        await stalled.wait()
        if url == healthy_listing:
            return 200, {}, LINK.replace(ARTICLE.encode(), healthy_article.encode())
        assert url == healthy_article
        return 200, {}, REPORT

    async with BrowserTransport(FakeSession(answer)) as fetcher:
        journal = Journal(log)
        releases = Releases(lambda: poster, fetcher, journal, writer)
        slow = Watch("SLOW", source_for(transport))
        healthy = Watch("UBER", source_for(transport, healthy_listing), period="FY2026Q3")
        # This is a recovery/ordering assertion, not a wall-clock performance budget.
        end = journal.now() + timedelta(seconds=1)
        try:
            await asyncio.gather(
                watch(slow, fetcher, journal, end),
                watch(
                    healthy,
                    fetcher,
                    journal,
                    end,
                    lambda source, item: releases.on_new(target(), source, item),
                ),
            )
        finally:
            await releases.finish()

    assert slow.timeouts == 3 and slow.stopped is None
    assert posted_during_stall
    assert healthy.done and healthy.problem is None
    assert poster.steps == ["alert", "summary"] and len(writer.texts) == 1
