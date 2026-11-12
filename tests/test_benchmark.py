import asyncio
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import discord
import pytest

from earnings_bot import unattended
from earnings_bot.benchmark import (
    EMBED,
    TIMELINES,
    BenchmarkPost,
    BenchmarkResult,
    EarningsBenchmark,
)
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher
from earnings_bot.pipeline import Releases
from earnings_bot.recorder import Journal, OnNew, Polls, Target, Watch
from earnings_bot.sources import EdgarLatest
from tests.fakes import RoutedFetcher
from tests.test_pipeline import FakePoster

UBER = BenchmarkPost("2117948805941223450", "UBER")
MCD = BenchmarkPost("2117950124349374669", "MCD")
UBER_TITLE = "$UBER (Uber Technologies) #earnings are out:"
MCD_TITLE = "$MCD (McDonald's) #earnings are out:"


def _feed(*rows: tuple[BenchmarkPost, str]) -> bytes:
    items = "".join(
        f"<item><title>{escape(title)}</title><link>{post.url}</link></item>"
        for post, title in rows
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


def _embed(post: BenchmarkPost, html: str, account: str = "earnings_guy") -> tuple[str, bytes]:
    params = urlencode({"url": post.url, "omit_script": "1"})
    data = {"author_url": f"https://twitter.com/{account}", "url": post.url, "html": html}
    return f"{EMBED}?{params}", json.dumps(data).encode()


async def test_tickers_share_one_timeline_lookup_with_a_fallback() -> None:
    results = _feed((UBER, UBER_TITLE), (MCD, MCD_TITLE))
    fetcher = RoutedFetcher({TIMELINES[0]: (429, b""), TIMELINES[1]: (200, results)})
    benchmark = EarningsBenchmark(fetcher, lambda: UBER.posted)

    snapshots = await asyncio.gather(*(benchmark.snapshot() for _ in range(20)))

    assert snapshots == [(UBER, MCD)] * 20
    assert [url for _, url, _ in fetcher.requests] == list(TIMELINES)


async def test_matches_the_window_and_verifies_x_author_and_earnings_text() -> None:
    preview = BenchmarkPost(str(int(UBER.id) - (1 << 22)), "UBER")
    bad_author = BenchmarkPost(str(int(UBER.id) - (2 << 22)), "UBER")
    feed = _feed(
        (UBER, UBER_TITLE),
        (preview, "$UBER (Uber Technologies) chart review before earnings"),
        (bad_author, UBER_TITLE),
        (MCD, MCD_TITLE),
    )
    url, body = _embed(UBER, "<p>$UBER (Uber Technologies) <a>#earnings</a> are out:</p>")
    bad_url, bad_body = _embed(bad_author, UBER_TITLE, "someone_else")
    fetcher = RoutedFetcher({TIMELINES[0]: (200, feed), url: (200, body), bad_url: (200, bad_body)})
    end = UBER.posted + timedelta(minutes=2)
    benchmark = EarningsBenchmark(fetcher, lambda: end)

    result = await benchmark.watch("UBER", UBER.posted - timedelta(minutes=2), end)

    assert result.state == "matched"
    assert result.post == UBER
    assert UBER.posted.isoformat(timespec="milliseconds") == "2026-11-04T11:55:19.579+00:00"
    assert [seen for _, seen, _ in fetcher.requests] == [TIMELINES[0], bad_url, url]


@pytest.mark.parametrize(
    ("status", "body", "state"),
    [(403, b"", "unavailable"), (200, _feed((MCD, MCD_TITLE)), "no_post_found")],
)
async def test_feed_unavailable_differs_from_no_matching_post(
    status: int, body: bytes, state: str
) -> None:
    now = UBER.posted + timedelta(minutes=2)
    fetcher = RoutedFetcher({TIMELINES[0]: (status, body)})
    benchmark = EarningsBenchmark(fetcher, lambda: now)

    result = await benchmark.watch("UBER", UBER.posted - timedelta(minutes=2), now)

    assert result.state == state
    assert result.post is None


async def test_window_report_uses_discord_message_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = UBER.posted - timedelta(seconds=3)
    target = Target(ticker="UBER", cik=1543151, expected=UBER.posted, period="FY2026Q3")
    log = io.StringIO()
    journal = Journal(log)
    status: list[str] = []
    start_lookup = asyncio.Event()
    alert_time = UBER.posted - timedelta(seconds=2)
    url, body = _embed(UBER, UBER_TITLE)
    fetcher = RoutedFetcher({TIMELINES[0]: (200, _feed((UBER, UBER_TITLE))), url: (200, body)})
    benchmark = EarningsBenchmark(fetcher, lambda: target.window[1])
    releases = Releases(FakePoster, fetcher, journal, None)

    async def record(
        t: Target, f: Fetcher, j: Journal, on_new: OnNew | None, polls: Polls | None
    ) -> list[Watch]:
        store.prepare(t.release_key, "alert")
        store.delivered(t.release_key, discord.utils.time_snowflake(alert_time))
        start_lookup.set()
        return [Watch(t.ticker, EdgarLatest(t.cik), first=first)]

    async def after_alert(
        self: EarningsBenchmark, ticker: str, start: datetime, end: datetime
    ) -> BenchmarkResult:
        await start_lookup.wait()
        return await original(self, ticker, start, end)

    original = EarningsBenchmark.watch
    monkeypatch.setattr(EarningsBenchmark, "watch", after_alert)
    monkeypatch.setattr(unattended, "record_target", record)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, releases, store, status.append, benchmark
        )
        await runner._watch(target)

    assert len(status) == 1
    assert status[0].splitlines()[-1] == (
        f"-# [@earnings_guy](<{UBER.url}>) posted at 6:55:19.579 AM ET, 2.00s after our alert"
    )
    assert "@earnings_guy" in runner.health()
    event = json.loads(log.getvalue())
    assert event["period"] == "FY2026Q3"
    assert datetime.fromisoformat(event["first_seen"]) == first
    assert datetime.fromisoformat(event["discord_alert"]) == alert_time
    assert datetime.fromisoformat(event["x_posted"]) == UBER.posted
