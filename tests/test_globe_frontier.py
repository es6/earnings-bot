import asyncio
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from first_print import globe_frontier, recorder, unattended
from first_print.delivery import DeliveryStore
from first_print.fetch import Fetcher, Response
from first_print.globe_frontier import (
    Frontier,
    GlobeBatch,
    Page,
    guess_release,
    guessed_for,
    is_release,
    run_batch,
    spans,
)
from first_print.pipeline import Releases
from first_print.recorder import ET, Journal, Target, Trial
from tests.fakes import FakePoster

FRIDAY = datetime(2026, 11, 20, 7, tzinfo=ET)
NUMBER = re.compile(r"/news-release/\d{4}/\d{2}/\d{2}/(\d+)/0/en/([^/]+)\.html$")


def page(title: str, tickers: str, story: str = "") -> bytes:
    """A GlobeNewswire article page, as far as the guessing reads it."""
    return (
        f'<html><head><meta name="ticker" content="{tickers}" />'
        f'<meta property="og:title" content="{title}" />'
        '<script>{"datePublished":"2026-11-20T11:00:00Z"}</script></head>'
        f"<body>{''.join(f'<p>Industry News {n}</p>' for n in range(46))}{story}"
        f"{'<p>The fleet grew in every market this quarter.</p>' * 30}</body></html>"
    ).encode()


WERIDE_Q3 = page(
    "Global Expansion Accelerates: Record YoY Revenue Growth",
    "Nasdaq:WRD, HKEX:0800",
    "<p>NEW YORK, Nov. 20, 2026 (GLOBE NEWSWIRE) -- WeRide Inc. today announced its unaudited "
    "financial results for the third quarter of 2026.</p><p>Total revenue grew 80% to RMB300.0 "
    "million.</p>",
)
OTHER = page("Acme Announces Third Quarter 2026 Results", "NYSE:ACME")


class Frontier_(Fetcher):
    """GlobeNewswire's feed, whose newest article is 100, and article pages that open once
    they've been asked for a set number of times: {number: (asks before it opens, page)}."""

    def __init__(self, pages: dict[int, tuple[int, bytes]], refuse: int | None = None) -> None:
        super().__init__("Test Person test@example.com")
        self.pages = pages
        self.refuse = refuse
        self.asked: dict[int, int] = {}
        self.urls: list[str] = []

    async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Response:
        self.urls.append(url)
        if "RssFeed/orgclass/1" in url:
            feed = "<rss><channel><item><link>https://www.globenewswire.com/news-release/2026/11/20/100/0/en/a.html</link></item></channel></rss>"
            return Response(url, 200, {}, feed.encode(), 0.01)
        found = NUMBER.search(url)
        if not found:
            return Response(url, 404, {}, b"", 0.01)  # SEC, in a window
        number = int(found[1])
        if number == self.refuse:
            return Response(url, 403, {}, b"", 0.01)
        self.asked[number] = self.asked.get(number, 0) + 1
        asks, body = self.pages.get(number, (10**6, b""))
        if self.asked[number] > asks:
            return Response(url, 200, {}, body, 0.01)
        return Response(url, 410, {}, b"gone", 0.01)


def test_guessing_takes_six_minutes_split_among_the_release_times() -> None:
    nvidia = datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    assert spans([nvidia]) == [(nvidia - timedelta(minutes=2), nvidia + timedelta(minutes=4))]
    weride = [FRIDAY.replace(hour=h) for h in (4, 6, 7)]  # 2 minutes each
    assert spans(weride)[0] == (
        weride[0] - timedelta(seconds=40),
        weride[0] + timedelta(seconds=80),
    )


def test_only_a_release_on_globenewswire_alone_is_guessed() -> None:
    def target(**fields: Any) -> Target:
        return Target(ticker="WRD", cik=1867729, expected=FRIDAY, **fields)

    assert guessed_for(target(trial=Trial(globenewswire=["NASDAQ: WRD"])))
    assert not guessed_for(target())
    # A company whose wire isn't known is looked for on all three
    unknown = ["WeRide"]
    assert not guessed_for(
        target(newswire=unknown, prnewswire=unknown, trial=Trial(globenewswire=unknown))
    )


def test_the_release_is_the_companys_ticker_with_this_quarters_results() -> None:
    weride = Target(ticker="WRD", cik=1867729, expected=FRIDAY, period="FY2026Q3")
    hidden = Page(1, "u", "Global Expansion Accelerates", "Nasdaq:WRD, HKEX:0800", None, WERIDE_Q3)
    assert is_release(hidden, weride)  # by its text
    assert not is_release(hidden, weride.model_copy(update={"period": "FY2026Q2"}))
    plain = Page(2, "u", "WeRide Announces Third Quarter 2026 Results", "Nasdaq:WRD", None, b"")
    assert is_release(plain, weride)
    assert not is_release(
        Page(3, "u", "Acme Announces Third Quarter 2026 Results", "NYSE:ACME", None, OTHER), weride
    )


async def test_guessing_finds_the_page_as_it_opens_with_a_new_url_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.02)
    fetcher = Frontier_({101: (2, OTHER), 102: (4, WERIDE_Q3)})
    log = io.StringIO()
    journal = Journal(log)
    weride = Target(ticker="WRD", cik=1867729, expected=FRIDAY, period="FY2026Q3")
    seen: list[Page] = []

    async def found(item: Page) -> bool:
        seen.append(item)
        return is_release(item, weride)

    outcome = await Frontier(fetcher, journal, "WRD").watch(
        journal.now() + timedelta(seconds=5), found
    )

    assert outcome == "found"
    assert [item.number for item in seen] == [101, 102]
    assert seen[-1].url.startswith("https://www.globenewswire.com/news-release/")
    guesses = [url for url in fetcher.urls if "/news-release/" in url]
    # A 410 can stay in GlobeNewswire's cache for 30 days, so no URL is asked twice
    assert len(set(guesses)) == len(guesses)
    assert max(int(NUMBER.search(url)[1]) for url in guesses) <= 105  # type: ignore[index]


async def test_a_number_that_opened_ahead_of_an_earlier_one_is_not_guessed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.0)
    # 102 is out at once and 101 opens a few rounds later, as the release itself
    fetcher = Frontier_({101: (3, WERIDE_Q3), 102: (0, OTHER)})
    journal = Journal(io.StringIO())
    weride = Target(ticker="WRD", cik=1867729, expected=FRIDAY, period="FY2026Q3")

    async def found(item: Page) -> bool:
        return is_release(item, weride)

    outcome = await Frontier(fetcher, journal, "WRD").watch(
        journal.now() + timedelta(seconds=5), found
    )

    assert outcome == "found"
    assert fetcher.asked[101] == 4  # asked each round until it opened
    assert fetcher.asked[102] == 1  # opened in the first round, never asked for again


async def test_a_refusal_ends_the_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.02)
    fetcher = Frontier_({}, refuse=102)
    journal = Journal(io.StringIO())

    async def never(item: Page) -> bool:
        return False

    frontier = Frontier(fetcher, journal, "WRD")
    assert await frontier.watch(journal.now() + timedelta(seconds=5), never) == "refused"
    assert frontier.sent == 3  # the first round only


async def test_the_budget_caps_the_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.0)
    fetcher = Frontier_({})
    journal = Journal(io.StringIO())

    async def never(item: Page) -> bool:
        return False

    frontier = Frontier(fetcher, journal, "WRD", budget=10)
    assert await frontier.watch(journal.now() + timedelta(seconds=5), never) == "budget"
    assert frontier.sent == 10


async def test_a_window_posts_the_guessed_page_as_a_globenewswire_sighting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.02)
    monkeypatch.setattr(globe_frontier, "SPAN", timedelta(seconds=3))
    journal = Journal(io.StringIO())
    weride = Target(
        ticker="WRD",
        cik=1867729,
        expected=journal.now() + timedelta(seconds=0.2),
        period="FY2026Q3",
        trial=Trial(globenewswire=["NASDAQ: WRD"]),
    )
    posted: list[tuple[str, str, bool]] = []

    def notify(source: Any, sighting: Any) -> None:
        posted.append((source.name, sighting.url, sighting.earnings))

    fetcher = Frontier_({101: (1, WERIDE_Q3)})
    await guess_release(weride, fetcher, journal, notify, journal.now() + timedelta(seconds=5))

    [(source, url, earnings)] = posted
    assert (source, earnings) == ("globenewswire_page", True)
    assert re.fullmatch(
        r"https://www\.globenewswire\.com/news-release/\d{4}/\d{2}/\d{2}/101/0/en/[^/]+\.html", url
    )


async def test_a_window_starts_the_guessing_only_for_a_globenewswire_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    async def guessing(target: Target, *args: Any) -> None:
        started.append(target.ticker)

    async def synced(journal: Journal, periodic: bool = False) -> bool:
        return True

    monkeypatch.setattr(recorder, "guess_release", guessing)
    monkeypatch.setattr(recorder, "sync_clock", synced)
    journal = Journal(io.StringIO())
    for ticker, trial in (("WRD", Trial(globenewswire=["NASDAQ: WRD"])), ("KLAR", Trial())):
        target = Target(
            ticker=ticker,
            cik=1,
            expected=journal.now(),
            before_minutes=1,
            after_minutes=0.003,
            trial=trial,
        )
        await recorder.record_target(target, Frontier_({}), journal)
    assert started == ["WRD"]


async def test_a_batch_records_every_page_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.02)
    log = io.StringIO()
    journal = Journal(log)
    batch = GlobeBatch(
        id="globe-batch-test",
        starts_at=journal.now(),
        duration_seconds=10,
        max_requests=30,
    )
    fetcher = Frontier_({101: (1, OTHER), 102: (1, WERIDE_Q3)})

    await asyncio.wait_for(run_batch(batch, fetcher, journal), 10)

    events = [json.loads(line) for line in log.getvalue().splitlines()]
    assert [e["number"] for e in events if e["event"] == "globe_page"] == [101, 102]
    [finished] = [e for e in events if e["event"] == "globe_batch_finished"]
    assert (finished["outcome"], finished["guesses"]) == ("budget", 30)
    # 102 is a results page for WeRide's ticker, yet a batch only records: it is never found
    assert not [e for e in events if e["event"] == "globe_found"]
    assert "NEW" not in log.getvalue()


async def test_the_bot_runs_globenewswire_batches_from_its_schedule_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(globe_frontier, "ROUND", 0.02)
    folder = tmp_path / "trials" / "globe"
    folder.mkdir(parents=True)
    log = io.StringIO()
    journal = Journal(log)
    batch = GlobeBatch(
        id="monday-0700", starts_at=journal.now(), duration_seconds=10, max_requests=6
    )
    (folder / "monday-0700.json").write_text(batch.model_dump_json(), encoding="utf-8")
    (folder / "broken.json").write_text('{"id": "x"}', encoding="utf-8")
    fetcher = Frontier_({101: (1, OTHER)})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, Releases(FakePoster, fetcher, journal, None), store, print
        )
        await runner.scan()
        await runner.scan()  # each batch starts once
        assert list(runner.trials) == ["monday-0700"]
        await asyncio.wait_for(runner.trials["monday-0700"], 10)

    events = [json.loads(line) for line in log.getvalue().splitlines()]
    assert [e["number"] for e in events if e["event"] == "globe_page"] == [101]
    errors = [e for e in events if e["event"] == "publication_trial_error"]
    assert len(errors) == 1 and errors[0]["detail"].startswith("broken.json")


async def test_when_every_guess_is_out_the_next_numbers_open_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public-company feed's newest number trailed GlobeNewswire's newest article by 13 on a
    quiet Saturday night (measured Nov 14), so guessing starts behind and catches up in bursts."""
    monkeypatch.setattr(globe_frontier, "ROUND", 1.0)
    fetcher = Frontier_(dict.fromkeys(range(101, 120), (0, OTHER)))
    journal = Journal(io.StringIO())
    opened: list[int] = []

    async def never(item: Page) -> bool:
        opened.append(item.number)
        return False

    await Frontier(fetcher, journal, "WRD", budget=9).watch(
        journal.now() + timedelta(seconds=0.5), never
    )
    assert opened == list(range(101, 110))  # 3, then 6 at once, with no 1 s wait between
