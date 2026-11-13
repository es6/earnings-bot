from typing import Any

import pytest

from earnings_bot.browser import _SETTLED, DesktopChrome, _Tab


class FakeTabSocket:
    """Answers DevTools commands the way a tab does, and remembers what it was asked."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.value: Any = None  # what the next in-page fetch answers, when set

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        result: dict[str, Any] = {}
        if message["method"] == "Runtime.evaluate":
            if message["params"]["expression"] == _SETTLED:
                value: Any = ["complete", 1, 10]
            elif self.value is not None:
                value = self.value
            else:
                value = {
                    "url": "https://ir.example.com/results",
                    "status": 200,
                    "title": "Results",
                    "text": "Q3 2026 Earnings Release",
                    "links": [["https://ir.example.com/q3.pdf", "Earnings Release", "card"]],
                    "frames": [],
                }
            result = {"result": {"value": value}}
        # A tab sends events between replies, which the reader must skip
        self.replies += [
            {"method": "Network.requestWillBeSent", "params": {}},
            {"id": 1, "result": result},
        ]

    async def receive_json(self) -> dict[str, Any]:
        return self.replies.pop(0)

    async def close(self) -> None:
        return None


async def test_a_render_turns_off_chromes_own_cache_before_it_navigates() -> None:
    # Without this, a second render of a page the site marks cacheable shows the first copy,
    # and so does the data its scripts load (checked with real Chrome on Nov 10, 2026)
    chrome = DesktopChrome()
    socket = FakeTabSocket()

    async def started() -> None:
        return None

    async def new_tab(host: str) -> _Tab:
        tab = _Tab("tab", socket)  # type: ignore[arg-type]
        chrome._tabs[host] = tab
        return tab

    async def drop(host: str) -> None:
        chrome._tabs.pop(host, None)

    async def wait_turn(host: str) -> None:
        return None

    chrome._start = started  # type: ignore[method-assign]
    chrome._new_tab = new_tab  # type: ignore[method-assign]
    chrome._drop = drop  # type: ignore[method-assign]

    page = await chrome.render("https://ir.example.com/results", 10, wait_turn)

    methods = [message["method"] for message in socket.sent]
    assert methods[:3] == ["Network.enable", "Network.setCacheDisabled", "Page.navigate"]
    assert socket.sent[1]["params"] == {"cacheDisabled": True}
    assert page.links == [("https://ir.example.com/q3.pdf", "Earnings Release")]


async def test_a_slow_answer_times_out_without_losing_the_warm_tab() -> None:
    """Brookfield's page timed out twice as its Q3 2026 release went up. Each timeout dropped
    the tab, so the retry had to navigate again first."""
    chrome = DesktopChrome()
    socket = FakeTabSocket()
    socket.value = {"slow": True}
    chrome._tabs["ir.example.com"] = _Tab("tab", socket)  # type: ignore[arg-type]
    dropped: list[str] = []

    async def started() -> None:
        return None

    async def drop(host: str) -> None:
        dropped.append(host)

    async def wait_turn(host: str) -> None:
        return None

    chrome._start = started  # type: ignore[method-assign]
    chrome._drop = drop  # type: ignore[method-assign]
    url = "https://ir.example.com/results"

    with pytest.raises(TimeoutError):
        await chrome.request(url, url, 3, wait_turn)

    assert dropped == []
    assert "ir.example.com" in chrome._tabs
    # The page is told how long to wait, so it gives up before the bot does
    assert socket.sent[-1]["params"]["expression"].endswith(", 3000)")


@pytest.mark.parametrize(
    ("label", "host", "samples"),
    [
        ("Q3 2026 Earnings Release", "ir.example.com", 1),
        ("Q2 2026 Earnings Release", "ir.example.com", 4),
        ("Q3 2026 Earnings Release", "other.example.net", 4),
        ("Q3 2026 Earnings Call", "ir.example.com", 4),
    ],
)
async def test_render_only_skips_settling_for_a_verified_target_quarter_pdf(
    label: str, host: str, samples: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from earnings_bot.browser import RenderedPage
    from earnings_bot.quarterly_table import rendered_pdf

    chrome = DesktopChrome()
    socket = FakeTabSocket()
    socket.value = {
        "url": "https://ir.example.com/results",
        "status": 200,
        "title": "Results",
        "text": "Results",
        "links": [[f"https://{host}/report.pdf", label, "card"]],
        "frames": [],
    }

    async def started() -> None:
        pass

    async def new_tab(key: str) -> _Tab:
        tab = chrome._tabs[key] = _Tab(key, socket)  # type: ignore[arg-type]
        return tab

    async def drop(key: str) -> None:
        chrome._tabs.pop(key)

    async def wait_turn(host: str) -> None:
        pass

    def ready(page: RenderedPage) -> bool:
        return rendered_pdf(page, 2026, 3) is not None

    monkeypatch.setattr(chrome, "_start", started)
    monkeypatch.setattr(chrome, "_new_tab", new_tab)
    monkeypatch.setattr(chrome, "_drop", drop)
    await chrome.render("https://ir.example.com/results", 10, wait_turn, ready=ready)
    assert (
        sum(
            msg["params"].get("expression") == _SETTLED
            for msg in socket.sent
            if msg["method"] == "Runtime.evaluate"
        )
        == samples
    )
    assert not chrome._tabs


async def test_research_yields_after_pacing_without_dropping_a_live_warm_tab() -> None:
    from earnings_bot.browser import BrowserDeferred

    chrome = DesktopChrome()
    socket = FakeTabSocket()
    chrome._tabs["ir.example.com"] = _Tab("tab", socket)  # type: ignore[arg-type]

    async def started() -> None:
        return None

    async def wait_turn(host: str) -> None:
        chrome.allowed = lambda: False

    async def drop(host: str) -> None:
        raise AssertionError("A deferred background request dropped the live tab")

    chrome._start = started  # type: ignore[method-assign]
    chrome._drop = drop  # type: ignore[method-assign]
    with pytest.raises(BrowserDeferred):
        await chrome.request(
            "https://ir.example.com/results", "https://ir.example.com/", 1, wait_turn
        )
    assert socket.sent == [] and "ir.example.com" in chrome._tabs


async def test_an_urgent_read_does_not_wait_for_a_slow_listing_poll_on_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8: the release's article read uses the host's second warm tab, so a listing poll
    that is taking its time on the first tab doesn't hold it up. Both still take turns."""
    import asyncio
    import time

    from earnings_bot import browser as browser_module
    from earnings_bot.browser import URGENT_LANE

    chrome = DesktopChrome()
    host = "ir.example.com"
    chrome._tabs[host] = _Tab("listing", FakeTabSocket())  # type: ignore[arg-type]
    chrome._tabs[host + URGENT_LANE] = _Tab("urgent", FakeTabSocket())  # type: ignore[arg-type]

    async def started() -> None:
        pass

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        # The listing poll is slow; the article read is quick
        await asyncio.sleep(0.05 if "q3-results" in expression else 1.0)
        return {"status": 200, "headers": {}, "body": ""}

    turns: list[str] = []

    async def wait_turn(asked: str) -> None:
        turns.append(asked)

    monkeypatch.setattr(chrome, "_start", started)
    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    begun = time.monotonic()
    listing = asyncio.create_task(
        chrome.request(f"https://{host}/news", f"https://{host}/news", 4, wait_turn)
    )
    await asyncio.sleep(0.01)
    article = await chrome.request(
        f"https://{host}/news/q3-results", f"https://{host}/news", 4, wait_turn, urgent=True
    )
    assert time.monotonic() - begun < 0.5  # it didn't wait the listing's second
    assert article.waits["tab_lock_ms"] < 100
    await listing
    assert turns == [host, host]  # each took its turn in the host's queue


async def test_the_urgent_tab_is_warmed_once_the_polling_tab_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from earnings_bot import browser as browser_module
    from earnings_bot.browser import URGENT_LANE

    chrome = DesktopChrome()
    host = "ir.example.com"
    warmed: list[str] = []

    async def started() -> None:
        pass

    async def warm(key: str, warmup: str, host_: str | None = None) -> _Tab:
        warmed.append(key)
        tab = chrome._tabs[key] = _Tab(key, FakeTabSocket())  # type: ignore[arg-type]
        return tab

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        return {"status": 200, "headers": {}, "body": ""}

    async def wait_turn(asked: str) -> None:
        pass

    monkeypatch.setattr(chrome, "_start", started)
    monkeypatch.setattr(chrome, "_warm", warm)
    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    await chrome.request(f"https://{host}/news", f"https://{host}/news", 4, wait_turn)
    await asyncio.gather(*chrome._prewarming.values())
    assert warmed == [host, host + URGENT_LANE]


async def test_closing_chrome_cancels_and_joins_speculative_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    chrome = DesktopChrome()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_turn(host: str) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    chrome._prewarm("ir.example.com", "https://ir.example.com/", wait_turn)
    await entered.wait()
    tasks = tuple(chrome._prewarming.values())
    await chrome.close()
    assert cancelled.is_set() and not chrome._prewarming
    assert all(task.done() for task in tasks)


async def test_an_urgent_read_cancels_lower_priority_speculative_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from earnings_bot import browser as browser_module
    from earnings_bot.browser import URGENT_LANE

    chrome = DesktopChrome()
    host = "ir.example.com"
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def prewarm_turn(asked: str) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def started() -> None:
        pass

    async def warm(key: str, warmup: str, host_: str | None = None) -> _Tab:
        return _Tab(key, FakeTabSocket())  # type: ignore[arg-type]

    turns: list[str] = []

    async def urgent_turn(asked: str) -> None:
        turns.append(asked)

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        return {"status": 200, "headers": {}, "body": ""}

    monkeypatch.setattr(chrome, "_start", started)
    monkeypatch.setattr(chrome, "_warm", warm)
    monkeypatch.setattr(browser_module, "_evaluate", evaluate)
    chrome._prewarm(host, f"https://{host}/", prewarm_turn)
    await entered.wait()
    result = await asyncio.wait_for(
        chrome.request(f"https://{host}/q3", f"https://{host}/", 1, urgent_turn, urgent=True),
        1,
    )
    assert cancelled.is_set() and not chrome._prewarming
    assert turns == [host, host]  # navigation and article use the same budget
    assert result.status == 200 and host + URGENT_LANE in chrome._locks
