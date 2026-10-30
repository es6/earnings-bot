from earnings_bot.fetch import Fetcher, Response


class RoutedFetcher(Fetcher):
    """Answers each request from the first route whose key appears in the URL."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        super().__init__("Test Person test@example.com")
        self.routes = routes
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []

    async def request(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> Response:
        self.requests.append((method, url, headers))
        for part, (status, body) in self.routes.items():
            if part in url:
                return Response(url, status, {}, body, 0.01)
        return Response(url, 404, {}, b"", 0.01)
