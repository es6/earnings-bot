"""/track debug mode: all of /track's research from scratch, saving nothing live."""

import asyncio
import json
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from earnings_bot import track_test
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.investigate import due
from earnings_bot.issuers import Issuers
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.recorder import ET, Target
from earnings_bot.source_audit import Speed
from earnings_bot.track_test import (
    FOUND,
    MISSING,
    UNSURE,
    TrackReport,
    compare,
    load_key,
    run_track_test,
    save,
)
from earnings_bot.tracking_commands import is_owner
from tests.fakes import RoutedFetcher
from tests.sec_data import LCID_SUBMISSIONS, sec_routes
from tests.test_tracking_commands import NOW, NVDA_CIK, OWNER, client

NVDA_ROUTE = f"data.sec.gov/submissions/CIK{NVDA_CIK:010d}.json"
RELEASE = datetime(2026, 11, 18, 16, 20, tzinfo=ET)
# NVIDIA's last four results, newest first: one came out at 4:25 PM
HISTORY = [RELEASE - timedelta(days=91 * n) for n in range(1, 5)]
HISTORY[1] += timedelta(minutes=5)


def nvda_routes() -> dict[str, tuple[int, bytes]]:
    """SEC as it answers for NVIDIA, which is in the catalog."""
    submissions = {**LCID_SUBMISSIONS, "cik": str(NVDA_CIK), "name": "NVIDIA CORP"}
    return {**sec_routes(), NVDA_ROUTE: (200, json.dumps(submissions).encode())}


@pytest.fixture
def research(monkeypatch: pytest.MonkeyPatch) -> list[KnownCompany]:
    """Research that finds NVIDIA's Nov 18 release on its IR site and its last four release
    times, without the network. Returns the companies research read from its calendar, each
    of which it would investigate."""
    given: list[KnownCompany] = []

    async def run_once(self: DateDiscovery, row: Any) -> None:
        # What research reads back, as its every step does, not just the row it was handed
        stored = self.calendar.find(row.ticker)
        assert stored is not None
        assert due(stored.ticker, stored.company, NOW.date(), self.calendar.catalog)
        given.append(stored.company)
        assert self.busy() is False and self.chrome_live() is False
        target = Target(ticker=row.ticker, cik=row.cik, expected=RELEASE, period="FY2027Q3")
        self.calendar.set_target(target, "the company's IR site")

    async def audit(*args: Any) -> Speed:
        return Speed(day=HISTORY[0].date(), history=HISTORY)

    monkeypatch.setattr(DateDiscovery, "run_once", run_once)
    monkeypatch.setattr(track_test, "audit_sources", audit)
    return given


async def test_a_run_researches_a_catalog_company_as_new_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research: list[KnownCompany]
) -> None:
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(
        track_test.tempfile, "mkdtemp", lambda prefix: str(scratch.mkdir() or scratch)
    )
    live = tmp_path / "schedules"
    live.mkdir()
    fetcher = RoutedFetcher(nvda_routes())

    report = await run_track_test("NVDA", fetcher, live, lambda: NOW)

    # Looked up on SEC like a company the bot has never seen, not read from the catalog
    [company] = research
    assert company.name == report.facts["name"] == "NVIDIA CORP" != known_companies()["NVDA"].name
    assert company.investigated is None and company.checks == []
    assert f"https://{NVDA_ROUTE}" in [url for _, url, _ in fetcher.requests]
    assert report.facts["cik"] == NVDA_CIK
    assert report.facts["next_release"] == "2026-11-18 16:20 ET"
    assert report.facts["period"] == "FY2027Q3"
    assert report.facts["evidence"] == "the company's IR site"
    assert report.facts["release_times"] == ["16:20", "16:25"]
    assert report.facts["usual_time"] == "16:20"
    assert [step.name for step in report.steps] == ["Identify", "Research", "Speed audit"]
    assert any("In the catalog" in note for note in report.notes)
    assert not report.error
    # Its throwaway calendar is gone and the live folder untouched
    assert not scratch.exists()
    assert list(live.iterdir()) == []


async def test_a_ticker_that_cant_be_identified_reports_why(tmp_path: Path) -> None:
    report = await run_track_test("ZZZZ", RoutedFetcher(sec_routes()), tmp_path, lambda: NOW)

    assert "isn't on SEC's list" in report.error
    assert {mark for mark, _, _ in report.checklist()} == {MISSING}


def test_the_checklist_marks_a_date_only_an_outside_calendar_gave_as_unsure() -> None:
    report = TrackReport("NVDA", "2026-11-15T12:00:00+00:00")
    report.facts = track_test._facts(
        KnownCompany.model_validate(
            {
                "name": "NVIDIA",
                "cik": NVDA_CIK,
                "platform": "q4",
                "reachable": True,
                "wire": "GlobeNewswire",
                "fastest": "ir-feed",
                "evidence": "",
                "checked": date(2026, 11, 1),
                "q4_host": "investor.nvidia.com",
            }
        ),
        Target(ticker="NVDA", cik=NVDA_CIK, expected=RELEASE, period="FY2027Q3"),
        "FMP's earnings calendar",
        HISTORY,
    )
    marks = {label: mark for mark, label, _ in report.checklist()}

    assert marks["Next release"] == marks["Period"] == UNSURE
    assert marks["IR site"] == marks["Recent release times"] == FOUND
    assert report.facts["ir_site"] == "https://investor.nvidia.com"


def test_an_answer_key_lists_every_fact_that_differs(tmp_path: Path) -> None:
    (tmp_path / "NVDA.toml").write_text(
        'symbol = "NVDA"\n'
        'wire = "globenewswire"\n'  # any case
        'ir_site = "investor.nvidia.com"\n'  # appears in what was found
        'routes = ["Q4 feed"]\n'  # one of those found
        'next_release = { day = 2026-11-18, time = "16:20" }\n'
        'usual_time = "16:00"\n',
        encoding="utf-8",
    )
    key = load_key("NVDA", tmp_path)
    assert key is not None
    report = TrackReport("NVDA", "2026-11-15T12:00:00+00:00")
    report.facts = {
        "wire": "GlobeNewswire",
        "ir_site": "https://investor.nvidia.com/news",
        "routes": ["SEC 8-K", "Q4 feed"],
        "next_release": "2026-11-18 16:20 ET",
        "usual_time": "16:20",
    }

    assert compare(report, key) == ["Usual release time: expected '16:00', found '16:20'"]

    key["next_release"] = tomllib.loads('v = { day = 2026-11-18, time = "16:30" }')["v"]
    assert len(compare(report, key)) == 2
    assert load_key("AAPL", tmp_path) is None


def test_saved_reports_keep_the_last_twenty_per_symbol(tmp_path: Path) -> None:
    for minute in range(22):
        save(TrackReport("NVDA", f"2026-11-15T12:{minute:02d}:00+00:00"), tmp_path)
    save(TrackReport("LCID", "2026-11-15T12:00:00+00:00"), tmp_path)

    folder = tmp_path / track_test.FOLDER
    kept = sorted(path.name for path in folder.glob("NVDA-*.md"))
    assert len(kept) == 20 and kept[0] == "NVDA-2026-11-15T120200Z.md"
    assert len(list(folder.glob("NVDA-*.json"))) == 20
    assert len(list(folder.glob("LCID-*"))) == 2


async def test_the_owner_is_whoever_owns_the_bots_application_or_its_team() -> None:
    def user(id: int) -> discord.abc.User:
        return cast(discord.abc.User, SimpleNamespace(id=id))

    assert await is_owner(client(), user(OWNER))
    assert not await is_owner(client(), user(1))
    assert await is_owner(client(team=[7, OWNER]), user(7))
    assert not await is_owner(client(owner=7, team=[OWNER]), user(8))


@pytest.mark.parametrize("catalog", [True, False])
async def test_research_investigates_a_catalog_company_only_in_a_test_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: bool
) -> None:
    from earnings_bot import date_discovery

    monkeypatch.setattr(date_discovery, "chrome_available", lambda: False)
    investigated: list[str] = []

    async def investigate(fetcher: Any, company: KnownCompany, *args: Any, **kwargs: Any):
        investigated.append(company.name)
        return company.model_copy(update={"investigated": NOW.date()})

    monkeypatch.setattr(date_discovery, "investigate", investigate)
    sec = await Issuers(RoutedFetcher(nvda_routes())).company("NVDA", NOW.date(), catalog=False)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store, catalog=catalog)
        row, _ = calendar.add("NVDA", sec)
        dates = DateDiscovery(calendar, RoutedFetcher({}), lambda: NOW, lambda: False, print)

        await dates._investigate(row)

    # The live calendar keeps the catalog's researched sources; a test looks for them afresh
    assert investigated == ([] if catalog else ["NVIDIA CORP"])


async def test_a_run_past_its_limit_stops_and_reports_what_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stuck(self: DateDiscovery, row: Any) -> None:
        await asyncio.sleep(3600)  # research waiting behind live windows

    monkeypatch.setattr(DateDiscovery, "run_once", stuck)
    fetcher = RoutedFetcher(nvda_routes())

    run = run_track_test("NVDA", fetcher, tmp_path, lambda: NOW, limit=0.2)
    report = await asyncio.wait_for(run, 5)

    assert report.error == "Stopped at its 0 s limit; the facts are what it found by then"
    assert [step.name for step in report.steps] == ["Identify"]
    assert report.facts["cik"] == NVDA_CIK and report.facts["name"] == "NVIDIA CORP"
    assert "next_release" not in report.facts


async def test_a_network_timeout_is_reported_as_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(self: DateDiscovery, row: Any) -> None:
        raise TimeoutError("SEC took too long")

    monkeypatch.setattr(DateDiscovery, "run_once", slow)

    report = await run_track_test("NVDA", RoutedFetcher(nvda_routes()), tmp_path, lambda: NOW)

    assert report.error == "TimeoutError: SEC took too long"
