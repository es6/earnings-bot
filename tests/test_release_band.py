import asyncio
import io
import json
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from earnings_bot import recorder
from earnings_bot.browser_sources import BrowserRelease
from earnings_bot.recorder import ET, Journal, Polls, Target, Watch, pace_routes, watch
from earnings_bot.release_band import NEAR, SLOW, pace, paced, release_times
from earnings_bot.sources import EdgarFilings, EdgarLatest, RssFeed
from tests.test_recorder import CompanyFeed, FakeFetcher, ListSource

T = datetime(2026, 11, 19, 7, 0, tzinfo=ET)
MINUTE = timedelta(minutes=1)


@pytest.mark.parametrize(
    ("at", "every", "change"),
    [
        (T - 60 * MINUTE, SLOW, 55 * 60.0),  # far from it: slowly, until 5 minutes before
        (T - 4 * MINUTE, NEAR, 2 * 60.0),  # 5 to 2 minutes before: every 5 s
        (T - 2 * MINUTE, 1.0, 12 * 60.0),  # 2 minutes before to 10 after: full speed
        (T + 9 * MINUTE, 1.0, 60.0),
        (T + 10 * MINUTE, NEAR, None),  # a late release: every 5 s until the window closes
    ],
)
def test_a_route_polls_slowly_far_from_the_release_time_and_fast_close_to_it(
    at: datetime, every: float, change: float | None
) -> None:
    assert pace(at, [T], own=1.0) == (every, change)


def test_a_route_never_polls_faster_than_its_own_speed_or_paces_without_times() -> None:
    assert pace(T, [T], own=10.5) == (10.5, 600.0)  # Q4's financial reports, every 10.5 s
    assert pace(T - 4 * MINUTE, [T], own=6.0)[0] == 6.0
    assert pace(T - 60 * MINUTE, [], own=1.0) == (1.0, None)


def test_between_two_release_times_a_route_polls_slowly_again() -> None:
    """WeRide released at 4:00 and 7:00 AM in different quarters."""
    times = [T.replace(hour=4), T]
    assert pace(T.replace(hour=4, minute=5), times, own=2.0)[0] == 2.0
    assert pace(T.replace(hour=5), times, own=2.0) == (SLOW, (T - T.replace(hour=5)).seconds - 300)
    assert pace(T + 11 * MINUTE, times, own=2.0) == (NEAR, None)


def test_every_route_but_secs_follows_the_release_times() -> None:
    assert paced(BrowserRelease("https://ir.example.com/news", "third quarter"))
    assert paced(RssFeed("rss:ir.example.com", "https://ir.example.com/rss"))
    assert paced(RssFeed("globenewswire_busted", "https://gn.example/rss"))
    assert paced(RssFeed("businesswire_busted", "https://bw.example/rss"))
    assert not paced(EdgarFilings(6951))
    assert not paced(EdgarLatest(6951))


def test_recent_release_times_are_placed_on_the_release_day_in_its_part_of_the_day() -> None:
    history = [
        datetime(2026, 8, 7, 7, 0, 12, tzinfo=ET),
        datetime(2026, 5, 8, 7, 30, tzinfo=ET),
        datetime(2026, 2, 5, 16, 5, tzinfo=ET),  # after the close: another part of the day
        datetime(2025, 11, 20, 7, 0, tzinfo=ET),
        datetime(2025, 8, 7, 6, 30, tzinfo=ET),  # the fifth most recent: too old
    ]
    assert release_times(history, T) == [T, T.replace(minute=30)]
    assert release_times([], T) == [T]


async def test_a_watch_polls_slowly_then_speeds_up_on_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nebius's newsroom refused us on Nov 12 after an hour of polling once a second."""
    monkeypatch.setattr(recorder, "pace", pace_scaled)
    sent: list[float] = []

    class Timed(FakeFetcher):
        async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            sent.append(time.monotonic())
            return await super().request(method, url, *args, **kwargs)

    journal = Journal(io.StringIO())
    started = time.monotonic()
    w = Watch("ROAD", ListSource(), times=[journal.now() + timedelta(seconds=0.35)])

    await watch(w, Timed((200, "old")), journal, end=journal.now() + timedelta(seconds=0.6))

    slow = [t - started for t in sent if t - started < 0.34]
    fast = [t - started for t in sent if t - started >= 0.34]
    assert len(slow) == 2  # at 0 and 0.2 s, against about 34 at full speed
    assert fast and fast[0] < 0.40  # full speed from 0.35 s, not at the next slow poll
    assert len(fast) > 10


def pace_scaled(at: datetime, times: list[datetime], own: float) -> tuple[float, float | None]:
    """Like pace, with seconds for minutes: slow polls 0.2 s apart until the release time, then
    the route's own speed."""
    [moment] = times
    if at < moment:
        return 0.2, (moment - at).total_seconds()
    return own, None


async def test_a_shared_feed_polls_as_fast_as_its_fastest_window_needs() -> None:
    """HD's and Klarna's windows overlap. One company's release time mustn't slow another's poll
    of the same wire feed."""
    journal = Journal(io.StringIO())
    now = journal.now()
    far = [now + timedelta(hours=2)]
    near = [now + timedelta(seconds=30)]

    slow_only = FakeFetcher((200, ""))
    await Polls(slow_only, journal).watch(
        Watch("HD", CompanyFeed("HD"), times=far), now + timedelta(seconds=0.2)
    )
    assert slow_only.sent == 1  # the next poll would be 15 s later

    both = FakeFetcher((200, ""))
    polls = Polls(both, journal)
    end = journal.now() + timedelta(seconds=0.2)
    await asyncio.gather(
        polls.watch(Watch("HD", CompanyFeed("HD"), times=far), end),
        polls.watch(Watch("KLAR", CompanyFeed("KLAR"), times=near + far), end),
        polls.watch(Watch("NVDA", CompanyFeed("NVDA")), end),  # no release times: full speed
    )
    assert both.sent > 5


def test_a_window_paces_every_route_but_secs_by_the_release_times() -> None:
    target = Target(ticker="ROAD", cik=1718227, expected=T, release_times=[T.replace(minute=30)])
    listing = BrowserRelease("https://ir.example.com/news", "fourth quarter")
    wire = RssFeed("prnewswire_busted", "https://prn.example/rss")
    watches = [Watch("ROAD", s) for s in (listing, wire, EdgarFilings(1718227))]
    log = io.StringIO()

    pace_routes(target, watches, Journal(log))

    assert watches[0].times == watches[1].times == [T, T.replace(minute=30)]
    assert watches[2].times == []
    [event] = [json.loads(line) for line in log.getvalue().splitlines()]
    assert event["event"] == "polling_band"
    assert event["paced_routes"] == ["browser-ir", "prnewswire_busted"]


async def test_a_window_paces_its_routes_from_the_moment_it_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def synced(journal: Journal, periodic: bool = False) -> bool:
        return True

    monkeypatch.setattr(recorder, "sync_clock", synced)
    log = io.StringIO()
    journal = Journal(log)
    now = journal.now()
    later = now + timedelta(hours=1)
    target = Target(
        ticker="ROAD",
        cik=1718227,
        expected=now,
        release_times=[later],
        before_minutes=1,
        after_minutes=0.005,  # closes 0.3 s from now
        rss=["https://ir.example.com/rss"],
    )
    started: list[Watch] = []

    await recorder.record_target(target, FakeFetcher((200, "")), journal, on_watches=started.extend)

    by_name = {w.source.name: w for w in started}
    assert by_name["rss:ir.example.com"].times == [now, later]
    assert all(w.times == [] for w in started if not paced(w.source))
    assert any(json.loads(line)["event"] == "polling_band" for line in log.getvalue().splitlines())


def test_a_company_releasing_by_a_clock_without_daylight_saving_is_placed_by_it_too() -> None:
    """WeRide releases in Hong Kong's evening: 4:00 to 7:10 AM ET in its last four quarters, which
    in November is 4:00 to 6:10 AM by the Hong Kong clock."""
    history = [
        datetime(2026, 8, 12, 7, 0, 18, tzinfo=ET),
        datetime(2026, 5, 13, 5, 40, tzinfo=ET),
        datetime(2026, 3, 23, 7, 10, tzinfo=ET),
        datetime(2025, 11, 24, 4, 0, tzinfo=ET),
    ]
    friday = datetime(2026, 11, 20, 7, tzinfo=ET)
    clock = [(4, 0), (4, 40), (5, 40), (6, 0), (6, 10), (7, 0), (7, 10)]
    assert release_times(history, friday) == [friday.replace(hour=h, minute=m) for h, m in clock]
    # A US company at the same Eastern time all year gains nothing from it
    nvidia = [datetime(2026, 8, 26, 16, 20, tzinfo=ET), datetime(2026, 2, 25, 16, 20, tzinfo=ET)]
    tuesday = datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    assert release_times(nvidia, tuesday) == [tuesday]
