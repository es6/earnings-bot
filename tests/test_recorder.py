import io
import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from earnings_bot import clock
from earnings_bot.clock import Sample
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.recorder import ET, Journal, Watch, load_plan, sync_clock, watch
from earnings_bot.sources import Sighting, Source


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

    def __init__(self, *responses: tuple[int, str]) -> None:
        super().__init__("Test Person test@example.com")
        self.responses = deque(responses)

    async def request(self, method: str, url: str) -> Response:
        status, body = self.responses[0] if len(self.responses) == 1 else self.responses.popleft()
        return Response(url, status, {"age": "3"}, body.encode(), 0.02)


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
        "businesswire",
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


async def test_watch_stops_when_blocked() -> None:
    log = io.StringIO()
    journal = Journal(log)
    w = Watch("FICO", ListSource())

    await watch(w, FakeFetcher((403, "")), journal, end=journal.now() + timedelta(seconds=5))

    assert w.polls == 1
    assert w.stopped is not None
    assert w.first is None


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
