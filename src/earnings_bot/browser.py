"""A schedule-scoped stock Chrome worker for the Windows IR trial."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp

from earnings_bot._windows_desktop import DesktopProcess, chrome_path

if TYPE_CHECKING:
    from earnings_bot.fetch import Response


# Names that only mean something inside a home or office network
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".corp")
# Bound the bytes Chrome transfers through its local control socket.
MAX_BROWSER_BYTES = 12 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BrowserJSON:
    method: str
    path: str
    value: object


class BrowserError(Exception):
    """A browser request failed before returning a usable HTTP response."""


class BrowserUnavailable(BrowserError):
    """The browser could not start in this Windows session."""


def _public(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "." in host and not host.endswith(_LOCAL_SUFFIXES)
    return address.is_global


def browser_host(url: str) -> str:
    """The host of a page Chrome may open: any public https site. Chrome runs on this PC, so a
    URL that would reach the PC itself or its network is refused: plain http, another port, a
    login in the URL, a private or loopback address, or a local-only name."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not _public(parts.hostname.rstrip(".").lower())
        or port not in (None, 443)
        or parts.username is not None
        or not parts.path.startswith("/")
        or parts.fragment
    ):
        raise ValueError(f"Not a public https page: {url}")
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

    async def _new_tab(self, host: str) -> _Tab:
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
        return tab

    async def _warm(self, host: str, warmup: str) -> _Tab:
        if tab := self._tabs.get(host):
            return tab
        tab = await self._new_tab(host)
        ws = tab.socket
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

    async def inspect_json(self, url: str) -> list[BrowserJSON]:
        """Observe public JSON requests from a normal navigation without replaying them."""
        host = browser_host(url)
        await self._start()
        async with self._locks.setdefault(host, asyncio.Lock()):
            await self._drop(host)
            try:
                tab = await self._new_tab(host)
                return await _observe_json(tab.socket, url, host)
            finally:
                await self._drop(host)

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


async def _observe_json(
    ws: aiohttp.ClientWebSocketResponse, url: str, host: str
) -> list[BrowserJSON]:
    results: list[BrowserJSON] = []
    requests: dict[str, str] = {}
    responses: dict[str, tuple[str, str]] = {}
    pending: dict[int, tuple[str, str]] = {}
    command = 3
    with suppress(TimeoutError):
        async with asyncio.timeout(6):
            await _command(
                ws,
                "Network.enable",
                maxTotalBufferSize=4 * MAX_JSON_BYTES,
                maxResourceBufferSize=MAX_JSON_BYTES,
            )
            await ws.send_json({"id": 2, "method": "Page.navigate", "params": {"url": url}})
            while len(results) < 12:
                message = await ws.receive_json()
                method = message.get("method")
                params = message.get("params", {})
                rid = params.get("requestId", "")
                if method == "Network.requestWillBeSent":
                    request = params["request"]
                    parts = urlsplit(request["url"])
                    if parts.scheme == "https" and parts.netloc == host:
                        requests[rid] = request["method"]
                    else:
                        requests.pop(rid, None)
                elif method == "Network.responseReceived" and rid in requests:
                    response = params["response"]
                    parts = urlsplit(response["url"])
                    mime = response["mimeType"].lower()
                    if (
                        params["type"] in ("XHR", "Fetch")
                        and parts.scheme == "https"
                        and parts.netloc == host
                        and 200 <= response["status"] < 300
                        and ("json" in mime or "text/plain" in mime)
                        and requests[rid] in ("GET", "POST")
                    ):
                        responses[rid] = (requests[rid], parts.path)
                elif method == "Network.loadingFinished" and rid in responses:
                    candidate = responses.pop(rid)
                    if params["encodedDataLength"] > MAX_JSON_BYTES or command >= 35:
                        continue
                    pending[command] = candidate
                    await ws.send_json(
                        {
                            "id": command,
                            "method": "Network.getResponseBody",
                            "params": {"requestId": rid},
                        }
                    )
                    command += 1
                elif (response_id := message.get("id")) in pending:
                    verb, path = pending.pop(response_id)
                    body = message.get("result", {}).get("body", "")
                    if len(body) > MAX_JSON_BYTES:
                        continue
                    try:
                        if message["result"].get("base64Encoded"):
                            body = base64.b64decode(body)
                        value = json.loads(body)
                    except (ValueError, KeyError):
                        continue
                    if isinstance(value, dict | list):
                        results.append(BrowserJSON(verb, path, value))
    return results
