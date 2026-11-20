"""Offline provider fixtures, real SQLite reservations, no external requests."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from first_print.calendar import Calendar, Tracked
from first_print.consensus_prefetch import ConsensusPrefetch
from first_print.date_discovery import DateDiscovery
from first_print.delivery import DeliveryStore
from first_print.known import KnownCompany
from first_print.recorder import Target
from tests.fakes import RoutedFetcher

CALENDAR = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
    "EX,Example,2026-11-20,2026-10-31,3.83,USD,pre-market\n"
    "OTHER,Other,2026-11-20,2026-10-31,1.5,USD,pre-market\n"
)


def tracked(symbol: str = "EX") -> Tracked:
    target = Target(
        ticker=symbol, cik=123, period="FY2027Q3", expected=datetime(2026, 11, 20, 9, tzinfo=UTC)
    )
    company = KnownCompany(
        name="Example",
        cik=123,
        platform="unknown",
        reachable=False,
        wire="unknown",
        fastest="unknown",
        evidence="test fixture",
        checked=date(2026, 11, 19),
    )
    return Tracked(symbol, 123, target, None, company)


class Provider:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 22, tzinfo=UTC)
        self.calls: list[tuple[datetime, str, dict[str, str]]] = []
        self.calendar = CALENDAR
        self.notice = ""
        self.error = False
        self.paused = False

    async def get(self, key: str, function: str, params: dict[str, str]) -> str:
        self.calls.append((self.now, function, params))
        self.now += timedelta(seconds=3)
        if self.error:
            raise ValueError("secret-bearing transport error must not be logged")
        if self.notice:
            return self.notice
        if function == "EARNINGS_CALENDAR":
            return self.calendar
        return json.dumps(
            {
                "symbol": params["symbol"],
                "estimates": [
                    {
                        "date": "2026-10-31",
                        "horizon": "fiscal quarter",
                        "revenue_estimate_average": "42000000000",
                        "revenue_estimate_analyst_count": "12",
                        "eps_estimate_average": "3.83",
                        "eps_estimate_analyst_count": "12",
                    },
                    {
                        "date": "2027-01-31",
                        "horizon": "fiscal year",
                        "revenue_estimate_average": "170000000000",
                        "revenue_estimate_analyst_count": "12",
                    },
                ],
            }
        )

    def service(self, store: DeliveryStore) -> ConsensusPrefetch:
        return ConsensusPrefetch(store, "fake-key", lambda: self.now, lambda: self.paused, self.get)


async def test_prefetch_cache_restarts_shared_servers_currency_and_hot_window(
    tmp_path: Path,
) -> None:
    provider, row = Provider(), tracked()
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        service = provider.service(store)
        await service.ensure(row, provider.now)
        assert len(provider.calls) == 2
        assert provider.calls[1][0] - provider.calls[0][0] >= timedelta(seconds=2)
        assert row.target is not None
        snapshot = service.context(row.target)["consensus_snapshot"]
        assert snapshot["symbol"] == "EX" and len(snapshot["estimates"]) == 2
        assert snapshot["estimates"][1]["basis"] == ""
        assert "fake-key" not in json.dumps(snapshot)
        await service.ensure(row, provider.now)
        assert len(provider.calls) == 2
    with DeliveryStore(tmp_path / "state.db", 2) as store:
        resumed = provider.service(store)
        assert resumed.context(row.target) == {"consensus_snapshot": snapshot}
        await resumed.ensure(row, provider.now)
        assert len(provider.calls) == 2
        provider.paused = True
        await resumed.ensure(tracked("OTHER"), provider.now)
        assert len(provider.calls) == 2
        provider.now += timedelta(days=2)
        assert resumed.context(row.target) == {}


@pytest.mark.parametrize("notice", ['{"Information":"quota"}', '{"Note":"slow down"}', "error"])
async def test_first_refusal_stops_requests_across_restart_and_never_logs_key(
    tmp_path: Path,
    notice: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = Provider()
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        service = provider.service(store)
        assert await service._ask("EARNINGS_CALENDAR", {}) == CALENDAR
        provider.notice, provider.error = notice, notice == "error"
        assert await service._ask("EARNINGS_CALENDAR", {}) is None
        assert len(provider.calls) == 2
    with DeliveryStore(tmp_path / "state.db", 2) as store:
        service = provider.service(store)
        assert await service._ask("EARNINGS_ESTIMATES", {"symbol": "EX"}) is None
        assert len(provider.calls) == 2
    assert "fake-key" not in caplog.text and "secret-bearing" not in caplog.text


async def test_quota_is_reserved_before_call_and_shared_by_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("first_print.consensus_prefetch.DAILY_CALLS", 2)
    provider = Provider()
    with (
        DeliveryStore(tmp_path / "state.db", 1) as one,
        DeliveryStore(tmp_path / "state.db", 2) as two,
    ):
        first, second = provider.service(one), provider.service(two)
        await first.ensure(tracked(), provider.now)
        assert len(provider.calls) == 2
        await second.ensure(tracked("OTHER"), provider.now)
        assert len(provider.calls) == 2
        assert one.db.execute("SELECT COUNT(*) FROM consensus_calls").fetchone()[0] == 2
        provider.now += timedelta(days=1, seconds=1)
        assert await second._ask("EARNINGS_CALENDAR", {}) == CALENDAR
        assert len(provider.calls) == 3


async def test_missing_currency_and_calendar_identity_cannot_become_a_snapshot(
    tmp_path: Path,
) -> None:
    provider, row = Provider(), tracked()
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        service = provider.service(store)
        await service.ensure(row, provider.now)
        assert row.target is not None and service.context(row.target)
    for index, calendar in enumerate(
        (CALENDAR.replace(",USD,", ",,"), CALENDAR.replace("2026-11-20", "2026-11-21"))
    ):
        provider = Provider()
        provider.calendar = calendar
        with DeliveryStore(tmp_path / f"unverified-{index}.db", 1) as store:
            service = provider.service(store)
            await service.ensure(row, provider.now)
            assert service.context(row.target) == {}


async def test_cancellation_keeps_reservation_and_pacing_prevents_burst(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    provider = Provider()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        provider.now += timedelta(seconds=seconds)

    async def cancelled(key: str, function: str, params: dict[str, str]) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr("first_print.consensus_prefetch.asyncio.sleep", sleep)
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        service = provider.service(store)
        assert await service._ask("EARNINGS_CALENDAR", {}) == CALENDAR
        service.get = cancelled
        with pytest.raises(asyncio.CancelledError):
            await service._ask("EARNINGS_CALENDAR", {})
        service.get = provider.get
        assert await service._ask("EARNINGS_CALENDAR", {}) == CALENDAR
        assert slept and min(slept) >= 2
        assert store.db.execute("SELECT COUNT(*) FROM consensus_calls").fetchone()[0] == 3


async def test_date_preflight_prepares_estimates_without_touching_live_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, row = Provider(), tracked()
    assert row.target is not None
    with DeliveryStore(tmp_path / "state.db", 1) as store:
        calendar = Calendar(store, catalog=False)
        calendar.add(row.ticker, row.company)
        calendar.set_target(row.target, "Manual override")
        dates = DateDiscovery(
            calendar,
            RoutedFetcher({}),
            lambda: provider.now,
            lambda: provider.paused,
            lambda _: None,
        )

        async def prepared(*args: object, **kwargs: object) -> None:
            pass

        monkeypatch.setattr(dates, "_check", prepared)
        dates.consensus = provider.service(store)
        await dates.run_once(row)
        assert len(provider.calls) == 2
        assert dates.consensus.context(row.target)
        provider.paused = True
        await dates.run_once(row)
        assert len(provider.calls) == 2


async def test_no_snapshot_can_finish_after_its_cutoff(tmp_path: Path) -> None:
    provider, row = Provider(), tracked()
    assert row.target is not None
    with DeliveryStore(tmp_path / "first.db", 1) as store:
        service = provider.service(store)
        await service.ensure(row, provider.now)
        assert service.context(row.target)
    provider = Provider()
    get = provider.get

    async def late(key: str, function: str, params: dict[str, str]) -> str:
        result = await get(key, function, params)
        if function == "EARNINGS_ESTIMATES":
            assert row.target is not None
            provider.now = row.target.window[0]
        return result

    with DeliveryStore(tmp_path / "late.db", 1) as store:
        service = provider.service(store)
        service.get = late
        await service.ensure(row, provider.now)
        assert len(provider.calls) == 2 and not service.context(row.target)
