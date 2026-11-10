"""A bounded comparison of the normal issuer URL and unique query strings.

Sites often sit behind more than one cache: a CDN such as Cloudflare in front, and the IR
platform's own cache (Varnish and the like) behind it. Each reports itself in its own headers,
so every layer's report counts: a CDN that passes a request through (`cf-cache-status: DYNAMIC`)
says nothing about the cache behind it (`x-cache-hits: 372`)."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.fetch import Response
from earnings_bot.formatting import clock, day
from earnings_bot.sources import bust_cache

CHECKING_CACHE = "Checking issuer cache freshness"
# Headers in which a cache layer says whether it served a stored copy
_STATUS_HEADERS = (
    "cache-status",  # RFC 9211, as in `Cloudflare; hit` or `ExampleCache; fwd=miss`
    "cf-cache-status",
    "x-cache",
    "x-cache-status",
    "x-proxy-cache",
    "x-drupal-cache",
    "x-nginx-cache",
    "akamai-cache-status",
)
_HIT = frozenset({"hit", "stale", "updating"})
_PASS = frozenset({"miss", "bypass", "dynamic", "pass", "expired", "fwd", "uncacheable"})


@dataclass(frozen=True)
class CacheResult:
    summary: str
    query_bust: bool = False  # a unique query string reaches the server behind the cache
    canonical_only: bool = False  # the site rejects query strings, so use the plain URL
    stored_only: bool = False  # every variant got the cache's stored copy


@dataclass(frozen=True)
class CacheRoute:
    label: str
    url: str
    warmup: str | None = None
    revalidate: bool = False


@dataclass(frozen=True)
class _Layers:
    hits: list[str]  # how each layer that served a stored copy said so
    passes: list[str]  # how each layer that went to the server said so


def _age(response: Response) -> int | None:
    age = response.headers.get("age", "")
    return int(age) if age.isascii() and age.isdecimal() and len(age) < 10 else None


def _layers(response: Response) -> _Layers:
    hits: list[str] = []
    passes: list[str] = []
    headers = response.headers
    for name in _STATUS_HEADERS:
        value = headers.get(name, "")
        tokens = set(re.findall(r"[a-z]+", value.lower()))
        if tokens & _HIT:
            hits.append(f"{name}: {value}")
        elif tokens & _PASS:
            passes.append(f"{name}: {value}")
    # Varnish and Fastly count the times their stored copy was served: "372", or "0, 3" per hop
    if counts := [int(n) for n in re.findall(r"\d+", headers.get("x-cache-hits", ""))]:
        (hits if any(counts) else passes).append(f"x-cache-hits: {headers['x-cache-hits']}")
    # Varnish names the request and, on a hit, the request that stored the copy
    if varnish := re.findall(r"\d+", headers.get("x-varnish", "")):
        (hits if len(varnish) > 1 else passes).append("x-varnish")
    age = _age(response)
    if age:
        hits.append(f"age: {age}")
    elif age == 0 and not hits:
        passes.append("age: 0")
    return _Layers(hits, passes)


def _rebuilt(response: Response) -> str:
    """When the stored copy was built, from its Last-Modified header, in Eastern time."""
    try:
        moment = parsedate_to_datetime(response.headers.get("last-modified", ""))
    except (TypeError, ValueError, IndexError):
        return ""
    return f", last rebuilt {day(moment)} at {clock(moment)}"


async def probe_cache(get: Callable[[str], Awaitable[Response]], url: str) -> CacheResult:
    """Two plain requests, then two with unique query strings. A verdict needs a cache layer to
    say what it did; a successful buster also needs equivalent content."""
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
    plain = [_layers(response) for response in normal]
    busted = [_layers(response) for response in changed]
    if any(layers.hits for layers in busted):
        # A unique query string can't have a stored copy of its own, so the cache ignores it
        evidence = next(layers.hits[0] for layers in busted if layers.hits)
        return CacheResult(
            f"Query strings don't get past the cache: a unique query got its stored copy too "
            f"({evidence}{_rebuilt(changed[-1])}). New releases show once the site refreshes it",
            stored_only=True,
        )
    if any(response.body != normal[0].body for response in samples[1:]):
        # A query string could pick a different page, so only the same content counts
        return CacheResult("Responses differed. Query bypass unverified")
    if plain[-1].hits and all(layers.passes for layers in busted):
        return CacheResult(
            f"Query bypass verified: the plain URL is cached ({plain[-1].hits[0]}), "
            f"unique queries reach the server ({busted[-1].passes[0]})",
            query_bust=True,
        )
    if not any(layers.hits for layers in plain) and all(
        layers.passes for layers in (*plain, *busted)
    ):
        return CacheResult(
            f"Not cached: every request reached the server ({plain[-1].passes[0]}). "
            "No query string needed"
        )
    control = normal[-1].headers.get("cache-control", "")
    ttl = re.search(r"(?:^|,\s*)s-maxage\s*=\s*([1-9]\d*)", control, re.IGNORECASE)
    if plain[-1].hits:
        return CacheResult(
            f"The plain URL is cached ({plain[-1].hits[0]}), but no cache reported on the "
            "unique queries. Query bypass unverified"
        )
    if ttl:
        return CacheResult(
            f"Shared TTL {ttl[1]} s advertised, but no cache reported a hit or a miss. "
            "Query bypass unverified"
        )
    return CacheResult("No cache reported a hit or a miss on 4 requests. Freshness unverified")
