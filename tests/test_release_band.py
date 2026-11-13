import io
import json
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from earnings_bot import recorder
from earnings_bot.browser_sources import BrowserRelease
from earnings_bot.recorder import ET, Journal, Target, Watch, slow_until_usual_time, watch
from earnings_bot.release_band import full_speed_from, issuer_route, results_minutes, usual_start
from earnings_bot.sources import EdgarFilings, EdgarLatest, RssFeed
from tests.fakes import RoutedFetcher
from tests.test_recorder import FakeFetcher, ListSource

# AMAT's last eight results 8-Ks, accepted 4:02 to 4:05 PM ET (measured Nov 12, 2026)
AMAT_TIMES = ["16:02", "16:03", "16:03", "16:03", "16:03", "16:03", "16:05", "16:05"]


def submissions(times: list[str], form: str = "8-K", items: str = "2.02,9.01") -> bytes:
    accepted = []
    for n, hhmm in enumerate(times):
        day = datetime(2026, 8, 14, tzinfo=ET) - timedelta(days=91 * n)
        hour, minute = map(int, hhmm.split(":"))
        accepted.append(day.replace(hour=hour, minute=minute).isoformat())
    # A filing that isn't results, which doesn't count
    forms, all_items = [form] * len(times) + ["8-K"], [items] * len(times) + ["5.07"]
    accepted.append(datetime(2026, 6, 2, 11, 0, tzinfo=ET).isoformat())
    recent = {"form": forms, "items": all_items, "acceptanceDateTime": accepted}
    return json.dumps({"filings": {"recent": recent}}).encode()


def minutes(*times: str) -> list[int]:
    return [int(t[:2]) * 60 + int(t[3:]) for t in times]


def test_results_filings_give_the_minutes_of_the_same_half_of_the_day() -> None:
    data = json.loads(submissions(["16:02", "07:01", "16:05"]))
    assert results_minutes(data, afternoon=True) == minutes("16:02", "16:05")
    assert results_minutes(data, afternoon=False) == minutes("07:01")


def test_the_usual_time_covers_six_of_the_last_eight_releases() -> None:
    assert usual_start(minutes(*AMAT_TIMES)) == minutes("16:02")[0]
    # One late quarter doesn't move it (MCD filed at 4:02 PM once, otherwise 7:01 AM)
    late = ["07:01"] * 6 + ["07:02", "07:40"]
    assert usual_start(minutes(*late)) == minutes("07:01")[0]
    # ROAD's last eight ran from 7:40 to 9:34 AM: there is no usual time
    road = ["07:40", "07:44", "08:04", "08:34", "09:10", "09:10", "09:17", "09:34"]
    assert usual_start(minutes(*road)) is None
    assert usual_start(minutes("16:01", "16:01", "16:02")) is None  # too few


async def test_full_speed_starts_a_minute_before_the_usual_or_expected_time() -> None:
    fetcher = RoutedFetcher({"CIK0000006951": (200, submissions(AMAT_TIMES))})
    expected = datetime(2026, 11, 12, 16, 5, tzinfo=ET)

    assert await full_speed_from(fetcher, 6951, "8-K", expected) == expected.replace(minute=1)
    # An expected time before the usual one wins
    early = expected.replace(minute=0)
    assert await full_speed_from(fetcher, 6951, "8-K", early) == early - timedelta(minutes=1)
    # A 6-K comes hours after its release, so it tells nothing; neither does a failed lookup
    assert await full_speed_from(fetcher, 6951, "6-K", expected) is None
    assert await full_speed_from(RoutedFetcher({}), 6951, "8-K", expected) is None


def test_only_the_issuers_own_routes_slow_down() -> None:
    listing = BrowserRelease("https://ir.example.com/news", "third quarter")
    assert issuer_route(listing)
    assert issuer_route(RssFeed("rss:ir.example.com", "https://ir.example.com/rss"))
    assert not issuer_route(RssFeed("globenewswire_busted", "https://gn.example/rss"))
    assert not issuer_route(RssFeed("businesswire_busted", "https://bw.example/rss"))
    assert not issuer_route(EdgarFilings(6951))
    assert not issuer_route(EdgarLatest(6951))


async def test_a_route_polls_slowly_until_its_full_speed_time_and_switches_on_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nebius's newsroom refused us on Nov 12 after an hour of polling once a second."""
    monkeypatch.setattr(recorder, "SLOW_INTERVAL", 0.2)
    sent: list[float] = []

    class Timed(FakeFetcher):
        async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            sent.append(time.monotonic())
            return await super().request(method, url, *args, **kwargs)

    journal = Journal(io.StringIO())
    started = time.monotonic()
    w = Watch("AMAT", ListSource(), slow_until=journal.now() + timedelta(seconds=0.35))

    await watch(w, Timed((200, "old")), journal, end=journal.now() + timedelta(seconds=0.6))

    slow = [t - started for t in sent if t - started < 0.34]
    fast = [t - started for t in sent if t - started >= 0.34]
    assert len(slow) == 2  # at 0 and 0.2 s, against about 34 at full speed
    assert fast and fast[0] < 0.40  # full speed from 0.35 s, not at the next slow poll
    assert len(fast) > 10


async def test_a_window_slows_only_issuer_routes_and_a_failed_lookup_slows_none() -> None:
    target = Target(ticker="AMAT", cik=6951, expected=datetime.now(ET) + timedelta(minutes=9))
    listing = BrowserRelease("https://ir.example.com/news", "fourth quarter")
    wire = RssFeed("globenewswire_busted", "https://gn.example/rss")
    watches = [Watch("AMAT", s) for s in (listing, wire, EdgarFilings(6951))]
    log = io.StringIO()
    usual = datetime.now(ET) + timedelta(minutes=5)
    history = [f"{usual:%H:%M}"] * 8
    fetcher = RoutedFetcher({"CIK0000006951": (200, submissions(history))})

    await slow_until_usual_time(target, watches, fetcher, Journal(log))

    assert watches[0].slow_until == usual.replace(second=0, microsecond=0) - timedelta(minutes=1)
    assert watches[1].slow_until is None and watches[2].slow_until is None
    [event] = [json.loads(line) for line in log.getvalue().splitlines()]
    assert (event["event"], event["slow_routes"]) == ("polling_band", ["browser-ir"])

    class Broken(RoutedFetcher):
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("no answer")

    fresh = [Watch("AMAT", listing)]
    await slow_until_usual_time(target, fresh, Broken({}), Journal(io.StringIO()))
    assert fresh[0].slow_until is None
