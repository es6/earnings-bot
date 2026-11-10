import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from earnings_bot import restart
from earnings_bot.calendar import Calendar
from earnings_bot.delivery import DeliveryStore
from earnings_bot.known import to_target
from earnings_bot.recorder import ET, Target
from earnings_bot.restart import WINDOW_OPEN, interrupted, schedule_targets

NOW = datetime(2026, 11, 9, 16, 0, tzinfo=ET)


def lucid(expected: datetime = NOW) -> Target:
    # Watched from 15:50 to 16:30
    return Target(ticker="LCID", expected=expected, cik=1811210, period="FY2026Q3")


def at(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


def test_a_restart_waits_while_a_window_is_open_or_about_to_open(tmp_path: Path) -> None:
    target = lucid()
    targets = {target.release_key: target}
    second = timedelta(seconds=1)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        assert interrupted(targets, store, at(-15) - second) == []
        assert interrupted(targets, store, at(-15)) == [
            "LCID's window opens at 15:50 and runs until 16:30 ET, with no alert posted yet"
        ]
        assert interrupted(targets, store, at(-10)) == [
            "LCID's window is open and runs until 16:30 ET, with no alert posted yet"
        ]
        assert len(interrupted(targets, store, at(30) - second)) == 1
        assert interrupted(targets, store, at(30)) == []


def test_a_restart_goes_ahead_once_the_alert_is_posted(tmp_path: Path) -> None:
    target = lucid()
    targets = {target.release_key: target}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        # Written down but not yet accepted by Discord: a restart could lose it
        store.prepare(target.release_key, "🚨 **$LCID earnings are out**")
        assert len(interrupted(targets, store, at(0))) == 1
        store.delivered(target.release_key, 123)
        assert interrupted(targets, store, at(0)) == []


def test_a_restart_goes_ahead_once_the_window_is_closed(tmp_path: Path) -> None:
    target = lucid()
    targets = {target.release_key: target}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.close_window(target.release_key, target.expected)
        assert interrupted(targets, store, at(0)) == []
    # Another channel's state doesn't count
    with DeliveryStore(tmp_path / "state.db", 8) as store:
        assert len(interrupted(targets, store, at(0))) == 1


def test_every_window_in_the_way_is_listed_in_order(tmp_path: Path) -> None:
    later = to_target("NVDA", at(10), 2027, 3)
    earlier = lucid()
    targets = {later.release_key: later, earlier.release_key: earlier}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        assert [reason.split("'")[0] for reason in interrupted(targets, store, at(0))] == [
            "LCID",
            "NVDA",
        ]


def _schedule(expected: datetime, ticker: str = "LCID", cik: int = 1811210) -> str:
    return (
        f'[[target]]\nticker = "{ticker}"\ncik = {cik}\nperiod = "FY2026Q3"\n'
        f"expected = {expected.replace(tzinfo=None).isoformat()}\n"
    )


def test_schedules_are_read_as_the_bot_reads_them(tmp_path: Path) -> None:
    (tmp_path / "2026-11-09.toml").write_text(_schedule(NOW), encoding="utf-8")
    # The same release again: the first file wins
    (tmp_path / "2026-11-10.toml").write_text(_schedule(at(60)), encoding="utf-8")
    # Saved by Notepad, with a byte order mark
    (tmp_path / "nvda.toml").write_text(_schedule(at(20), "NVDA", 1045810), encoding="utf-8-sig")
    (tmp_path / "broken.toml").write_text("[[target]]\nticker = ", encoding="utf-8")
    (tmp_path / "notes.txt").write_text(_schedule(at(40), "AAPL", 320193), encoding="utf-8")

    targets, problems = schedule_targets(tmp_path)

    assert {t.ticker: t.expected for t in targets.values()} == {"LCID": NOW, "NVDA": at(20)}
    assert len(problems) == 1 and problems[0].startswith("Can't read broken.toml: ")


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int | str | None, str]:
    monkeypatch.chdir(tmp_path)  # away from a developer's real .env
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setattr(sys, "argv", ["restart", str(tmp_path / "schedules")])
    with pytest.raises(SystemExit) as exit_info:
        restart.main()
    return exit_info.value.code, capsys.readouterr().out


def test_the_check_counts_schedule_files_and_tracked_tickers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schedules = tmp_path / "schedules"
    schedules.mkdir()
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "7")
    now = datetime.now(ET).replace(microsecond=0)

    assert _run(tmp_path, monkeypatch, capsys) == (0, "")

    # A ticker someone tracked, released in 3 minutes
    with DeliveryStore(schedules / ".earnings-state.sqlite3", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA", 1, now)
        calendar.set_target(to_target("NVDA", now + timedelta(minutes=3), 2027, 3), "Confirmed")
    code, out = _run(tmp_path, monkeypatch, capsys)
    assert code == WINDOW_OPEN
    assert out.startswith("NVDA's window is open")

    (schedules / "today.toml").write_text(_schedule(now + timedelta(minutes=2)), encoding="utf-8")
    code, out = _run(tmp_path, monkeypatch, capsys)
    assert code == WINDOW_OPEN
    assert [line.split("'")[0] for line in out.splitlines()] == ["LCID", "NVDA"]


def test_the_check_needs_the_alert_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "schedules").mkdir()
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    code, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == "Set DISCORD_CHANNEL_ID in .env"
