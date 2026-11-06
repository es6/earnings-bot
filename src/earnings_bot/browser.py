"""A schedule-scoped stock Chrome worker for the Windows IR trial."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp

from earnings_bot._windows_desktop import DesktopProcess, chrome_path

if TYPE_CHECKING:
    from earnings_bot.fetch import Response


BROWSER_HOSTS = frozenset(
    {
        "investors.fico.com",
        "ir.crowdstrike.com",
        "investor.lilly.com",
        "ir.hubspot.com",
        "investors.broadcom.com",
        "corporate.mcdonalds.com",
        "ir.homedepot.com",
    }
)
# Bound the bytes Chrome transfers through its local control socket.
MAX_BROWSER_BYTES = 12 * 1024 * 1024


class BrowserError(Exception):
    """A browser request failed before returning a usable HTTP response."""


class BrowserUnavailable(BrowserError):
    """The browser could not start in this Windows session."""


def browser_host(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc not in BROWSER_HOSTS
        or not parts.path.startswith("/")
        or parts.fragment
    ):
        raise ValueError(f"Not an allowed public browser IR URL: {url}")
    return parts.netloc


async def _command(ws: aiohttp.ClientWebSocketResponse, method: str, **params: Any) -> Any:
    # One command at a time per socket. Ignore unrelated DevTools events.
    await ws.send_json({"id": 1, "method": method, "params": params})
    while True:
        message = await ws.receive_json()
        if message.get("id") != 1:
            continue
        if "error" in message:
            raise BrowserError(f"{method}: {message['error'].get('message', 'Chrome error')}")
        return message.get("result", {})


async def _evaluate(ws: aiohttp.ClientWebSocketResponse, expression: str) -> Any:
    result = await _command(
        ws, "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
    )
    if "exceptionDetails" in result:
        raise BrowserError("Chrome could not read the public page")
    return result["result"].get("value")


# Read at most the configured size, including for a response with no Content-Length.
# The browser stays in the tab's origin and the bot drives the polling timer.
_FETCH = """
(async (url, limit) => {
    const response = await fetch(url, {cache: 'no-store', redirect: 'error'});
    const headers = Object.fromEntries(response.headers);
    if (response.status !== 200) {
        await response.body?.cancel();
        return {status: response.status, headers, body: ''};
    }
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > limit) {
            await reader.cancel();
            return {status: response.status, headers, oversized: true};
        }
        chunks.push(value);
    }
    let bytes = '';
    for (const chunk of chunks) {
        for (let i = 0; i < chunk.length; i += 8192) {
            bytes += String.fromCharCode(...chunk.subarray(i, i + 8192));
        }
    }
    return {status: response.status, headers, body: btoa(bytes)};
})
"""


@dataclass
class _Tab:
    id: str
    socket: aiohttp.ClientWebSocketResponse


class DesktopChrome:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._process: DesktopProcess | None = None
        self._browser_socket: str | None = None
        self._base = ""
        self._start_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tabs: dict[str, _Tab] = {}
        self._start_failure: str | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None
        return self._session

    def _local_socket(self, value: str) -> str:
        if not value.startswith(self._base.replace("http:", "ws:", 1) + "/devtools/"):
            raise BrowserUnavailable("Chrome returned a non-local control socket")
        return value

    async def _start(self) -> None:
        async with self._start_lock:
            if self._browser_socket and self._process and self._process.running:
                return
            if self._process:
                await self.close()
            if self._start_failure:
                raise BrowserUnavailable(self._start_failure)
            try:
                exe = chrome_path()
                self._profile = tempfile.TemporaryDirectory(
                    prefix="earnings-bot-chrome-", ignore_cleanup_errors=True
                )
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                self._base = f"http://127.0.0.1:{port}"
                self._process = DesktopProcess(
                    exe,
                    [
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-background-networking",
                        "--disable-sync",
                        "--remote-debugging-address=127.0.0.1",
                        f"--remote-debugging-port={port}",
                        f"--user-data-dir={Path(self._profile.name)}",
                        "about:blank",
                    ],
                )
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
                async with asyncio.timeout(6):
                    while True:
                        try:
                            async with self.session.get(
                                self._base + "/json/version",
                                timeout=aiohttp.ClientTimeout(total=0.4),
                                allow_redirects=False,
                            ) as response:
                                response.raise_for_status()
                                version = await response.json(content_type=None)
                            endpoint = version["webSocketDebuggerUrl"]
                            self._browser_socket = self._local_socket(endpoint)
                            return
                        except (aiohttp.ClientConnectionError, TimeoutError):
                            await asyncio.sleep(0.1)
            except (OSError, TimeoutError, aiohttp.ClientError, ValueError, KeyError) as error:
                self._start_failure = str(error) or "Chrome did not start on the inactive desktop"
                await self.close()
                raise BrowserUnavailable(self._start_failure) from error
            except BaseException:
                await self.close()
                raise

    async def _warm(self, host: str, warmup: str) -> _Tab:
        if tab := self._tabs.get(host):
            return tab
        async with self.session.put(
            self._base + "/json/new?about%3Ablank", allow_redirects=False
        ) as response:
            response.raise_for_status()
            info = await response.json(content_type=None)
        ws = await self.session.ws_connect(
            self._local_socket(info["webSocketDebuggerUrl"]),
            max_msg_size=2 * MAX_BROWSER_BYTES,
        )
        tab = _Tab(info["id"], ws)
        self._tabs[host] = tab
        try:
            async with asyncio.timeout(5):
                navigation = await _command(ws, "Page.navigate", url=warmup)
                if "errorText" in navigation:
                    raise BrowserError(navigation["errorText"])
                origin = f"https://{host}"
                while True:
                    state = await _evaluate(ws, "[location.origin, document.readyState]")
                    if state and state[0] == origin and state[1] != "loading":
                        return tab
                    await asyncio.sleep(0.1)
        except BaseException:
            await self._drop(host)
            raise

    async def _drop(self, host: str) -> None:
        if tab := self._tabs.pop(host, None):
            await tab.socket.close()
            try:
                async with self.session.get(
                    self._base + "/json/close/" + tab.id,
                    timeout=aiohttp.ClientTimeout(total=1),
                    allow_redirects=False,
                ):
                    pass
            except (aiohttp.ClientError, TimeoutError):
                pass

    async def request(
        self,
        url: str,
        warmup: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
    ) -> Response:
        from earnings_bot.fetch import Response

        host = browser_host(url)
        if browser_host(warmup) != host:
            raise ValueError("A browser request must stay on the warm tab's own origin")
        await self._start()
        async with self._locks.setdefault(host, asyncio.Lock()):
            try:
                if host not in self._tabs:
                    await wait_turn(host)
                tab = await self._warm(host, warmup)
                asked = time.monotonic()
                await wait_turn(host)
                started = time.monotonic()
                async with asyncio.timeout(give_up_after):
                    result = await _evaluate(
                        tab.socket, f"{_FETCH}({json.dumps(url)}, {MAX_BROWSER_BYTES})"
                    )
                if result.get("oversized"):
                    raise BrowserError("Browser response exceeded the trial's size limit")
                return Response(
                    url,
                    result["status"],
                    {name.lower(): value for name, value in result["headers"].items()},
                    base64.b64decode(result["body"], validate=True),
                    time.monotonic() - started,
                    queued=started - asked,
                )
            except (aiohttp.ClientError, ValueError, KeyError, TypeError) as error:
                await self._drop(host)
                raise BrowserError(str(error)) from error
            except BaseException:
                await self._drop(host)
                raise

    async def close(self) -> None:
        if self._session:
            try:
                if self._browser_socket:
                    async with asyncio.timeout(2):
                        async with self.session.ws_connect(self._browser_socket) as ws:
                            await ws.send_json({"id": 1, "method": "Browser.close"})
            except (aiohttp.ClientError, TimeoutError):
                pass
            finally:
                for tab in self._tabs.values():
                    await tab.socket.close()
                await self.session.close()
                self._tabs.clear()
                self._session = None
                self._browser_socket = None
        if self._process:
            await asyncio.to_thread(self._process.close)
            self._process = None
        if self._profile:
            self._profile.cleanup()
            self._profile = None
