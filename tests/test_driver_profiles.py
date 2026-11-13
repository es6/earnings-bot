import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from earnings_bot import driver_profiles
from earnings_bot.calendar import Calendar, Tracked
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.driver_profiles import DriverProfiles, ProfileReply, ProfileSource
from earnings_bot.sec_results import SixKResults
from tests.fakes import RoutedFetcher
from tests.test_driver_profile import SOURCE, TODAY, content

NOW = datetime(2026, 11, 13, 15, tzinfo=UTC)
RELEASE = "Acme Announces Results for Third Quarter 2026\n" + SOURCE


def source() -> ProfileSource:
    return ProfileSource(RELEASE, "https://issuer.example/results", NOW - timedelta(days=5))


async def test_track_research_builds_and_reuses_a_durable_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    async def latest(*args: object) -> ProfileSource:
        return source()

    async def write(request: dict) -> ProfileReply:
        calls.append(request)
        return ProfileReply(content(), 1000, 300)

    async def checked(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(driver_profiles, "latest_profile_source", latest)
    db = tmp_path / "state.db"
    for _ in range(2):
        with DeliveryStore(db, 7) as store:
            calendar, fetcher = Calendar(store), RoutedFetcher({})
            row, _ = calendar.add("UBER")
            profiles = DriverProfiles(
                store, fetcher, SixKResults(fetcher), write, lambda cik: False
            )
            dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
            dates.profiles = profiles
            monkeypatch.setattr(dates, "_check", checked)
            monkeypatch.setattr(dates, "_prepare_cache", checked)
            await dates.run_once(row)
            await dates.run_once(row)
            context = profiles.context(row.cik, NOW)["advisory_prior_release_profile"]
            assert context["source_period"] == "FY2026Q3"
            assert context["reviewed"] == TODAY.isoformat()
            assert context["business"]["evidence"] in RELEASE
            assert profiles.context(99999999, NOW) == {}
            assert profiles.context(row.cik, NOW + timedelta(days=121)) == {}
    assert len(calls) == 1  # restart and repeated /track checks make no second paid call


async def test_a_window_opening_during_profile_source_retrieval_defers_the_paid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = False

    async def latest(*args: object) -> ProfileSource:
        nonlocal paused
        paused = True
        return source()

    async def write(request: dict) -> ProfileReply:
        raise AssertionError("Paid work started after the release priority interval began")

    monkeypatch.setattr(driver_profiles, "latest_profile_source", latest)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        row, _ = Calendar(store).add("UBER")
        fetcher = RoutedFetcher({})
        profiles = DriverProfiles(store, fetcher, SixKResults(fetcher), write, lambda cik: paused)
        await profiles.ensure(row, NOW)
        assert profiles.checked[row.cik] == NOW + timedelta(minutes=10)
        assert store.db.execute("SELECT COUNT(*) FROM driver_profile_calls").fetchone()[0] == 0


async def test_an_uncertain_profile_call_is_not_repeated_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    calls = 0

    async def latest(*args: object) -> ProfileSource:
        return source()

    async def write(request: dict) -> ProfileReply:
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Future()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(driver_profiles, "latest_profile_source", latest)
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        row, _ = Calendar(store).add("UBER")
        fetcher = RoutedFetcher({})
        profiles = DriverProfiles(store, fetcher, SixKResults(fetcher), write, lambda cik: False)
        task = asyncio.create_task(profiles.ensure(row, NOW))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with DeliveryStore(db, 7) as store:
        profiles = DriverProfiles(store, fetcher, SixKResults(fetcher), write, lambda cik: False)
        await profiles.ensure(row, NOW + timedelta(days=1))
        assert calls == 1 and profiles.context(row.cik, NOW) == {}
        call = store.db.execute("SELECT * FROM driver_profile_calls").fetchone()
        assert call["status"] == "sent" and call["reserved_micros"] > 6000


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "end_turn"])
async def test_an_incomplete_profile_does_not_replace_previous_valid_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    from tests.test_driver_profile import parsed

    async def latest(*args: object) -> ProfileSource:
        return source()

    async def write(request: dict) -> ProfileReply:
        return ProfileReply("{}", 1000, 300, stop=stop)

    monkeypatch.setattr(driver_profiles, "latest_profile_source", latest)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        row, _ = Calendar(store).add("UBER")
        fetcher = RoutedFetcher({})
        profiles = DriverProfiles(store, fetcher, SixKResults(fetcher), write, lambda cik: False)
        profiles.profiles.claim(row.cik, "a" * 64, TODAY, 30_000)
        profiles.profiles.finish(row.cik, "a" * 64, 1000, parsed())
        await profiles.ensure(row, NOW)
        assert profiles.profiles.get(row.cik, TODAY) == parsed()
        assert "release alone" in profiles.notes[row.cik]


async def test_deferred_research_cannot_start_profile_source_retrieval(tmp_path: Path) -> None:
    async def write(request: dict) -> ProfileReply:
        raise AssertionError("Deferred research made a paid call")

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        row: Tracked = Calendar(store).add("UBER")[0]
        fetcher = RoutedFetcher({})
        profiles = DriverProfiles(store, fetcher, SixKResults(fetcher), write, lambda cik: False)
        with fetcher.background(chrome=True):
            fetcher.defer_research()
            await profiles.ensure(row, NOW)
        assert fetcher.requests == [] and row.cik not in profiles.checked
