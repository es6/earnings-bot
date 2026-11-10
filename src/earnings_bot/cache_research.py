"""A bounded comparison of the normal issuer URL and unique query strings."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.fetch import Response
from earnings_bot.sources import bust_cache

CHECKING_CACHE = "Checking issuer cache freshness"
_CACHE_HEADERS = ("cache-status", "cf-cache-status", "x-cache", "x-proxy-cache")


@dataclass(frozen=True)
class CacheResult:
    summary: str
    query_bust: bool = False
    canonical_only: bool = False


@dataclass(frozen=True)
class CacheRoute:
    label: str
    url: str
    warmup: str | None = None
    revalidate: bool = False


def _tokens(response: Response) -> set[str]:
    return set(
        re.findall(r"[a-z]+", " ".join(response.headers.get(h, "") for h in _CACHE_HEADERS).lower())
    )


def _age(response: Response) -> int | None:
    age = response.headers.get("age", "")
    return int(age) if age.isascii() and age.isdecimal() and len(age) < 10 else None


def _hit(response: Response) -> bool:
    return (_age(response) or 0) > 0 or "hit" in _tokens(response)


def _fresh(response: Response) -> bool:
    return not _hit(response) and (
        _age(response) == 0 or bool(_tokens(response) & {"miss", "bypass", "dynamic"})
    )


async def probe_cache(get: Callable[[str], Awaitable[Response]], url: str) -> CacheResult:
    """A successful buster needs repeatable cache evidence and equivalent content."""
    samples: list[Response] = []
    rejected: list[int] = []
    for fresh in (False, False, True, True):
        candidate = bust_cache(url) if fresh else url
        try:
            response = await get(candidate)
        except (aiohttp.ClientError, BrowserError, TimeoutError, ValueError):
            return CacheResult("Request failed during the cache comparison")
        if response.status != 200 or not response.body:
            if fresh and response.status in (400, 403, 404, 410, 422):
                rejected.append(response.status)
                continue
            status = f"HTTP {response.status}" if response.status else "no response"
            return CacheResult(f"Cache comparison stopped at {status}")
        samples.append(response)
    if len(rejected) == 2:
        statuses = "/".join(str(status) for status in sorted(set(rejected)))
        return CacheResult(
            f"Normal URL readable. Both queries rejected (HTTP {statuses})", canonical_only=True
        )
    if rejected:
        return CacheResult("Normal URL readable. Query response inconsistent")
    normal, changed = samples[:2], samples[2:]
    if any(response.body != normal[0].body for response in samples[1:]):
        return CacheResult("Responses differed. Cache bypass unverified")
    if _hit(normal[-1]) and all(_fresh(response) for response in changed):
        return CacheResult("Query bypass verified at the responding cache", query_bust=True)
    if any(_hit(response) for response in samples):
        return CacheResult("Cache hits observed. Query bypass unverified")
    control = normal[-1].headers.get("cache-control", "")
    ttl = re.search(r"(?:^|,\s*)s-maxage\s*=\s*([1-9]\d*)", control, re.IGNORECASE)
    if ttl:
        return CacheResult(f"Shared TTL {ttl[1]} s advertised. Query bypass unverified")
    return CacheResult("No shared-cache evidence in the samples. Freshness unverified")
