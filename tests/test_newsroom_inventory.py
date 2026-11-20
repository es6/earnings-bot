"""Previously rejected corporate routes survive a working IR feed and reach the real plan."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from first_print import investigate
from first_print.calendar import Calendar
from first_print.date_discovery import DateDiscovery
from first_print.delivery import DeliveryStore
from first_print.known import KnownCompany, SourceCheck, to_target
from first_print.sources import Sighting
from tests.builders import INVESTIGATED
from tests.fakes import FakeDiscord, RoutedFetcher
from tests.test_independent_newsrooms import BODY, TITLE
from tests.test_track_progress import NOW, SPEED, lines_of, lucid_target, tracker

TODAY = date(2026, 11, 20)


def fixture(
    monkeypatch: pytest.MonkeyPatch, name: str, host: str
) -> tuple[KnownCompany, RoutedFetcher]:
    async def releases(fetcher: object, cik: int) -> list[tuple[Sighting, str]]:
        text = (
            f"{name} reports third quarter 2026 results /PRNewswire/. See ir.{host} and www.{host}."
        )
        return [(Sighting("old", "prior", "https://sec.example/old"), text)]

    async def q4(fetcher: object, hostname: str) -> tuple[bool, str, str]:
        return hostname == f"ir.{host}", "PR Newswire", "fixture"

    monkeypatch.setattr(investigate, "_last_releases", releases)
    monkeypatch.setattr(investigate, "_q4_wire", q4)
    company = INVESTIGATED.model_copy(update={"name": name, "names": [name], "sec_form": "8-K"})
    title = TITLE.replace("WeRide", name)
    listing, article = f"https://www.{host}/press", f"https://www.{host}/posts/results"
    fetcher = RoutedFetcher(
        {
            article: (200, BODY.replace(b"WeRide", name.encode())),
            listing: (200, f'<a href="{article}">{title}</a>'.encode()),
            f"https://ir.{host}/": (200, b"IR home"),
            f"https://www.{host}/": (200, b'<a href="/press">Press</a>'),
        }
    )
    return company, fetcher


@pytest.mark.parametrize(
    ("name", "host"), [("WeRide", "weride.ai"), ("Example Mobility", "examplemobility.example")]
)
async def test_working_ir_feed_does_not_end_independent_corporate_discovery(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    host: str,
) -> None:
    company, fetcher = fixture(monkeypatch, name, host)
    found = await investigate.investigate(fetcher, company, TODAY)

    inventory = found.model_dump().get("newsrooms", [])
    assert any(
        r["listing"] == f"https://www.{host}/press" and r["state"] == "verified" for r in inventory
    )
    planned = to_target("TEST", NOW, 2026, 3, found)
    assert any(s.url() == f"https://www.{host}/press" for s in planned.sources())
    assert planned.q4_host == f"ir.{host}"


async def test_empty_newsroom_is_retained_and_revisited_even_when_navigation_drops_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    company, fetcher = fixture(monkeypatch, "WeRide", "weride.ai")
    listing = "https://www.weride.ai/press"
    populated = fetcher.routes[listing]
    fetcher.routes[listing] = 200, b"No recognized earnings article today"
    first = await investigate.investigate(fetcher, company, TODAY)
    assert first.model_dump().get("newsrooms", [{}])[0].get("state") == "unresolved"
    with DeliveryStore(tmp_path / "inventory.db", 1) as store:
        calendar = Calendar(store)
        calendar.add("TEST", first)
    with DeliveryStore(tmp_path / "inventory.db", 1) as store:
        [row] = Calendar(store).entries()
        fetcher.routes["https://www.weride.ai/"] = 200, b"Navigation changed"
        fetcher.routes[listing] = populated
        second = await investigate.investigate(fetcher, row.company, TODAY)
    assert any(s.url() == listing for s in to_target("TEST", NOW, 2026, 3, second).sources())


def room_profile(state: str = "verified") -> KnownCompany:
    return KnownCompany.model_validate(
        INVESTIGATED.model_dump()
        | {
            "newsrooms": [
                {
                    "listing": "https://www.lucidmotors.com/press",
                    "names": ["Lucid"],
                    "state": state,
                    "enabled": state == "verified",
                    "reason": "Publication latency not measured",
                    "discovered_from": "https://www.lucidmotors.com/",
                    "checked": str(TODAY),
                }
            ]
        }
    )


@pytest.mark.parametrize("state", ["verified", "unresolved"])
def test_scorecard_does_not_claim_complete_when_a_candidate_is_unresolved_or_not_scheduled(
    tmp_path: Path,
    state: str,
) -> None:
    planned = lucid_target(ir=True)
    with DeliveryStore(tmp_path / "scorecard.db", 1) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", room_profile(state))
        progress = tracker(store, FakeDiscord(), planned=lambda: {planned.release_key: planned})
        card = progress.render(row, SPEED.to_json(), False)
    assert card.description and "Research complete" not in card.description
    assert "Independent newsroom coverage needs follow-up" in "\n".join(lines_of(card)["Progress"])
    assert "Not in the active plan" in "\n".join(lines_of(card)["Independent newsroom inventory"])


def test_profile_routes_join_existing_schedule_files_and_survive_reopening(tmp_path: Path) -> None:
    planned = lucid_target(ir=True)
    path = tmp_path / "plan.db"
    with DeliveryStore(path, 1) as store:
        calendar = Calendar(store)
        calendar.add("LCID", room_profile())
        calendar.set_target(planned, "Manual override")
    with DeliveryStore(path, 1) as store:
        calendar = Calendar(store)
        result = calendar.plan({planned.release_key: planned})[planned.release_key]
    assert result.ir == planned.ir and result.period == planned.period
    assert any(s.url() == "https://www.lucidmotors.com/press" for s in result.sources())


def test_rejecting_corporate_newsroom_is_not_hidden_by_a_different_working_ir_page(
    tmp_path: Path,
) -> None:
    planned = lucid_target(ir=True)
    company = INVESTIGATED.model_copy(
        update={
            "checks": [
                SourceCheck(
                    source="www.lucidmotors.com press release page",
                    watch=False,
                    why="No recognized article yet",
                )
            ]
        }
    )
    with DeliveryStore(tmp_path / "reject.db", 1) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", company)
        card = tracker(store, FakeDiscord(), planned=lambda: {planned.release_key: planned}).render(
            row, SPEED.to_json(), False
        )
    assert "www.lucidmotors.com" in "\n".join(lines_of(card)["🚫 Checked and rejected"])


def test_unresolved_newsrooms_are_due_daily_instead_of_disappearing_for_a_month() -> None:
    company = room_profile("unresolved")
    assert investigate.due("LCID", company, TODAY + timedelta(days=1))
    assert not investigate.due("LCID", room_profile(), TODAY)


async def test_transient_failure_retains_a_verified_route_but_marks_it_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company, fetcher = fixture(monkeypatch, "WeRide", "weride.ai")
    first = await investigate.investigate(fetcher, company, TODAY)
    fetcher.routes["https://www.weride.ai/press"] = 503, b"temporary failure"
    second = await investigate.investigate(fetcher, first, TODAY)
    inventory = second.model_dump().get("newsrooms", [])
    assert any(r["state"] == "unresolved" and r["enabled"] for r in inventory)
    assert any(
        s.url() == "https://www.weride.ai/press"
        for s in to_target("TEST", NOW, 2026, 3, second).sources()
    )


async def test_research_updates_an_already_pinned_target_without_replacing_its_ir_route(
    tmp_path: Path,
) -> None:
    planned = lucid_target(ir=True)
    with DeliveryStore(tmp_path / "pinned.db", 1) as store:
        calendar = Calendar(store)
        calendar.add("LCID", room_profile())
        calendar.set_target(planned, "Manual override")
        [row] = calendar.entries()
        discovery = DateDiscovery(
            calendar, RoutedFetcher({}), now=lambda: NOW, busy=lambda: False, status=lambda _: None
        )
        await discovery._check(row)
        [updated] = calendar.entries()
    assert updated.target is not None and updated.target.ir == planned.ir
    assert any(s.url() == "https://www.lucidmotors.com/press" for s in updated.target.sources())
