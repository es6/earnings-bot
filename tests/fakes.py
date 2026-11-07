import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self, cast

import aiohttp

from earnings_bot.fetch import Fetcher, Response


class RoutedFetcher(Fetcher):
    """Answers each request from the first route whose key appears in the URL."""

    def __init__(
        self, routes: dict[str, tuple[int, bytes]], headers: dict[str, str] | None = None
    ) -> None:
        super().__init__("Test Person test@example.com")
        self.routes = routes
        self.headers = headers or {}
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []
        self.no_redirect: list[str] = []
        self.urgent: list[str] = []  # URLs asked for ahead of the host's polls

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
    ) -> Response:
        self.requests.append((method, url, headers))
        if not allow_redirects:
            self.no_redirect.append(url)
        if urgent:
            self.urgent.append(url)
        for part, (status, body) in self.routes.items():
            if part in url:
                return Response(url, status, self.headers, body, 0.01)
        return Response(url, 404, {}, b"", 0.01)


# Answers one request with (status, headers, body). It can sleep first, to play a slow server.
Answer = Callable[[str, str], Awaitable[tuple[int, dict[str, str], bytes]]]


class FakeSession:
    """Stands in for aiohttp's ClientSession under a real Fetcher, so a test goes through the
    Fetcher's pacing and priorities but never touches the network. `sent` lists each request
    as it leaves the Fetcher: (time.monotonic(), method, url)."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.sent: list[tuple[float, str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        allow_redirects: bool = True,
    ) -> "_FakeReply":
        return _FakeReply(self, method, url, timeout.total if timeout else None)

    async def close(self) -> None:
        pass


class _FakeReply:
    def __init__(self, session: FakeSession, method: str, url: str, limit: float | None) -> None:
        self.session, self.method, self.url, self.limit = session, method, url, limit
        self.status = 0
        self.headers: dict[str, str] = {}
        self._body = b""

    async def __aenter__(self) -> Self:
        self.session.sent.append((time.monotonic(), self.method, self.url))
        # Raises TimeoutError past the limit, like aiohttp
        async with asyncio.timeout(self.limit):
            self.status, self.headers, self._body = await self.session.answer(self.method, self.url)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def read(self) -> bytes:
        return self._body


class SessionFetcher(Fetcher):
    """A real Fetcher, pacing and all, whose requests go to a FakeSession."""

    def __init__(self, session: FakeSession, timeout: float = 10.0) -> None:
        super().__init__("Test Person test@example.com", timeout)
        self.fake = session

    async def __aenter__(self) -> Self:
        self._session = cast(aiohttp.ClientSession, self.fake)
        return self
