import json
from datetime import date

import pytest

from earnings_bot.estimates import Estimates, EstimatesError, fmp_estimates, format_dollars
from tests.fakes import RoutedFetcher

CALENDAR = [
    {"symbol": "UBER", "date": "2026-08-05", "epsEstimated": 0.83, "revenueEstimated": 14.05e9},
    {"symbol": "UBER", "date": "2026-11-04", "epsEstimated": 0.86, "revenueEstimated": 15.1e9},
    {"symbol": "FICO", "date": "2026-11-04", "epsEstimated": 9.12, "revenueEstimated": None},
]


async def test_fmp_estimates_for_the_nearest_report() -> None:
    fetcher = RoutedFetcher({"earnings-calendar": (200, json.dumps(CALENDAR).encode())})

    estimates = await fmp_estimates(fetcher, "secret", "UBER", date(2026, 11, 4))

    assert estimates == Estimates(eps=0.86, revenue=15.1e9)
    [(_, url, headers)] = fetcher.requests
    assert "from=2026-11-03&to=2026-11-05" in url
    assert "secret" not in url
    assert headers == {"apikey": "secret"}


async def test_fmp_estimates_errors() -> None:
    no_report = RoutedFetcher({"earnings-calendar": (200, b"[]")})
    bad_key = RoutedFetcher({"earnings-calendar": (401, b'{"Error Message": "Invalid API KEY"}')})

    with pytest.raises(EstimatesError, match="no AAPL report"):
        await fmp_estimates(no_report, "secret", "AAPL", date(2026, 11, 2))
    with pytest.raises(EstimatesError, match="HTTP 401"):
        await fmp_estimates(bad_key, "secret", "AAPL", date(2026, 11, 2))


def test_describe_estimates() -> None:
    assert format_dollars(102_351_000_000) == "$102.35B"
    assert format_dollars(860_000_000) == "$860.00M"
    assert Estimates(eps=9.12, revenue=None).describe() == (
        "EPS (the company's headline measure, usually adjusted): $9.12"
    )
    assert Estimates(eps=None, revenue=None).describe() == "None available"
