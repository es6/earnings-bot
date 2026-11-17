import asyncio
from typing import Any

import pytest

from first_print import browser as browser_module
from first_print.browser import (
    _SETTLED,
    URGENT_LANE,
    BrowserDeferred,
    DesktopChrome,
    RenderedPage,
    _Tab,
    browser_host,
)
from first_print.quarterly_table import rendered_pdf
from tests.fakes import FakeTabSocket

HOST = "ir.example.com"


async def no_turn(host: str) -> None:
    return None


@pytest.fixture
def quick_settling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A render waits 0.3 s between samples of the page. Keep every sample, skip the wait."""
    real_sleep = asyncio.sleep

    async def yield_once(delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(browser_module.asyncio, "sleep", yield_once)


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> DesktopChrome:
    """A DesktopChrome that never starts a browser."""
    chrome = DesktopChrome()

    async def started() -> None:
        return None

    monkeypatch.setattr(chrome, "_start", started)
    return chrome


def render_in(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch, socket: FakeTabSocket
) -> None:
    """Every tab a render opens is this socket, and closing it forgets it."""

    async def new_tab(key: str) -> _Tab:
        tab = chrome._tabs[key] = _Tab(key, socket)  # type: ignore[arg-type]
        return tab

    async def drop(key: str) -> None:
        chrome._tabs.pop(key, None)

    monkeypatch.setattr(chrome, "_new_tab", new_tab)
    monkeypatch.setattr(chrome, "_drop", drop)


@pytest.mark.usefixtures("quick_settling")
async def test_a_render_turns_off_chromes_own_cache_before_it_navigates(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without this, a second render of a page the site marks cacheable shows the first copy,
    # and so does the data its scripts load (checked with real Chrome on Nov 10, 2026)
    socket = FakeTabSocket()
    render_in(chrome, monkeypatch, socket)

    page = await chrome.render(f"https://{HOST}/results", 10, no_turn)

    methods = [message["method"] for message in socket.sent]
    assert methods[:3] == ["Network.enable", "Network.setCacheDisabled", "Page.navigate"]
    assert socket.sent[1]["params"] == {"cacheDisabled": True}
    assert page.links == [(f"https://{HOST}/q3.pdf", "Earnings Release")]


async def test_a_slow_answer_times_out_without_losing_the_warm_tab(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brookfield's page timed out twice as its Q3 2026 release went up. Each timeout dropped
    the tab, so the retry had to navigate again first."""
    socket = FakeTabSocket()
    socket.value = {"slow": True}
    chrome._tabs[HOST] = _Tab("tab", socket)  # type: ignore[arg-type]
    dropped: list[str] = []

    async def drop(host: str) -> None:
        dropped.append(host)

    monkeypatch.setattr(chrome, "_drop", drop)
    url = f"https://{HOST}/results"

    with pytest.raises(TimeoutError):
        await chrome.request(url, url, 3, no_turn)

    assert dropped == []
    assert HOST in chrome._tabs
    # The page is told how long to wait, so it gives up before the bot does
    assert socket.sent[-1]["params"]["expression"].endswith(", 3000)")


@pytest.mark.usefixtures("quick_settling")
@pytest.mark.parametrize(
    ("label", "host", "samples"),
    [
        ("Q3 2026 Earnings Release", HOST, 1),
        ("Q2 2026 Earnings Release", HOST, 4),
        ("Q3 2026 Earnings Release", "other.example.net", 4),
        ("Q3 2026 Earnings Call", HOST, 4),
    ],
    ids=[
        "this-quarters-release-stops-at-once",
        "a-prior-quarter-settles",
        "a-file-on-another-host-settles",
        "a-call-settles",
    ],
)
async def test_render_only_skips_settling_for_a_verified_target_quarter_pdf(
    label: str,
    host: str,
    samples: int,
    chrome: DesktopChrome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeTabSocket()
    socket.value = {
        "url": f"https://{HOST}/results",
        "status": 200,
        "title": "Results",
        "text": "Results",
        "links": [[f"https://{host}/report.pdf", label, "card"]],
        "frames": [],
    }
    render_in(chrome, monkeypatch, socket)

    def ready(page: RenderedPage) -> bool:
        return rendered_pdf(page, 2026, 3) is not None

    await chrome.render(f"https://{HOST}/results", 10, no_turn, ready=ready)

    settle_checks = [
        msg
        for msg in socket.sent
        if msg["method"] == "Runtime.evaluate" and msg["params"].get("expression") == _SETTLED
    ]
    assert len(settle_checks) == samples
    assert not chrome._tabs


async def test_research_yields_after_pacing_without_dropping_a_live_warm_tab(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = FakeTabSocket()
    chrome._tabs[HOST] = _Tab("tab", socket)  # type: ignore[arg-type]

    async def wait_turn(host: str) -> None:
        chrome.allowed = lambda: False

    async def drop(host: str) -> None:
        raise AssertionError("A deferred background request dropped the live tab")

    monkeypatch.setattr(chrome, "_drop", drop)
    with pytest.raises(BrowserDeferred):
        await chrome.request(f"https://{HOST}/results", f"https://{HOST}/", 1, wait_turn)
    assert socket.sent == [] and HOST in chrome._tabs


async def test_an_urgent_read_does_not_wait_for_a_slow_listing_poll_on_its_host(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 8: the release's article read uses the host's second warm tab, so a listing poll
    that is taking its time on the first tab doesn't hold it up. Both still take turns."""
    chrome._tabs[HOST] = _Tab("listing", FakeTabSocket())  # type: ignore[arg-type]
    chrome._tabs[HOST + URGENT_LANE] = _Tab("urgent", FakeTabSocket())  # type: ignore[arg-type]
    listing_started, listing_may_finish = asyncio.Event(), asyncio.Event()

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        # The listing poll is stuck until the test lets it go; the article read is quick
        if "q3-results" not in expression:
            listing_started.set()
            await listing_may_finish.wait()
        return {"status": 200, "headers": {}, "body": ""}

    turns: list[str] = []

    async def wait_turn(asked: str) -> None:
        turns.append(asked)

    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    listing = asyncio.create_task(
        chrome.request(f"https://{HOST}/news", f"https://{HOST}/news", 4, wait_turn)
    )
    await listing_started.wait()
    # A read on the listing's own tab would wait for it forever; the bound only ends that
    article = await asyncio.wait_for(
        chrome.request(
            f"https://{HOST}/news/q3-results", f"https://{HOST}/news", 4, wait_turn, urgent=True
        ),
        5,
    )

    assert not listing.done()
    assert article.status == 200
    listing_may_finish.set()
    await listing
    assert turns == [HOST, HOST]  # each took its turn in the host's queue


async def test_the_urgent_tab_is_warmed_once_the_polling_tab_is(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    warmed: list[str] = []

    async def warm(key: str, warmup: str, host_: str | None = None) -> _Tab:
        warmed.append(key)
        tab = chrome._tabs[key] = _Tab(key, FakeTabSocket())  # type: ignore[arg-type]
        return tab

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        return {"status": 200, "headers": {}, "body": ""}

    monkeypatch.setattr(chrome, "_warm", warm)
    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    await chrome.request(f"https://{HOST}/news", f"https://{HOST}/news", 4, no_turn)
    await asyncio.gather(*chrome._prewarming.values())
    assert warmed == [HOST, HOST + URGENT_LANE]


async def test_closing_chrome_cancels_and_joins_speculative_warmup() -> None:
    chrome = DesktopChrome()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_turn(host: str) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    chrome._prewarm(HOST, f"https://{HOST}/", wait_turn)
    await entered.wait()
    tasks = tuple(chrome._prewarming.values())
    await chrome.close()
    assert cancelled.is_set() and not chrome._prewarming
    assert all(task.done() for task in tasks)


async def test_an_urgent_read_cancels_lower_priority_speculative_pacing(
    chrome: DesktopChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def prewarm_turn(asked: str) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def warm(key: str, warmup: str, host_: str | None = None) -> _Tab:
        return _Tab(key, FakeTabSocket())  # type: ignore[arg-type]

    turns: list[str] = []

    async def urgent_turn(asked: str) -> None:
        turns.append(asked)

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        return {"status": 200, "headers": {}, "body": ""}

    monkeypatch.setattr(chrome, "_warm", warm)
    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    chrome._prewarm(HOST, f"https://{HOST}/", prewarm_turn)
    await entered.wait()
    result = await asyncio.wait_for(
        chrome.request(f"https://{HOST}/q3", f"https://{HOST}/", 1, urgent_turn, urgent=True),
        1,
    )
    assert cancelled.is_set() and not chrome._prewarming
    assert turns == [HOST, HOST]  # navigation and article use the same budget
    assert result.status == 200 and HOST + URGENT_LANE in chrome._locks


@pytest.mark.parametrize(
    "url",
    [
        "https://ir.lucidmotors.com/press-releases",
        "https://investor.example.co.uk/news?page=2",
        "https://IR.Example.com:443/news",
        "https://8.8.8.8/news",
    ],
)
def test_chrome_may_open_any_public_https_page(url: str) -> None:
    assert browser_host(url) == url.split("/")[2]


@pytest.mark.parametrize(
    "url",
    [
        "http://ir.example.com/news",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://10.0.0.5/admin",
        "https://192.168.1.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest/meta-data",
        "https://printer.local/",
        "https://nas.home.arpa/",
        "https://router/",
        "https://ir.example.com:8443/news",
        "https://user@ir.example.com/news",
        "https://ir.example.com/news#latest",
        "https://ir.example.com",
    ],
    ids=[
        "http",
        "localhost",
        "loopback",
        "private",
        "home-router",
        "ipv6-loopback",
        "link-local",
        "mdns",
        "home-arpa",
        "single-label",
        "other-port",
        "login",
        "fragment",
        "no-path",
    ],
)
def test_chrome_never_opens_this_pc_or_its_network(url: str) -> None:
    with pytest.raises(ValueError, match="Not a public https page"):
        browser_host(url)
