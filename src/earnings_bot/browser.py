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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urldefrag, urlsplit

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
    url: str = ""


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


async def _skip_browser_cache(ws: aiohttp.ClientWebSocketResponse, **network: Any) -> None:
    """Open the tab's pages without Chrome's own cache. A navigation would otherwise take the
    page, and the data its scripts load, from an earlier copy for as long as the site's
    Cache-Control allows. Requests made with fetch() already pass `cache: 'no-store'`."""
    await _command(ws, "Network.enable", **network)
    await _command(ws, "Network.setCacheDisabled", cacheDisabled=True)


class _SlowAnswer(Exception):
    """The page gave up on a slow answer itself. Its tab is still good."""


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
(async (url, limit, patience) => {
  try {
    // An unpublished file can redirect to a generic page. Keep the tab warm and never follow it.
    // A slow answer is given up on in the page, so the tab itself stays usable.
    const response = await fetch(
        url, {cache: 'no-store', redirect: 'manual', signal: AbortSignal.timeout(patience)}
    );
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
  } catch (error) {
    if (error.name === 'TimeoutError' || error.name === 'AbortError') return {slow: true};
    throw error;
  }
})
"""


@dataclass(frozen=True)
class RenderedPage:
    """A page as a person sees it, after its scripts have built it."""

    url: str  # where it ended up, after any redirects
    status: int  # the page's HTTP status, or 0 when Chrome didn't say
    title: str
    text: str  # what it shows, one line per block
    links: list[tuple[str, str]]  # each link's absolute URL and visible text
    frames: list[str]  # pages it embeds
    # The text of the small block around each link, such as an event's card with its date
    cards: list[str] = field(default_factory=list)


# Enough for a long press release or a list of a few hundred releases
MAX_RENDERED_TEXT = 400_000
_SETTLED = (
    "[document.readyState, document.links.length,"
    " document.body ? document.body.innerText.length : 0]"
)
_READ_PAGE = """
((limit) => {
    const links = [];
    const tidy = (text) => (text || '').replace(/\\s+/g, ' ').trim();
    for (const a of document.querySelectorAll('a[href]')) {
        const text = tidy(a.innerText || a.getAttribute('aria-label') || a.title);
        if (!text || !/^https:/.test(a.href)) continue;
        // The largest block around the link that is still one item: its card in a list
        let card = a;
        for (let up = a.parentElement, i = 0; up && i < 4; up = up.parentElement, i++) {
            if (tidy(up.innerText).length > 400) break;
            card = up;
        }
        links.push([a.href, text.slice(0, 300), tidy(card.innerText).slice(0, 400)]);
        if (links.length >= 3000) break;
    }
    const frames = [...document.querySelectorAll('iframe[src]')]
        .map(frame => frame.src).filter(src => /^https:/.test(src)).slice(0, 20);
    const navigation = performance.getEntriesByType('navigation')[0];
    return {
        url: location.href,
        status: (navigation && navigation.responseStatus) || 0,
        title: document.title || '',
        text: (document.body ? document.body.innerText : '').slice(0, limit),
        links,
        frames,
    };
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

    async def render(
        self,
        url: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
    ) -> RenderedPage:
        """Open a page in its own tab and read it once its scripts stop changing it. Many IR
        sites build their release lists in the browser, so the HTML a request gets is empty."""
        host = browser_host(url)
        await self._start()
        await wait_turn(host)
        tab = await self._new_tab(f"render:{host}:{time.monotonic_ns()}")
        try:
            async with asyncio.timeout(give_up_after):
                await _skip_browser_cache(tab.socket)
                navigation = await _command(tab.socket, "Page.navigate", url=url)
                if "errorText" in navigation:
                    raise BrowserError(navigation["errorText"])
                seen: list[object] = []
                started = time.monotonic()
                # Settled: loaded, and the links and text stayed the same for about a second
                while True:
                    await asyncio.sleep(0.3)
                    with suppress(BrowserError):
                        state = await _evaluate(tab.socket, _SETTLED)
                        seen.append(state)
                        if (
                            state
                            and state[0] == "complete"
                            and len(seen) >= 4
                            and seen[-1] == seen[-2] == seen[-3] == seen[-4]
                        ) or time.monotonic() - started > give_up_after - 1.5:
                            break
                page = await _evaluate(tab.socket, f"{_READ_PAGE}({MAX_RENDERED_TEXT})")
            final = urldefrag(str(page["url"])).url
            browser_host(final)  # a redirect to somewhere Chrome may not go isn't read
            return RenderedPage(
                url=final,
                status=int(page["status"] or 0),
                title=str(page["title"]),
                text=str(page["text"]),
                links=[(str(link), str(text)) for link, text, _ in page["links"]],
                frames=[str(frame) for frame in page["frames"]],
                cards=[str(card) for _, _, card in page["links"]],
            )
        except (aiohttp.ClientError, ValueError, KeyError, TypeError) as error:
            raise BrowserError(str(error)) from error
        finally:
            await self._drop(next(key for key, value in self._tabs.items() if value is tab))

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
                # The page gives up first and keeps its tab. Dropping the tab on every slow
                # answer cost a fresh navigation each time: Brookfield's page timed out twice as
                # its Q3 2026 release went up, and each retry took 5 s instead of 3.
                async with asyncio.timeout(give_up_after + 1):
                    result = await _evaluate(
                        tab.socket,
                        f"{_FETCH}({json.dumps(url)}, {MAX_BROWSER_BYTES}, "
                        f"{round(give_up_after * 1000)})",
                    )
                if result.get("slow"):
                    raise _SlowAnswer
                if result.get("oversized"):
                    raise BrowserError("Browser response exceeded the size limit")
                return Response(
                    url,
                    result["status"],
                    {name.lower(): value for name, value in result["headers"].items()},
                    base64.b64decode(result["body"], validate=True),
                    time.monotonic() - started,
                    queued=started - asked,
                )
            except _SlowAnswer:
                raise TimeoutError from None
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
            await _skip_browser_cache(
                ws,
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
                        responses[rid] = (requests[rid], response["url"])
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
                    verb, endpoint = pending.pop(response_id)
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
                        results.append(BrowserJSON(verb, urlsplit(endpoint).path, value, endpoint))
    return results
