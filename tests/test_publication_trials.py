"""Record-only publication trials (plan item 8): they journal what each route showed and when,
take their turns behind monitoring, stop at a durable request budget, and never post."""

import asyncio
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from first_print import fetch, unattended
from first_print.delivery import DeliveryStore
from first_print.fetch import Fetcher, Response, ResponseTooLarge
from first_print.pipeline import Releases
from first_print.publication_trials import load_plans, run_trials
from first_print.recorder import Journal
from tests.builders import events, plan
from tests.fakes import FakePoster, QueuedFetcher

RELEASE = (
    "Example Corp Reports Third Quarter 2026 Results\n"
    "Example Corp today announced financial results for the third quarter ended September 30, "
    "2026.\n"
    + "Revenue was $5.0 billion, up 10% compared with the third quarter of 2025.\n" * 30
    + "Condensed Consolidated Statements of Operations\n(in millions, except per share data)\n"
    + (
        "Revenue | 5,000 | 4,545\nNet income | 900 | 800\n"
        "Diluted earnings per share | $1.20 | $1.05\n"
    )
    * 3
)


@pytest.fixture(autouse=True)
def quick_host_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts hand out a turn every second; shrink that so tests don't wait in real time."""
    monkeypatch.setattr(fetch, "_DEFAULT_GAP", 0.02)


async def test_a_trial_records_a_transition_and_stops_at_its_budget() -> None:
    fetcher = QueuedFetcher(
        {
            "/news/q3-2026": [b"<p>Not found yet</p>", RELEASE.encode()],
            "/news": [b"<p>Older news</p>", RELEASE.encode()],
        }
    )
    log = io.StringIO()
    with closing(sqlite3.connect(":memory:")) as db, db:
        await run_trials(plan(), fetcher, Journal(log), db)

    seen = events(log, "publication_trial")
    assert len(seen) == 4  # the plan's request budget, then it ends
    assert events(log, "publication_trial_ended")[0]["turns"] == 4
    article = [row for row in seen if row["route"] == "article"]
    assert not article[0]["candidate_visible"]
    # The release showed up after a poll that didn't have it: a measured transition
    assert any(row["transition_observed"] and row["usable_report"] for row in article)
    assert all("queued_ms" in row and "network_ms" in row for row in seen)


async def test_a_release_already_up_at_the_first_poll_is_not_a_measured_transition() -> None:
    fetcher = QueuedFetcher({"example.com": [RELEASE.encode()]})
    log = io.StringIO()
    with closing(sqlite3.connect(":memory:")) as db, db:
        await run_trials(plan(), fetcher, Journal(log), db)
    seen = events(log, "publication_trial")
    article = [row for row in seen if row["route"] == "article"]
    assert len(article) >= 1 and all(row["usable_report"] for row in article)
    assert not any(row["transition_observed"] for row in seen)


async def test_a_trial_budget_survives_a_restart_and_its_id_cant_be_reused(tmp_path: Path) -> None:
    fetcher = QueuedFetcher({"example.com": [RELEASE.encode()]})
    path = tmp_path / "trials.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        await run_trials(plan(max_requests=3), fetcher, Journal(io.StringIO()), db)
    log = io.StringIO()
    with closing(sqlite3.connect(path)) as db, db:
        await run_trials(plan(max_requests=3), fetcher, Journal(log), db)
        assert events(log, "publication_trial") == []  # the budget was already spent
        other = io.StringIO()
        await run_trials(plan(max_requests=3, duration_seconds=6), fetcher, Journal(other), db)
        assert events(other, "publication_trial_error")  # same ID, different plan


async def test_trial_turns_wait_behind_monitoring_polls() -> None:
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[str] = []

    async def take(name: str, observed: bool) -> None:
        if observed:
            with fetcher.observing(lambda host: None, lambda: None):
                await fetcher.wait_turn("ir.example.com")
        else:
            await fetcher.wait_turn("ir.example.com")
        turns.append(name)

    await take("first poll", False)
    trial = asyncio.create_task(take("trial", True))
    await asyncio.sleep(0)
    polls = [asyncio.create_task(take(f"poll {i}", False)) for i in range(2)]
    await asyncio.gather(trial, *polls)
    assert turns == ["first poll", "poll 0", "poll 1", "trial"]


async def test_the_bot_runs_trial_plans_from_its_schedule_folder(tmp_path: Path) -> None:
    (tmp_path / "trials").mkdir()
    (tmp_path / "trials" / "q3.json").write_text(plan().model_dump_json(), encoding="utf-8")
    (tmp_path / "trials" / "broken.json").write_text('{"id": "x"}', encoding="utf-8")
    plans, problems = load_plans(tmp_path)
    assert [p.id for p in plans] == ["example-q3"] and problems[0].startswith("broken.json")

    fetcher = QueuedFetcher({"example.com": [RELEASE.encode()]})
    log = io.StringIO()
    journal = Journal(log)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, Releases(FakePoster, fetcher, journal, None), store, print
        )
        await runner.scan()
        await runner.scan()  # each plan starts once
        assert list(runner.trials) == ["example-q3"]
        await asyncio.wait_for(runner.trials["example-q3"], 10)
        if runner.trial_db:
            runner.trial_db.close()
    assert len(events(log, "publication_trial")) == 4
    assert len(events(log, "publication_trial_error")) == 1  # the broken plan, reported once
    assert "posted" not in {json.loads(line)["event"] for line in log.getvalue().splitlines()}
    assert (tmp_path / ".publication-trials.sqlite3").exists()


async def test_a_trial_s_options_reach_its_requests_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan's byte cap stops an HTTP read, and a Chrome route's cache mode and service-worker
    bypass reach the browser. Monitoring's requests keep their usual options."""

    async def respond(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    app = web.Application()
    app.router.add_get("/big", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/big"
    asked: list[dict[str, Any]] = []

    class Browser:
        async def request(self, *args: Any, **options: Any) -> Response:
            asked.append(options)
            return Response(args[0], 200, {}, b"", 0.01)

        async def close(self) -> None:
            pass

    try:
        async with Fetcher("Test Person test@example.com") as fetcher:
            monkeypatch.setattr(fetcher, "_browser", Browser())
            assert len((await fetcher.request("GET", url)).body) == 5000
            await fetcher.browser_request(url, url, 8)
            options = {"cache_mode": "reload", "bypass_service_worker": True}
            with fetcher.observing(lambda host: None, lambda: None, max_bytes=4096, **options):
                with pytest.raises(ResponseTooLarge):
                    await fetcher.request("GET", url)
                await fetcher.browser_request(url, url, 8)
    finally:
        await runner.cleanup()
    monitoring, trial = asked
    assert monitoring["cache_mode"] == "no-store" and not monitoring["observational"]
    assert trial | options == trial and trial["observational"] and trial["max_bytes"] == 4096
