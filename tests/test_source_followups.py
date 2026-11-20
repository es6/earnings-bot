import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from first_print.delivery import DeliveryStore
from first_print.globe_frontier import FEED, Frontier, Page, guess_release, is_release
from first_print.pipeline import Releases
from first_print.recorder import Journal, Target, Trial, Watch, summarize
from first_print.unattended import DirectoryWatcher
from tests.fakes import FakePoster, PinnedClock, RoutedFetcher, embed_text


def test_missing_ticker_requires_the_named_issuer_to_make_the_announcement() -> None:
    journal = Journal(io.StringIO(), PinnedClock())
    target = Target(
        ticker="EX",
        cik=123,
        expected=journal.now(),
        period="FY2026Q3",
        trial=Trial(globenewswire=["Example", "NASDAQ: EX"]),
    )
    story = (
        "<p>Example Inc. today announced its financial results for the third quarter of 2026.</p>"
        "<p>Total revenue grew 20% to $300 million.</p>"
    )
    page = Page(1, "https://example.com", "Global expansion", "", None, story.encode())
    assert is_release(page, target)
    for wrong in (
        story.replace("Example Inc.", "Example's competitor Acme"),
        story.replace("2026", "2025"),
        story.replace("third quarter", "second quarter"),
        story.replace("Example", "Other"),
    ):
        assert not is_release(Page(1, page.url, page.title, "", None, wrong.encode()), target)
    assert not is_release(
        Page(1, page.url, page.title, "NASDAQ: OTHER", None, story.encode()), target
    )


@pytest.mark.parametrize("status", [403, 429])
async def test_a_frontier_feed_refusal_stops_before_any_article_guess(status: int) -> None:
    fetcher = RoutedFetcher({FEED: (status, b"")})
    journal = Journal(io.StringIO(), PinnedClock())
    target = Target(
        ticker="EX", cik=123, expected=journal.now(), trial=Trial(globenewswire=["Example"])
    )
    await guess_release(target, fetcher, journal, None, journal.now() + timedelta(seconds=5))
    assert journal.guess_status[target.release_key] == "refused"
    assert len(fetcher.requests) == 1


@pytest.mark.parametrize("result", ["refused", "budget", "error", "time"])
async def test_guess_stops_are_in_status_and_the_window_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    log = io.StringIO()
    journal = Journal(log, PinnedClock())
    target = Target(
        ticker="EX",
        cik=123,
        expected=journal.now(),
        period="FY2026Q3",
        trial=Trial(globenewswire=["Example"]),
    )

    async def stopped(self: Frontier, *args: object) -> str:
        if result == "error":
            raise ValueError("fixture failure")
        return result

    monkeypatch.setattr(Frontier, "watch", stopped)
    await guess_release(
        target, RoutedFetcher({}), journal, None, journal.now() + timedelta(seconds=5)
    )
    assert journal.guess_status[target.release_key] == result
    summarize(target, [Watch(target.ticker, target.sources()[0])], journal)
    records = [json.loads(line) for line in log.getvalue().splitlines()]
    assert records[-1]["globe_guess_outcome"] == result
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        watcher = DirectoryWatcher(
            tmp_path,
            RoutedFetcher({}),
            journal,
            Releases(FakePoster, RoutedFetcher({}), journal, None),
            store,
            lambda _: None,
        )
        from first_print.unattended import _Window

        async def pending() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        try:
            watcher.windows[target.release_key] = _Window(target, task)
            assert f"guessing ended ({result})" in embed_text(watcher.health_embed("test", None))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_delivered_period_later_window_only_follows_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = Journal(io.StringIO(), PinnedClock())
    target = Target(
        ticker="EX", cik=123, expected=journal.now() + timedelta(minutes=5), period="FY2026Q3"
    )
    calls: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        store.prepare(target.release_key, "Already delivered")
        store.delivered(target.release_key, 1)
        watcher = DirectoryWatcher(
            tmp_path,
            RoutedFetcher({}),
            journal,
            Releases(FakePoster, RoutedFetcher({}), journal, None),
            store,
            lambda _: None,
        )
        watcher.follow_up = timedelta(hours=1)
        monkeypatch.setattr(watcher, "_targets", lambda: {target.release_key: target})

        async def follow(planned: Target) -> None:
            calls.append("follow")

        async def full(planned: Target) -> None:
            calls.append("full")

        monkeypatch.setattr(watcher, "_resume_links", follow)
        monkeypatch.setattr(watcher, "_watch", full)
        await watcher.scan()
        await asyncio.gather(*(w.task for w in watcher.windows.values()))
        assert calls == ["follow"]
        await watcher.scan()
        assert not watcher.windows
