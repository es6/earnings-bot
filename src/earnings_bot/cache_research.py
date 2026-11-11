"""A bounded comparison of the normal issuer URL, unique query strings and, when queries don't
get past a cache, new spellings of the URL's path.

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
from earnings_bot.sources import Spellings, bust_cache

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
    # Query strings don't get past the cache, but a new spelling of the path does
    spelled: bool = False


@dataclass(frozen=True)
class CacheRoute:
    label: str
    url: str
    warmup: str | None = None
    revalidate: bool = False
    table_period: tuple[int, int] | None = None


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


_TITLE = re.compile(
    rb"<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.DOTALL | re.IGNORECASE
)


def _same_resource(plain: bytes, other: bytes, signature: Callable[[bytes], object] | None) -> bool:
    """Whether another URL returned the same feed or page. A fresh copy can list a release the
    stored one doesn't have yet, so it only needs the same title."""
    if signature:
        return bool(signature(plain)) and signature(plain) == signature(other)
    if plain == other:
        return True
    first, second = _TITLE.search(plain), _TITLE.search(other)
    return bool(first and second and first[1].strip() and first[1] == second[1])


def _modified(response: Response) -> float | None:
    try:
        return parsedate_to_datetime(response.headers.get("last-modified", "")).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


async def _spelled(
    get: Callable[[str], Awaitable[Response]],
    url: str,
    plain: Response,
    stored: str,
    signature: Callable[[bytes], object] | None,
) -> CacheResult | None:
    """Two new spellings of the path, each with a few letters percent-encoded. They count when
    both return the same feed or page and no cache served them a stored copy: a layer says it
    went to the server, or the copy is newer than the plain URL's."""
    spellings = Spellings(url)
    if len(spellings.positions) < 8:
        return None  # too few spellings to poll a release window with
    reached = ""
    for _ in range(2):
        try:
            response = await get(spellings.next())
        except (aiohttp.ClientError, BrowserError, TimeoutError, ValueError):
            return None
        if response.status != 200 or not _same_resource(plain.body, response.body, signature):
            return None
        layers = _layers(response)
        modified, before = _modified(response), _modified(plain)
        newer = modified is not None and before is not None and modified > before
        if layers.hits or not (layers.passes or newer):
            return None
        reached = layers.passes[0] if layers.passes else "a newer Last-Modified"
    return CacheResult(
        f"Query strings don't get past the cache ({stored}{_rebuilt(plain)}), but a new spelling "
        f"of the path reaches the server ({reached}). Polled with a new spelling each time",
        spelled=True,
    )


def _rebuilt(response: Response) -> str:
    """When the stored copy was built, from its Last-Modified header, in Eastern time."""
    try:
        moment = parsedate_to_datetime(response.headers.get("last-modified", ""))
    except (TypeError, ValueError, IndexError):
        return ""
    return f", last rebuilt {day(moment)} at {clock(moment)}"


async def probe_cache(
    get: Callable[[str], Awaitable[Response]],
    url: str,
    signature: Callable[[bytes], object] | None = None,
) -> CacheResult:
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
        stored = _layers(samples[-1]).hits
        if stored and (found := await _spelled(get, url, samples[-1], stored[0], signature)):
            return found
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
        if found := await _spelled(get, url, normal[-1], evidence, signature):
            return found
        return CacheResult(
            f"Query strings don't get past the cache: a unique query got its stored copy too "
            f"({evidence}{_rebuilt(changed[-1])}). New releases show once the site refreshes it",
            stored_only=True,
        )
    if any(not _same_resource(samples[0].body, r.body, signature) for r in samples[1:]):
        # Ignore per-request markup, but reject a different page/feed or table signature.
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
        if found := await _spelled(get, url, normal[-1], plain[-1].hits[0], signature):
            return found
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
