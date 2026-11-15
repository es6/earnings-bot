import json
import sqlite3
import urllib.request
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from earnings_bot import watchdog as watchdog_module
from earnings_bot.watchdog import Watchdog, Window, post_webhook

OPEN = datetime(2026, 11, 17, 21, 10, tzinfo=UTC)
WINDOW = Window("1045810:FY2027Q3", "NVDA", OPEN, OPEN + timedelta(minutes=50))


def write(journal: Path, *records: dict[str, Any]) -> None:
    with journal.open("a", encoding="utf-8") as file:
        for record in records:
            values = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in record.items()}
            file.write(json.dumps(values) + "\n")


def progress(
    at: datetime, source: str, answered: datetime | None, problem: str | None = None, **more: Any
) -> dict[str, Any]:
    return {
        "at": at,
        "event": "progress",
        "ticker": "NVDA",
        "source": source,
        "interval": 1.0,
        "answered": answered,
        "problem": problem,
        "done": False,
        **more,
    }


def watchdog(journal: Path, posts: list[str]) -> Watchdog:
    return Watchdog(journal, lambda: [WINDOW], posts.append)


def test_a_route_that_stops_answering_raises_one_alarm_naming_it_then_clears(
    tmp_path: Path,
) -> None:
    """Nebius's newsroom refused a poll at 6:59 AM on Nov 12, and the route was down for the
    rest of the window without anything saying so."""
    journal, posts = tmp_path / "watch.jsonl", []
    dog = watchdog(journal, posts)
    seen = OPEN + timedelta(minutes=5)
    write(journal, progress(seen, "http-ir", seen), progress(seen, "edgar", seen))
    later = seen + timedelta(seconds=100)
    refused = "HTTP 403: the site refused us. Trying again after a pause"
    write(journal, progress(later, "http-ir", seen, refused), progress(later, "edgar", later))

    dog.check(later + timedelta(seconds=1))
    dog.check(later + timedelta(seconds=31))  # still down: no second post
    assert len(posts) == 1
    assert posts[0].startswith("🚨 **NVDA** · `http-ir`: no answer for 1m 41s.")
    assert refused in posts[0]

    back = later + timedelta(seconds=40)
    write(journal, progress(back, "http-ir", back), progress(back, "edgar", back))
    dog.check(back + timedelta(seconds=1))
    assert posts[1:] == ["✅ **NVDA** · `http-ir`: polling again."]


def test_a_silent_journal_while_a_window_is_open_says_the_bot_may_be_down(
    tmp_path: Path,
) -> None:
    journal, posts = tmp_path / "watch.jsonl", []
    seen = OPEN + timedelta(minutes=5)
    write(journal, progress(seen, "edgar", seen))
    watchdog(journal, posts).check(seen + timedelta(minutes=3))
    # A bot that never started writes no journal at all
    watchdog(tmp_path / "missing.jsonl", posts).check(seen)

    assert len(posts) == 2
    assert posts[0].startswith("🚨 **NVDA**: nothing written to the bot's journal for 3m.")
    assert "nothing written to the bot's journal at all" in posts[1]
    assert all("The bot may be down." in post for post in posts)


def test_a_route_with_no_poll_since_its_window_opened_is_named(tmp_path: Path) -> None:
    journal, posts = tmp_path / "watch.jsonl", []
    sources = ["edgar", "browser-ir"]
    window = {"event": "window", "ticker": "NVDA", "start": OPEN, "end": WINDOW.end}
    seen = OPEN + timedelta(minutes=3)
    write(journal, {"at": OPEN, **window, "sources": sources}, progress(seen, "edgar", seen))

    watchdog(journal, posts).check(seen + timedelta(seconds=1))

    assert len(posts) == 1
    assert posts[0].startswith("🚨 **NVDA** · `browser-ir`: no poll recorded since the window")


def test_no_alarm_early_in_a_window_after_it_or_for_a_finished_route(tmp_path: Path) -> None:
    journal, posts = tmp_path / "watch.jsonl", []
    dog = watchdog(journal, posts)
    found = OPEN + timedelta(seconds=30)
    write(journal, progress(found, "browser-ir", found, done=True))
    dog.check(OPEN + timedelta(seconds=90))  # sources are still starting

    fresh = OPEN + timedelta(minutes=10)
    write(journal, progress(fresh, "edgar", fresh))
    dog.check(fresh + timedelta(seconds=5))  # browser-ir found the release and stopped
    dog.check(WINDOW.end + timedelta(minutes=5))  # the window is over

    assert posts == []


def test_the_journal_is_read_as_it_grows_even_mid_line(tmp_path: Path) -> None:
    journal, posts = tmp_path / "watch.jsonl", []
    dog = watchdog(journal, posts)
    seen = OPEN + timedelta(minutes=5)
    line = json.dumps(
        {
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in progress(seen, "edgar", seen).items()
        }
    )
    with journal.open("w", encoding="utf-8") as file:
        file.write(line[:40])
    dog.read()
    assert dog.progress == {}
    with journal.open("a", encoding="utf-8") as file:
        file.write(line[40:] + "\n")
    dog.read()

    assert dog.progress["NVDA", "edgar"].answered == seen
    assert dog.last_line == seen
    assert posts == []


def test_a_failed_check_is_logged_and_the_next_one_still_alarms(tmp_path: Path) -> None:
    """The bot's calendar is a SQLite file the bot writes to; a busy moment must not end the
    watch, as an uncaught error would."""
    journal, posts = tmp_path / "watch.jsonl", []
    calls = 0

    def windows() -> list[Window]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return [WINDOW]

    dog = Watchdog(journal, windows, posts.append)
    seen = OPEN + timedelta(minutes=5)
    write(journal, progress(seen, "edgar", seen))

    assert not dog.check_safely(seen + timedelta(minutes=3))
    assert dog.check_safely(seen + timedelta(minutes=3, seconds=30))
    assert len(posts) == 1 and "The bot may be down." in posts[0]


def test_a_slow_route_gets_five_of_its_own_poll_intervals_before_it_counts_as_down(
    tmp_path: Path,
) -> None:
    journal, posts = tmp_path / "watch.jsonl", []
    seen = OPEN + timedelta(minutes=5)
    write(journal, progress(seen, "slow-feed", seen, interval=60.0))
    # Another route keeps the journal busy
    write(journal, progress(seen + timedelta(seconds=290), "edgar", seen + timedelta(seconds=290)))
    dog = watchdog(journal, posts)

    dog.check(seen + timedelta(seconds=250))  # past the usual 90 s, within 5 x 60 s
    assert posts == []
    dog.check(seen + timedelta(seconds=301))
    assert len(posts) == 1 and "`slow-feed`: no answer for 5m 1s." in posts[0]


def test_an_alarm_whose_webhook_post_failed_is_tried_again_at_the_next_check(
    tmp_path: Path,
) -> None:
    journal, posts, attempts = tmp_path / "watch.jsonl", [], []

    def flaky(text: str) -> None:
        attempts.append(text)
        if len(attempts) == 1:
            raise OSError("webhook unreachable")
        posts.append(text)

    seen = OPEN + timedelta(minutes=5)
    write(journal, progress(seen, "edgar", seen))
    dog = Watchdog(journal, lambda: [WINDOW], flaky)

    dog.check(seen + timedelta(minutes=3))
    assert posts == []  # the post failed, so nothing has been raised
    dog.check(seen + timedelta(minutes=3, seconds=30))
    assert len(posts) == 1 and "The bot may be down." in posts[0]
    dog.check(seen + timedelta(minutes=4))
    assert len(posts) == 1


def test_a_long_journal_is_read_from_its_tail_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "watch.jsonl"
    long_ago = OPEN - timedelta(hours=3)
    seen = OPEN + timedelta(minutes=5)
    write(
        journal,
        progress(long_ago, "old-route", long_ago),
        progress(long_ago, "older-route", long_ago),
        progress(seen, "edgar", seen),
    )
    last = journal.read_bytes().splitlines(keepends=True)[-1]
    monkeypatch.setattr(watchdog_module, "START_BYTES", len(last) + 10)  # into the line before it
    dog = watchdog(journal, [])

    dog.read()

    assert list(dog.progress) == [("NVDA", "edgar")]


def test_the_webhook_gets_the_text_cut_to_discords_limit_and_mentions_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> Any:
        sent.append(request)
        return nullcontext()

    monkeypatch.setattr(watchdog_module.urllib.request, "urlopen", urlopen)

    post_webhook("https://discord.example/api/webhooks/1/x", "@everyone " + "a" * 3000)

    [request] = sent
    body = json.loads(request.data)  # pyright: ignore[reportArgumentType]
    assert len(body["content"]) == 2000 and body["allowed_mentions"] == {"parse": []}
    assert request.get_method() == "POST"
