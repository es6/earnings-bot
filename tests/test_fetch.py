"""The Fetcher's pacing: SEC's hosts share one budget, urgent requests go first, and every other
host keeps its own queue."""

import asyncio
import heapq
import itertools
import socket
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from earnings_bot import fetch as fetch_module
from earnings_bot.browser import BrowserError
from earnings_bot.fetch import Fetcher, HostBusy, host_gap, queue_key
from earnings_bot.sources import EdgarFilings, EdgarLatest, Sighting
from earnings_bot.text import release_text
from tests import fakes
from tests.fakes import FakeSession, SessionFetcher

SEC_GAP = 0.125  # 8 requests a second, under SEC's limit of 10
EXHIBIT = "<p>" + "Revenue grew 12 percent. " * 120 + "</p>"
FILING = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101"
INDEX = '{"directory": {"item": [{"name": "aapl-20261106.htm"}, {"name": "ex99-1.htm"}]}}'


class VirtualClock:
    """The Fetcher's time, which only moves when every task has run out of things to do.

    A pause (`asyncio.sleep`) in the fetcher takes no real time: once all tasks are waiting,
    the clock jumps to the earliest wake-up. The pacing the tests check is then exact, and
    can't be thrown off by a busy machine."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._count = itertools.count()
        self._real_sleep = asyncio.sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await self._real_sleep(0)
            return
        wake = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + delay, next(self._count), wake))
        await wake

    async def drive(self) -> None:
        while True:
            for _ in range(20):  # let every task that can run, run
                await self._real_sleep(0)
            if self._sleepers:
                moment, _, wake = heapq.heappop(self._sleepers)
                self.now = max(self.now, moment)
                if not wake.done():
                    wake.set_result(None)


@pytest_asyncio.fixture
async def clock(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VirtualClock]:
    clock = VirtualClock()
    clock_module = SimpleNamespace(monotonic=clock.monotonic)

    class AsyncioWithVirtualSleep:
        sleep = staticmethod(clock.sleep)

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    monkeypatch.setattr(fetch_module, "time", clock_module)
    monkeypatch.setattr(fetch_module, "asyncio", AsyncioWithVirtualSleep())
    monkeypatch.setattr(fakes, "time", clock_module)
    driver = asyncio.create_task(clock.drive())
    yield clock
    driver.cancel()
    await asyncio.gather(driver, return_exceptions=True)


def most_in_one_second(times: list[float]) -> int:
    """The most of these moments that fall in any one-second span [t, t + 1)."""
    times = sorted(times)
    start, most = 0, 0
    for end, moment in enumerate(times):
        while moment - times[start] >= 1.0:
            start += 1
        most = max(most, end - start + 1)
    return most


def test_every_sec_host_waits_in_one_queue_at_8_requests_a_second() -> None:
    assert queue_key("www.sec.gov") == queue_key("data.sec.gov") == queue_key("sec.gov")
    assert host_gap(queue_key("data.sec.gov")) == SEC_GAP
    # Every other host, the file CDN's included, still has a queue of its own
    assert queue_key("s1.q4cdn.com") != queue_key("s23.q4cdn.com")
    assert queue_key("investors.example.com") == "investors.example.com"
    assert queue_key("notsec.gov") == "notsec.gov"


@pytest.mark.parametrize(
    ("host", "gap"),
    [
        ("data.sec.gov", 0.125),
        ("s23.q4cdn.com", 0.2),
        ("feed.businesswire.com", 1.0),
        ("investor.uber.com", 1.0),
        ("notsec.gov", 1.0),
    ],
)
def test_host_gaps(host: str, gap: float) -> None:
    assert host_gap(host) == gap


async def sec(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
    if url.endswith("index.json"):
        return 200, {}, INDEX.encode()
    if url.endswith("ex99-1.htm"):
        return 200, {}, EXHIBIT.encode()
    return 200, {}, b"{}"


@pytest.mark.usefixtures("clock")
async def test_polls_of_both_sec_hosts_and_exhibit_downloads_share_8_requests_a_second() -> None:
    session = FakeSession(sec)
    filing = Sighting(
        "0000320193-26-000101", "8-K 2.02", f"{FILING}/aapl-20261106.htm", earnings=True
    )
    async with SessionFetcher(session) as fetcher:
        # Both polls for each of 5 companies, and an 8-K's exhibit: its folder, then the file
        polls = [
            fetcher.request("GET", url)
            for cik in range(5)
            for url in (EdgarFilings(cik).url(), EdgarLatest(cik).url())
        ]
        _, text = await asyncio.gather(
            asyncio.gather(*polls), release_text(fetcher, EdgarFilings(1), filing)
        )

    assert text and text.startswith("Revenue grew")
    hosts = {url.split("/")[2] for _, _, url in session.sent}
    assert hosts == {"www.sec.gov", "data.sec.gov"}
    times = [moment for moment, _, _ in session.sent]
    assert len(times) == 12
    # SEC's two hosts share one queue: never more than 8 in a second, never closer than the gap
    assert most_in_one_second(times) <= 8
    gaps = [b - a for a, b in itertools.pairwise(sorted(times))]
    assert min(gaps) >= SEC_GAP - 1e-9
    # and the queue is kept full: all 12 went out one gap apart
    assert max(times) - min(times) == pytest.approx(11 * SEC_GAP)


async def test_an_urgent_exhibit_download_goes_ahead_of_polls_of_the_other_sec_host(
    clock: VirtualClock,
) -> None:
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[tuple[str, float]] = []

    async def take_turn(name: str, host: str, urgent: bool = False) -> None:
        await fetcher.wait_turn(host, urgent)
        turns.append((name, clock.now))

    await take_turn("latest filings", "www.sec.gov")  # SEC is free, so this goes at once
    polls = [asyncio.create_task(take_turn(f"submissions {i}", "data.sec.gov")) for i in range(3)]
    await asyncio.sleep(0)  # they line up behind it
    await take_turn("exhibit", "www.sec.gov", urgent=True)
    await asyncio.gather(*polls)

    assert [name for name, _ in turns] == [
        "latest filings",
        "exhibit",
        "submissions 0",
        "submissions 1",
        "submissions 2",
    ]
    gaps = [b - a for (_, a), (_, b) in itertools.pairwise(turns)]
    assert gaps == [pytest.approx(SEC_GAP)] * 4


async def test_other_hosts_dont_wait_behind_a_full_sec_queue(clock: VirtualClock) -> None:
    async with Fetcher("Test Person test@example.com") as fetcher:
        sec_turns = [
            asyncio.create_task(fetcher.wait_turn(host))
            for host in ("www.sec.gov", "data.sec.gov") * 10
        ]
        await asyncio.sleep(0)  # 20 SEC requests line up: 2.5 s of SEC's budget
        started = clock.now
        # An IR site's poll and the file CDN's downloads go at once
        for host in ("investors.example.com", "s1.q4cdn.com", "s23.q4cdn.com"):
            await fetcher.wait_turn(host)

        assert clock.now == started  # no time passed: they didn't line up behind SEC
        assert sum(turn.done() for turn in sec_turns) <= 1
        for turn in sec_turns:
            turn.cancel()
        await asyncio.gather(*sec_turns, return_exceptions=True)


class _LoopbackResolver(AbstractResolver):
    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        return [
            ResolveResult(
                hostname=host, host="127.0.0.1", port=port, family=socket.AF_INET, proto=0, flags=0
            )
        ]

    async def close(self) -> None:
        pass


async def test_sec_redirects_and_connection_retries_wait_for_the_shared_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrivals: list[tuple[str, str]] = []
    turns: list[tuple[str, float]] = []
    wait_turn = Fetcher.wait_turn

    async def record_turn(fetcher: Fetcher, host: str, urgent: bool = False) -> None:
        await wait_turn(fetcher, host, urgent)
        # Observe the grant recorded by the queue. Connection setup and server arrival
        # times vary independently, even over loopback.
        turns.append((host, fetcher._queues[queue_key(host)].next_turn))

    monkeypatch.setattr(Fetcher, "wait_turn", record_turn)
    retried = False

    async def respond(request: web.Request) -> web.Response:
        nonlocal retried
        arrivals.append((request.host.split(":")[0], request.path))
        if request.path == "/start":
            raise web.HTTPFound(f"http://www.sec.gov:{request.url.port}/exhibit")
        if request.path == "/exhibit" and not retried:
            retried = True
            assert request.transport is not None
            request.transport.abort()
        return web.Response(text=request.headers["User-Agent"])

    app = web.Application()
    app.router.add_get("/{step}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    session_class = aiohttp.ClientSession

    def local_session(**kwargs: Any) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(resolver=_LoopbackResolver())
        return session_class(connector=connector, **kwargs)

    monkeypatch.setattr(aiohttp, "ClientSession", local_session)
    try:
        async with Fetcher("Test SEC client") as fetcher:
            response = await fetcher.request(
                "GET", f"http://data.sec.gov:{port}/start", urgent=True
            )
            assert response.body == b"Test SEC client"

            # A publisher can also redirect a release link into EDGAR. That hop must use
            # SEC's queue and identity even though the first request was on another site.
            response = await fetcher.request("GET", f"http://news.example.com:{port}/start")
            assert response.body == b"Test SEC client"
    finally:
        await runner.cleanup()

    assert arrivals == [
        ("data.sec.gov", "/start"),
        ("www.sec.gov", "/exhibit"),
        ("www.sec.gov", "/exhibit"),
        ("news.example.com", "/start"),
        ("www.sec.gov", "/exhibit"),
    ]
    assert [host for host, _ in turns] == [host for host, _ in arrivals]
    sec_turns = [at for host, at in turns if host.endswith(".sec.gov")]
    assert all(b - a >= SEC_GAP - 1e-6 for a, b in itertools.pairwise(sec_turns))


# Item 9: research during open release windows runs in the background


async def test_background_research_waits_behind_every_poll_and_paces_sec(
    monkeypatch: pytest.MonkeyPatch, clock: VirtualClock
) -> None:
    monkeypatch.setattr("earnings_bot.fetch.BACKGROUND_SEC_GAP", 0.4)
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[tuple[str, float]] = []

    async def take_turn(name: str, host: str, background: bool = False) -> None:
        if background:
            with fetcher.background(chrome=False):
                await fetcher.wait_turn(host)
        else:
            await fetcher.wait_turn(host)
        turns.append((name, clock.now))

    await take_turn("latest filings", "www.sec.gov")  # SEC is free, so this goes at once
    research = [
        asyncio.create_task(take_turn(f"research {i}", "data.sec.gov", background=True))
        for i in range(2)
    ]
    await asyncio.sleep(0)
    polls = [asyncio.create_task(take_turn(f"poll {i}", "data.sec.gov")) for i in range(3)]
    await asyncio.gather(*research, *polls)

    names = [name for name, _ in turns]
    # Every poll that was waiting went before the research that asked earlier
    assert names[:4] == ["latest filings", "poll 0", "poll 1", "poll 2"]
    times = dict(turns)
    assert times["research 1"] - times["research 0"] >= 0.4 - 1e-9


@pytest.mark.usefixtures("clock")
async def test_background_research_gives_way_to_a_host_a_window_is_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = Fetcher("Test Person test@example.com")
    await fetcher.wait_turn("investors.example.com")  # a window's poll
    with fetcher.background(chrome=False), pytest.raises(HostBusy):
        await fetcher.wait_turn("investors.example.com")
    # A host no window polls is fine, and so is the polled one once it has been quiet a while
    with fetcher.background(chrome=False):
        await fetcher.wait_turn("www.another-issuer.com")
    monkeypatch.setattr("earnings_bot.fetch.LIVE_SECONDS", 0.0)
    with fetcher.background(chrome=False):
        await fetcher.wait_turn("investors.example.com")


async def test_background_research_without_chrome_cannot_open_it() -> None:
    fetcher = Fetcher("Test Person test@example.com")
    assert fetcher.chrome_allowed()
    with fetcher.background(chrome=True):
        assert fetcher.chrome_allowed()
    with fetcher.background(chrome=False):
        assert not fetcher.chrome_allowed()
        with pytest.raises(BrowserError):
            async with fetcher.research_browser():
                pass


async def test_background_research_cannot_use_the_shared_live_browser() -> None:
    class LiveBrowser:
        async def render(self, *args: object) -> None:
            raise AssertionError("Research reached the live browser")

    fetcher = Fetcher("Test Person test@example.com")
    fetcher._session = object()  # type: ignore[assignment]
    fetcher._browser = LiveBrowser()  # type: ignore[assignment]
    with fetcher.background(chrome=False), pytest.raises(BrowserError):
        await fetcher.browser_render("https://issuer.example/results", 1.0)


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize("condition", ["active", "paused"])
async def test_queued_background_research_yields_without_spending_a_turn(condition: str) -> None:
    """A window opens (`active`) or the hot interval begins (`paused`) while research waits
    in SEC's queue: it gives up its place instead of taking the turn."""
    fetcher = Fetcher("Test Person test@example.com")
    state = {condition: False}
    await fetcher.wait_turn("data.sec.gov")
    reserved_until = fetcher._queues["sec.gov"].next_turn

    async def research() -> None:
        with fetcher.background(chrome=True, **{condition: lambda: state[condition]}):
            with pytest.raises(HostBusy):
                await fetcher.wait_turn("data.sec.gov")
            assert fetcher.research_deferred()

    task = asyncio.create_task(research())
    await asyncio.sleep(0)
    state[condition] = True
    await task
    assert fetcher._queues["sec.gov"].next_turn == reserved_until
    await fetcher.wait_turn("data.sec.gov")


async def test_fetcher_gives_up_on_a_server_that_never_answers() -> None:
    async def accept_and_say_nothing(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read()  # until the client gives up and closes the connection
        writer.close()

    server = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await fetcher.request("GET", f"http://127.0.0.1:{port}/", give_up_after=0.2)
        assert time.monotonic() - started < 1


async def test_fetcher_reads_a_header_longer_than_aiohttps_default() -> None:
    # Some of cloudflare.com's 404 pages carry a 13 KB policy header
    policy = "script-src 'self' " + "cdn.example " * 1200

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            "HTTP/1.1 404 Not Found\r\n"
            f"Content-Security-Policy-Report-Only: {policy}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        response = await fetcher.request("HEAD", f"http://127.0.0.1:{port}/")
    assert response.status == 404
    assert len(response.headers["content-security-policy-report-only"]) > 13_000


async def test_fetcher_doesnt_send_cookies_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_module, "_DEFAULT_GAP", 0.0)
    # cloudflare.com answers 500 to requests that carry the cookies it set
    requests: list[bytes] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: visitor=1; Path=/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        for _ in range(2):
            # A host name, since aiohttp's usual cookie jar ignores cookies from IP addresses
            # anyway, and this test would pass without the fix
            await fetcher.request("GET", f"http://localhost:{port}/")
    assert len(requests) == 2
    assert b"cookie" not in requests[1].lower()


async def test_an_urgent_request_takes_the_hosts_next_turn_but_keeps_its_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_module, "host_gap", lambda host: 0.05)
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[tuple[str, float]] = []

    async def take_turn(name: str, urgent: bool = False) -> None:
        await fetcher.wait_turn("example.com", urgent)
        turns.append((name, time.monotonic()))

    await take_turn("poll")  # the host is free, so this goes at once
    queued = [asyncio.create_task(take_turn(f"poll {i}")) for i in range(3)]
    await asyncio.sleep(0)  # they line up behind it
    await take_turn("release text", urgent=True)
    await asyncio.gather(*queued)

    assert [name for name, _ in turns] == ["poll", "release text", "poll 0", "poll 1", "poll 2"]
    gaps = [b - a for (_, a), (_, b) in itertools.pairwise(turns)]
    assert min(gaps) >= 0.045


async def test_fetcher_reports_how_long_a_request_waited_for_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_module, "host_gap", lambda host: 0.2)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, Fetcher("Test Person test@example.com") as fetcher:
        url = f"http://127.0.0.1:{port}/"
        first, second = await asyncio.gather(
            fetcher.request("GET", url), fetcher.request("GET", url)
        )
    assert first.queued < 0.05
    assert 0.15 < second.queued < 0.4
