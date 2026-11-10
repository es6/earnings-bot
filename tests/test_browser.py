from typing import Any

from earnings_bot.browser import _SETTLED, DesktopChrome, _Tab


class FakeTabSocket:
    """Answers DevTools commands the way a tab does, and remembers what it was asked."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        result: dict[str, Any] = {}
        if message["method"] == "Runtime.evaluate":
            if message["params"]["expression"] == _SETTLED:
                value: Any = ["complete", 1, 10]
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
