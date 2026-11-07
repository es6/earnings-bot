"""The Fetcher's pacing: SEC's hosts share one budget, urgent requests go first, and every other
host keeps its own queue."""

import asyncio
import itertools
import socket
import time
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from earnings_bot.fetch import Fetcher, host_gap, queue_key
from earnings_bot.sources import EdgarFilings, EdgarLatest, Sighting
from earnings_bot.text import release_text
from tests.fakes import FakeSession, SessionFetcher

SEC_GAP = 0.125  # 8 requests a second, under SEC's limit of 10
EXHIBIT = "<p>" + "Revenue grew 12 percent. " * 120 + "</p>"
FILING = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101"
INDEX = '{"directory": {"item": [{"name": "aapl-20261106.htm"}, {"name": "ex99-1.htm"}]}}'


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


async def sec(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
    await asyncio.sleep(0.01)
    if url.endswith("index.json"):
        return 200, {}, INDEX.encode()
    if url.endswith("ex99-1.htm"):
        return 200, {}, EXHIBIT.encode()
    return 200, {}, b"{}"


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
    # Each host on its own queue at 8 a second sent all 12 within 0.75 s
    assert most_in_one_second(times) == 8
    gaps = [b - a for a, b in itertools.pairwise(sorted(times))]
    assert min(gaps) >= SEC_GAP - 0.005


async def test_an_urgent_exhibit_download_goes_ahead_of_polls_of_the_other_sec_host() -> None:
    fetcher = Fetcher("Test Person test@example.com")
    turns: list[tuple[str, float]] = []

    async def take_turn(name: str, host: str, urgent: bool = False) -> None:
        await fetcher.wait_turn(host, urgent)
        turns.append((name, time.monotonic()))

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
    assert min(gaps) >= SEC_GAP - 0.005


async def test_other_hosts_dont_wait_behind_a_full_sec_queue() -> None:
    async with Fetcher("Test Person test@example.com") as fetcher:
        sec_turns = [
            asyncio.create_task(fetcher.wait_turn(host))
            for host in ("www.sec.gov", "data.sec.gov") * 10
        ]
        await asyncio.sleep(0)  # 20 SEC requests line up: 2.5 s of SEC's budget
        started = time.monotonic()
        # An IR site's poll and the file CDN's downloads go at once
        for host in ("investors.example.com", "s1.q4cdn.com", "s23.q4cdn.com"):
            await fetcher.wait_turn(host)

        assert time.monotonic() - started < 0.05
        assert sum(turn.done() for turn in sec_turns) <= 1
        for turn in sec_turns:
            turn.cancel()


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
    arrivals: list[tuple[float, str, str]] = []
    retried = False

    async def respond(request: web.Request) -> web.Response:
        nonlocal retried
        arrivals.append((time.monotonic(), request.host.split(":")[0], request.path))
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
            assert response.queued >= 2 * SEC_GAP - 0.03

            # A publisher can also redirect a release link into EDGAR. That hop must use
            # SEC's queue and identity even though the first request was on another site.
            response = await fetcher.request("GET", f"http://news.example.com:{port}/start")
            assert response.body == b"Test SEC client"
    finally:
        await runner.cleanup()

    assert [(host, path) for _, host, path in arrivals] == [
        ("data.sec.gov", "/start"),
        ("www.sec.gov", "/exhibit"),
        ("www.sec.gov", "/exhibit"),
        ("news.example.com", "/start"),
        ("www.sec.gov", "/exhibit"),
    ]
    sec_times = [at for at, host, _ in arrivals if host.endswith(".sec.gov")]
    assert min(b - a for a, b in itertools.pairwise(sec_times)) >= SEC_GAP - 0.02
