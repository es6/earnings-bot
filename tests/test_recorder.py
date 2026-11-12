import asyncio
import io
import itertools
import json
import sys
import time
from collections import deque
from collections.abc import Hashable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from earnings_bot import clock, fetch
from earnings_bot.clock import Sample
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.recorder import (
    ET,
    Journal,
    Polls,
    Target,
    Watch,
    configure_console,
    follow_up,
    load_plan,
    record_items,
    summarize,
    sync_clock,
    watch,
)
from earnings_bot.sources import GuessedFile, GuessedPage, RssFeed, Sighting, Source, published_time
from tests.fakes import RoutedFetcher


class ListSource(Source):
    """Lists the keys in the response body, one per line."""

    name = "list"
    interval = 0.01

    def url(self) -> str:
        return "https://example.com/list"

    def parse(self, response: Response) -> list[Sighting]:
        keys = response.body.decode().split()
        return [Sighting(key, f"Item {key}", "", earnings=True) for key in keys]


class FakeFetcher(Fetcher):
    """Serves canned responses in order, then repeats the last one."""

    def __init__(self, *responses: tuple[int, str], queued: float = 0.0) -> None:
        super().__init__("Test Person test@example.com")
        self.responses = deque(responses)
        self.queued = queued  # how long each request waits for its host's turn
        self.sent = 0

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
        encoded_url: bool = False,
    ) -> Response:
        self.sent += 1
        status, body = self.responses[0] if len(self.responses) == 1 else self.responses.popleft()
        if status == 0:
            raise TimeoutError
        return Response(url, status, {"age": "3"}, body.encode(), 0.02, self.queued)


def events(log: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.getvalue().splitlines()]


def test_plan_times_default_to_eastern(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.toml"
    plan_file.write_text(
        '[[target]]\nticker = "UBER"\nexpected = 2026-11-04T06:55:00\ncik = 1543151\n'
        'q4_host = "investor.uber.com"\nnewswire = ["Uber"]\n'
        'guess = ["https://s23.q4cdn.com/1/Uber-Q3-26-Earnings-Press-Release.pdf"]\n'
    )

    [target] = load_plan(plan_file).target

    assert target.expected == datetime(2026, 11, 4, 6, 55, tzinfo=ET)
    assert target.expected.utcoffset() == timedelta(hours=-5)  # daylight saving ended Nov 1
    assert target.window[0] == datetime(2026, 11, 4, 6, 45, tzinfo=ET)
    assert [source.name for source in target.sources()] == [
        "q4_press",
        "q4_files",
        "edgar",
        "edgar_latest",
        "businesswire_busted",
        "guess:Uber-Q3-26-Earnings-Press-Release.pdf",
    ]


async def test_watch_logs_items_that_appear_after_the_first_poll() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("UBER", ListSource())
    fetcher = FakeFetcher((200, "a"), (500, ""), (200, "a b"))

    await watch(w, fetcher, journal, end=journal.now() + timedelta(seconds=0.2))

    new = [e for e in events(log) if e["event"] == "new"]
    assert [e["key"] for e in new] == ["b"]
    assert new[0]["headers"] == {"age": "3"}
    assert new[0]["previous_sent"] is not None
    assert w.first_title == "Item b"
    assert w.errors == 1
    assert w.problem is None
    assert w.polls > 3


async def test_watch_keeps_polling_after_a_timeout() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("HUBS", ListSource())
    fetcher = FakeFetcher((200, "a"), (0, ""), (200, "a b"))

    await watch(w, fetcher, journal, end=journal.now() + timedelta(seconds=0.2))

    [error] = [e for e in events(log) if e["event"] == "error"]
    assert error["message"] == "no answer within 3s"
    assert error["timeout"] is True
    assert [e["key"] for e in events(log) if e["event"] == "new"] == ["b"]
    assert w.problem is None
    # Counted apart from other errors, so a slow origin stays visible in the summary
    assert (w.timeouts, w.errors) == (1, 0)


async def test_fetcher_gives_up_on_a_server_that_never_answers() -> None:
    async def accept_and_say_nothing(reader: asyncio.StreamReader, _: asyncio.StreamWriter) -> None:
        await reader.read()  # until the client gives up and closes the connection

    server = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await fetcher.request("GET", f"http://127.0.0.1:{port}/", give_up_after=0.2)
        assert time.monotonic() - started < 1


async def test_fetcher_reads_a_header_longer_than_aiohttps_default() -> None:
    # Some of cloudflare.com's 404 pages carry a 13 KB policy header
    policy = "script-src 'self' " + "cdn.example " * 1200

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            "HTTP/1.1 404 Not Found\r\n"
            f"Content-Security-Policy-Report-Only: {policy}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        response = await fetcher.request("HEAD", f"http://127.0.0.1:{port}/")
    assert response.status == 404
    assert len(response.headers["content-security-policy-report-only"]) > 13_000


async def test_fetcher_doesnt_send_cookies_back() -> None:
    # cloudflare.com answers 500 to requests that carry the cookies it set
    requests: list[bytes] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: visitor=1; Path=/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        for _ in range(2):
            # A host name, since aiohttp's usual cookie jar ignores cookies from IP addresses
            # anyway, and this test would pass without the fix
            await fetcher.request("GET", f"http://localhost:{port}/")
    assert len(requests) == 2
    assert b"cookie" not in requests[1].lower()


async def test_a_refused_poll_is_retried_after_a_growing_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nebius refused one poll 12 minutes before its Q3 2026 release, then answered normally.
    The route had been stopped for the rest of the window."""
    monkeypatch.setattr(fetch, "BLOCK_PAUSE", 0.05)
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("NBIS", ListSource())
    fetcher = FakeFetcher((200, "old"), (403, ""), (403, ""), (200, "old results"))

    await watch(w, fetcher, journal, end=journal.now() + timedelta(seconds=0.6))

    assert w.stopped is None
    assert (w.blocked, w.blocks) == (2, 0)
    assert w.first is not None and w.first_title == "Item results"
    refused = [
        record
        for line in log.getvalue().splitlines()
        if (record := json.loads(line))["event"] == "error"
    ]
    assert [record["retry_in"] for record in refused] == [0.05, 0.1]


async def test_clock_sync_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"a": Sample("a", 5.0, 0.010), "b": Sample("b", 5.002, 0.030)}
    monkeypatch.setattr(clock, "ask", answers.get)
    log = io.StringIO()
    journal = Journal(log, clock.TrustedClock(servers=("a", "b")))

    assert await sync_clock(journal)

    [event] = events(log)
    assert event["server"] == "a"
    assert event["max_error_ms"] == 5.0
    assert event["servers"] == {"a": 0.0, "b": 2.0}


@pytest.mark.parametrize(("periodic", "kept"), [(False, False), (True, True)])
async def test_failed_clock_sync(
    monkeypatch: pytest.MonkeyPatch, periodic: bool, kept: bool
) -> None:
    monkeypatch.setattr(clock, "ask", lambda server: None)
    journal = Journal(io.StringIO(), clock.TrustedClock(servers=("a",)))
    journal.clock.synced = Sample("a", 5.0, 0.01)

    assert not await sync_clock(journal, periodic=periodic)

    # When a window opens, an old sync is dropped for the system clock. Mid-window it's kept.
    assert (journal.clock.synced is not None) is kept


async def test_windows_opening_together_share_a_clock_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def ask(server: str) -> Sample:
        asked.append(server)
        return Sample(server, 5.0, 0.01)

    monkeypatch.setattr(clock, "ask", ask)
    journal = Journal(io.StringIO(), clock.TrustedClock(servers=("a",)))

    assert await sync_clock(journal)
    assert await sync_clock(journal)
    assert asked == ["a"]


class FoundOnceSource(ListSource):
    stop_when_found = True


async def test_watch_stops_polling_a_guess_once_found() -> None:
    journal = Journal(io.StringIO())
    w = Watch("AAPL", FoundOnceSource())

    await watch(
        w, FakeFetcher((200, ""), (200, "page")), journal, journal.now() + timedelta(seconds=5)
    )

    assert w.polls == 2
    assert w.first_title == "Item page"


def test_extra_sources_come_from_the_targets_trial_table(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.toml"
    plan_file.write_text(
        '[[target]]\nticker = "AVGO"\nexpected = 2026-12-10T16:15:00\ncik = 1730168\n'
        'prnewswire = ["Broadcom Inc"]\n'
        "[target.trial]\nbroadcom_newsroom = true\n"
        'globenewswire = ["Broadcom Inc"]\nprnewswire_semiconductors = ["Broadcom Inc"]\n'
        'rss = ["https://ir.hubspot.com/rss/news-releases.xml"]\n'
        'pages = ["https://news.example.com/q4-results", "https://news.example.com/q4.pdf"]\n'
    )

    [target] = load_plan(plan_file).target
    sources = target.sources()

    assert [s.name for s in sources] == [
        "edgar",
        "edgar_latest",
        "prnewswire_busted",
        "prnewswire",
        "broadcom_newsroom",
        "rss:ir.hubspot.com",
        "globenewswire",
        "globenewswire_busted",  # both copies, to see whether busting helps
        "prnewswire_semiconductors",
        "page:news.example.com/q4-results",
        "page:news.example.com/q4.pdf",
    ]
    named = {source.name: source for source in sources}
    assert "_=" in named["rss:ir.hubspot.com"].url()  # a company feed is always busted
    assert "_=" not in named["globenewswire"].url()
    assert "_=" in named["globenewswire_busted"].url()


class StampedSource(Source):
    """Lists the items in a JSON body, each with its own timestamp."""

    name = "stamped"
    interval = 0.01

    def url(self) -> str:
        return "https://example.com/stamped"

    def parse(self, response: Response) -> list[Sighting]:
        return [
            Sighting(
                item["key"],
                f"Item {item['key']}",
                "",
                published=item["published"],
                earnings=item.get("earnings", True),
            )
            for item in json.loads(response.body)
        ]


async def test_a_late_start_catches_up_on_releases_published_inside_the_window() -> None:
    log = io.StringIO()
    journal = Journal(log)
    now = journal.now()
    late = {"key": "8-K", "published": (now - timedelta(minutes=2)).isoformat()}
    listed = [
        late,
        late,  # listed twice, still new once
        # Published days before the window opened, so it's old news
        {"key": "last quarter", "published": (now - timedelta(days=90)).isoformat()},
        # Q4's site-local time names no zone, but its day is the window's: already out
        {"key": "q4", "published": f"{now.astimezone(ET):%m/%d/%Y %H:%M:%S}"},
        # And one from another day isn't
        {
            "key": "q4 old",
            "published": f"{now.astimezone(ET) - timedelta(days=90):%m/%d/%Y %H:%M:%S}",
        },
        # Inside the window, but not the release
        {"key": "webcast", "published": now.isoformat(), "earnings": False},
    ]
    w = Watch("ABNB", StampedSource(), (now - timedelta(minutes=10), now + timedelta(seconds=1)))
    seen: list[Sighting] = []

    await watch(
        w,
        FakeFetcher((200, json.dumps(listed))),
        journal,
        now + timedelta(seconds=0.1),
        lambda source, item: seen.append(item),
    )

    new = [e for e in events(log) if e["event"] == "new"]
    assert [(e["key"], e["caught_up"]) for e in new] == [("8-K", True), ("q4", True)]
    assert [(item.key, item.caught_up) for item in seen] == [("8-K", True), ("q4", True)]
    [ready] = [e for e in events(log) if e["event"] == "ready"]
    assert (ready["listed"], ready["caught_up"]) == (6, 2)
    # Not a measurement of how fast the source is
    assert w.first is None
    assert w.caught_up == "Item 8-K"


async def test_a_release_out_before_the_window_opened_is_already_out() -> None:
    # On, Nov 10: its results were on its IR site at 5:00 AM ET, and the watch opened at 5:10
    log = io.StringIO()
    journal = Journal(log)
    now = journal.now()
    listed = [
        {"key": "results", "published": (now - timedelta(minutes=10)).isoformat()},
        {"key": "yesterday", "published": (now - timedelta(days=1)).isoformat()},
    ]
    w = Watch("ONON", StampedSource(), (now, now + timedelta(minutes=30)))

    await watch(w, FakeFetcher((200, json.dumps(listed))), journal, now + timedelta(seconds=0.1))

    new = [e for e in events(log) if e["event"] == "new"]
    assert [(e["key"], e["caught_up"]) for e in new] == [("results", True)]
    assert w.caught_up == "Item results"


class SoftNotFound(GuessedFile):
    """A guessed page whose site answers 200 for anything, stamped with the time of asking."""

    interval = 0.01

    def parse(self, response: Response) -> list[Sighting]:
        now = datetime.now(UTC).isoformat()
        return [Sighting(self.file_url, "Page", self.file_url, published=now, earnings=True)]


async def test_a_guess_already_there_at_the_first_poll_isnt_caught_up() -> None:
    log = io.StringIO()
    journal = Journal(log)
    now = journal.now()
    w = Watch(
        "UNH",
        SoftNotFound("https://example.com/q4-results"),
        (now - timedelta(minutes=10), now + timedelta(minutes=30)),
    )

    await watch(w, FakeFetcher((200, "")), journal, now + timedelta(seconds=0.1))

    assert [e for e in events(log) if e["event"] == "new"] == []
    assert w.polls == 1  # found at once, so it stops, as before


async def test_an_item_listed_twice_is_new_once() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("NET", ListSource())
    seen: list[str] = []

    await watch(
        w,
        FakeFetcher((200, "a"), (200, "a b b"), (200, "b a b")),
        journal,
        journal.now() + timedelta(seconds=0.1),
        lambda source, item: seen.append(item.key),
    )

    assert [e["key"] for e in events(log) if e["event"] == "new"] == ["b"]
    assert seen == ["b"]


async def test_new_items_log_how_long_the_poll_waited_for_its_host() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("AVGO", ListSource())
    fetcher = FakeFetcher((200, "a"), (0, ""), (200, "a b"), queued=0.25)

    await watch(w, fetcher, journal, end=journal.now() + timedelta(seconds=0.1))
    target = Target(ticker="AVGO", expected=journal.now(), cik=1730168)
    summarize(target, [w], journal)

    [new] = [e for e in events(log) if e["event"] == "new"]
    assert new["queued_ms"] == 250
    [summary] = [e for e in events(log) if e["event"] == "summary"]
    [row] = cast(list[dict[str, object]], summary["sources"])
    assert (row["queued_median_ms"], row["queued_max_ms"]) == (250, 250)
    assert (row["timeouts"], row["errors"]) == (1, 0)


class PageSource(ListSource):
    """Lists pages it downloaded whole."""

    def parse(self, response: Response) -> list[Sighting]:
        return [
            Sighting(key, f"Page {key}", "", earnings=True, body=b"<html>the release</html>")
            for key in response.body.decode().split()
        ]


async def test_a_poll_that_downloads_the_release_logs_its_text_arriving() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("CRWD", PageSource())

    await watch(
        w, FakeFetcher((200, ""), (200, "page")), journal, journal.now() + timedelta(seconds=0.1)
    )

    [new] = [e for e in events(log) if e["event"] == "new"]
    assert "body" not in new  # far too big for the log
    [body] = [e for e in events(log) if e["event"] == "body"]
    assert (body["key"], body["via"], body["bytes"]) == ("page", "poll", 24)


class CompanyFeed(Source):
    """One feed for every company, listing "TICKER:key" items, read for one company."""

    name = "feed"
    interval = 0.01

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    @property
    def share_key(self) -> Hashable:
        return self.name

    def url(self) -> str:
        return "https://example.com/feed"

    def parse(self, response: Response) -> list[Sighting]:
        items = response.body.decode().split()
        return [Sighting(i, i, "", earnings=True) for i in items if i.startswith(self.ticker)]


async def test_overlapping_windows_share_one_poll_of_a_feed() -> None:
    log = io.StringIO()
    journal = Journal(log)
    fetcher = FakeFetcher((200, ""), (200, "NVDA:1 AMD:2"))
    polls = Polls(fetcher, journal)
    nvda, amd = Watch("NVDA", CompanyFeed("NVDA")), Watch("AMD", CompanyFeed("AMD"))
    now = journal.now()

    await asyncio.gather(
        polls.watch(nvda, now + timedelta(seconds=0.1)),
        polls.watch(amd, now + timedelta(seconds=0.25)),
    )

    # Every request went to both watches while both windows were open
    assert fetcher.sent == amd.polls
    assert 1 < nvda.polls < amd.polls
    # Each watch still kept only its own company's items
    assert (nvda.first_title, amd.first_title) == ("NVDA:1", "AMD:2")
    new = [(e["ticker"], e["key"]) for e in events(log) if e["event"] == "new"]
    assert new == [("NVDA", "NVDA:1"), ("AMD", "AMD:2")]
    # Once both windows close, nothing polls the feed
    await asyncio.sleep(0.05)
    sent = fetcher.sent
    await asyncio.sleep(0.05)
    assert fetcher.sent == sent


class CountingFetcher(FakeFetcher):
    """Says when it has answered a given number of requests."""

    def __init__(self, *responses: tuple[int, str], after: int) -> None:
        super().__init__(*responses)
        self.after = after
        self.answered = asyncio.Event()

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
        encoded_url: bool = False,
    ) -> Response:
        response = await super().request(method, url, headers, give_up_after, urgent)
        if self.sent == self.after:
            self.answered.set()
        return response


async def test_a_window_opening_later_joins_the_feeds_poll() -> None:
    log = io.StringIO()
    journal = Journal(log)
    old, new = (200, "AMD:old"), (200, "AMD:old AMD:new")
    fetcher = CountingFetcher(old, old, old, new, after=2)
    polls = Polls(fetcher, journal)
    first, later = Watch("NVDA", CompanyFeed("NVDA")), Watch("AMD", CompanyFeed("AMD"))
    end = journal.now() + timedelta(seconds=0.2)

    async def join_later() -> None:
        await fetcher.answered.wait()  # the feed has been polled twice
        await polls.watch(later, end)

    await asyncio.gather(polls.watch(first, end), join_later())

    assert fetcher.sent == first.polls > later.polls
    # Its first poll is only a baseline, even though the feed had been polled before it joined
    assert [e["key"] for e in events(log) if e["event"] == "new"] == ["AMD:new"]


async def test_an_urgent_request_takes_the_hosts_next_turn_but_keeps_its_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "host_gap", lambda host: 0.05)
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[tuple[str, float]] = []

    async def take_turn(name: str, urgent: bool = False) -> None:
        await fetcher.wait_turn("example.com", urgent)
        turns.append((name, time.monotonic()))

    await take_turn("poll")  # the host is free, so this goes at once
    queued = [asyncio.create_task(take_turn(f"poll {i}")) for i in range(3)]
    await asyncio.sleep(0)  # they line up behind it
    await take_turn("release text", urgent=True)
    await asyncio.gather(*queued)

    assert [name for name, _ in turns] == ["poll", "release text", "poll 0", "poll 1", "poll 2"]
    gaps = [b - a for (_, a), (_, b) in itertools.pairwise(turns)]
    assert min(gaps) >= 0.045


async def test_fetcher_reports_how_long_a_request_waited_for_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "host_gap", lambda host: 0.2)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        url = f"http://127.0.0.1:{port}/"
        first, second = await asyncio.gather(
            fetcher.request("GET", url), fetcher.request("GET", url)
        )
    assert first.queued < 0.05
    assert 0.15 < second.queued < 0.4


class RateLimitedFetcher(FakeFetcher):
    """Answers every request with 429 and a Retry-After of an hour."""

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
        encoded_url: bool = False,
    ) -> Response:
        self.sent += 1
        return Response(url, 429, {"retry-after": "3600"}, b"", 0.01)


async def test_a_long_retry_after_is_honored_but_doesnt_hold_the_window_open() -> None:
    journal = Journal(io.StringIO())
    fetcher = RateLimitedFetcher()
    w = Watch("ABNB", ListSource())

    await asyncio.wait_for(watch(w, fetcher, journal, journal.now() + timedelta(seconds=0.2)), 2)

    assert fetcher.sent == 1


async def test_a_long_retry_after_on_a_shared_feed_lets_each_window_close_on_time() -> None:
    journal = Journal(io.StringIO())
    fetcher = RateLimitedFetcher()
    polls = Polls(fetcher, journal)
    nvda, amd = Watch("NVDA", CompanyFeed("NVDA")), Watch("AMD", CompanyFeed("AMD"))
    now = journal.now()

    closed: list[str] = []

    async def until_closed(w: Watch, seconds: float) -> None:
        await polls.watch(w, now + timedelta(seconds=seconds))
        closed.append(w.ticker)

    await asyncio.wait_for(asyncio.gather(until_closed(nvda, 0.1), until_closed(amd, 0.3)), 2)

    assert closed == ["NVDA", "AMD"]
    assert fetcher.sent == 1  # nothing polled during the hour it asked for


async def test_late_shared_watcher_closes_during_backoff_and_preserves_it() -> None:
    journal = Journal(io.StringIO())
    fetcher = RateLimitedFetcher()
    polls = Polls(fetcher, journal)
    first = asyncio.create_task(
        polls.watch(Watch("NVDA", CompanyFeed("NVDA")), journal.now() + timedelta(seconds=5))
    )
    try:
        await asyncio.sleep(0)  # the first member joins and starts the shared task
        await asyncio.sleep(0)  # the shared task reads the 429 and starts backing off
        assert fetcher.sent == 1
        end = journal.now() + timedelta(seconds=0.1)
        await asyncio.wait_for(polls.watch(Watch("AMD", CompanyFeed("AMD")), end), timeout=1)
        assert not first.done()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    # There are no other members now. A new window still honors the feed's Retry-After.
    end = journal.now() + timedelta(seconds=0.1)
    await asyncio.wait_for(polls.watch(Watch("AMD", CompanyFeed("AMD")), end), timeout=1)
    assert fetcher.sent == 1


async def test_a_bug_in_posting_doesnt_stop_the_source_polling() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("UBER", ListSource())
    fetcher = FakeFetcher((200, "a"), (200, "a b"), (200, "a b c"))
    handed: list[str] = []

    def on_new(source: Source, item: Sighting) -> None:
        handed.append(item.key)
        raise RuntimeError("bug in the poster")

    await watch(w, fetcher, journal, journal.now() + timedelta(seconds=0.2), on_new)

    assert handed == ["b", "c"]
    errors = [e["message"] for e in events(log) if e["event"] == "post_error"]
    assert errors == ["RuntimeError('bug in the poster')"] * 2


def test_console_output_cant_fail_on_characters_its_encoding_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What Windows gives Python when run-bot.ps1 sends its output to a file
    out, err = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err, encoding="cp1252"))

    configure_console()
    headline = f"Lilly{chr(0x200B)} reports results"  # a zero-width space, not in cp1252
    Journal(io.StringIO()).say(f"LLY prnewswire: NEW * {headline}")

    assert headline.encode("cp1252", errors="backslashreplace") in out.getvalue()


def test_a_timestamp_that_isnt_text_has_no_time() -> None:
    assert published_time(cast(Any, 1793394900)) is None
    assert published_time("2026-11-04T16:15:00-05:00") == datetime(2026, 11, 4, 21, 15, tzinfo=UTC)


async def test_after_the_window_a_source_that_hadnt_shown_the_release_still_links_it() -> None:
    # Sea: Business Wire had its results at 6:30 AM, its 6-K came hours after the window
    journal = Journal(io.StringIO())
    now = journal.now()
    target = Target(ticker="SE", cik=1703399, expected=now, before_minutes=1, after_minutes=0)
    later = {"key": "6-K", "published": now.isoformat()}
    quiet = Watch("SE", StampedSource(), target.window, known=set())  # nothing in the window
    found = Watch("SE", StampedSource(), target.window, known={"wire"}, first=now)
    seen: list[tuple[str, str]] = []

    await follow_up(
        target,
        [quiet, found],
        FakeFetcher((200, json.dumps([later]))),
        journal,
        lambda t, source, item: seen.append((t.ticker, item.key)),
        longest=timedelta(seconds=1),
    )

    # Only the source that hadn't shown it is checked, and its link joins the alert
    assert seen == [("SE", "6-K")]
    assert quiet.polls >= 1 and found.polls == 0


def test_new_wire_items_require_a_results_headline_for_the_scheduled_quarter() -> None:
    source = RssFeed("wire", "https://example.com/rss")
    journal = Journal(io.StringIO())
    now = journal.now()
    watch_state = Watch("XYZ", source, known=set(), period="FY2026Q3")
    articles = [
        "Company posts Q3 product sales figures",
        "Company Reports Q3 2026 Results of Its Share Repurchase Program",
        "Company to Report Q3 2026 Financial Results",
        "Company Reports Q2 2026 Financial Results",
        "Company Reports 2026 Q3 Financial Results, Up 17% over Q3 2025",
        "Company Reports 2026 Third Quarter Net Income",
    ]
    feed = (
        "<rss><channel>"
        + "".join(
            f"<item><title>{title}</title><link>https://example.com/{i}</link></item>"
            for i, title in enumerate(articles)
        )
        + "</channel></rss>"
    )
    response = Response(source.url(), 200, {}, feed.encode(), 0.01)
    sightings: list[Sighting] = []

    record_items(
        watch_state,
        source.parse(response),
        response,
        journal,
        now,
        lambda _, item: sightings.append(item),
    )

    assert [item.title for item in sightings if item.earnings] == articles[-2:]


async def test_resume_enriches_from_a_verified_guessed_page_and_baselines_head_only_urls() -> None:
    journal = Journal(io.StringIO())
    now = journal.now()
    target = Target(ticker="AAA", cik=7, expected=now, after_minutes=0, period="FY2026Q3")
    report = "https://example.com/third-quarter-2026"
    file = "https://example.com/latest"
    body = b"<title>Company Q3 2026 Financial Results</title>" + (
        b"<p>Quarterly revenue of $2 billion and net income of $100 million.</p>" * 50
    )
    fetcher = RoutedFetcher(
        {report: (200, body), file: (200, b"")},
        {"content-type": "text/html"},
    )
    page = Watch("AAA", GuessedPage(report), target.window, period=target.period)
    head = Watch("AAA", GuessedFile(file), target.window, period=target.period)
    sightings: list[Sighting] = []

    await follow_up(
        target,
        [page, head],
        fetcher,
        journal,
        lambda _, __, item: sightings.append(item),
        longest=timedelta(milliseconds=50),
    )

    assert [item.url for item in sightings if item.earnings] == [report]
