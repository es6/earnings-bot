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
