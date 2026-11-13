"""Bounded publication comparisons that only journal observations; never post alerts.

Configured trials share the live Fetcher, host pacing and Chrome process. Their turns
wait behind monitoring; Chrome uses a separate trial tab. An isolated CLI is also
available for a bot-free pilot. No model client or Discord publisher is created here.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from earnings_bot.browser import MAX_BROWSER_BYTES, CacheMode, browser_host
from earnings_bot.config import RecorderSettings
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.recorder import Journal
from earnings_bot.sec_results import is_results_release
from earnings_bot.text import html_to_text, pdf_text

_CACHE_HEADERS = {
    "date",
    "age",
    "cache-control",
    "vary",
    "etag",
    "last-modified",
    "retry-after",
    "cache-status",
    "cf-cache-status",
    "x-cache",
    "x-cache-hits",
    "x-served-by",
}
_SECRET_PARAMETER = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|token|auth|password)$", re.IGNORECASE
)


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(_SECRET_PARAMETER.fullmatch(key) for key, _ in pairs):
        return url
    return urlunsplit(
        parts._replace(
            query=urlencode(
                [
                    (key, "REDACTED" if _SECRET_PARAMETER.fullmatch(key) else value)
                    for key, value in pairs
                ]
            )
        )
    )


class TrialRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=80)
    url: str
    transport: Literal["http", "chrome"] = "http"
    kind: Literal["listing", "api", "article", "file", "sec_directory"] = "listing"
    warmup: str | None = None
    interval: float = Field(default=5, ge=0.125, le=60)
    minute_radius: float = Field(default=0, ge=0, le=10)
    burst_interval: float = Field(default=1, ge=0.125, le=60)
    headers: dict[str, str] = Field(default_factory=dict)
    cache_mode: CacheMode = "default"
    bypass_service_worker: bool = False
    issuer_pattern: str
    period_pattern: str
    result_pattern: str
    identifiers_pattern: str = r"\b\d{10}-?\d{2}-?\d{6}\b"
    pause_key: str | None = None

    @field_validator("issuer_pattern", "period_pattern", "result_pattern", "identifiers_pattern")
    @classmethod
    def pattern(cls, value: str) -> str:
        if not value or len(value) > 500:
            raise ValueError("An explicit bounded pattern is required")
        re.compile(value)
        return value

    @model_validator(mode="after")
    def public_route(self) -> Self:
        try:
            host = browser_host(self.url)
            if self.warmup is not None and browser_host(self.warmup) != host:
                raise ValueError("Warmup origin mismatch")
        except ValueError as error:
            raise ValueError(
                "Trial routes require public HTTPS and a same-origin warmup"
            ) from error
        if {key.lower() for key in self.headers} - {
            "accept",
            "accept-language",
            "cache-control",
            "pragma",
        }:
            raise ValueError("Only scoped representation/cache headers are permitted")
        if self.transport == "chrome" and (not self.warmup or self.headers):
            raise ValueError("Chrome trials require a warmup and no custom HTTP headers")
        return self

    def cadence(self, now: datetime) -> float:
        second = now.second + now.microsecond / 1_000_000
        if self.minute_radius and min(second, 60 - second) <= self.minute_radius:
            return min(self.interval, self.burst_interval)
        return self.interval


class TrialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    duration_seconds: float = Field(default=300, ge=1, le=3600)
    max_requests: int = Field(default=60, ge=1, le=120)
    max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=MAX_BROWSER_BYTES)
    routes: list[TrialRoute] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def distinct_controls(self) -> Self:
        if self.starts_at.tzinfo is None or len({r.name for r in self.routes}) != len(self.routes):
            raise ValueError("Use an explicit start timezone and unique route names")
        return self

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(seconds=self.duration_seconds)


class TrialFinished(Exception):
    pass


class TrialBudget:
    def __init__(self, plan: TrialPlan, db: sqlite3.Connection, journal: Journal) -> None:
        self.plan, self.db, self.journal = plan, db, journal
        self.closed = False
        self.deadline = time.monotonic() + max(0, (plan.ends_at - journal.now()).total_seconds())
        digest = hashlib.sha256(
            json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_trials "
                "(id TEXT PRIMARY KEY, plan TEXT NOT NULL, turns INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO publication_trials VALUES (?, ?, 0)", (plan.id, digest)
            )
        if (
            db.execute("SELECT plan FROM publication_trials WHERE id = ?", (plan.id,)).fetchone()[0]
            != digest
        ):
            raise ValueError("A trial ID cannot be reused for a different plan")

    @property
    def turns(self) -> int:
        return self.db.execute(
            "SELECT turns FROM publication_trials WHERE id = ?", (self.plan.id,)
        ).fetchone()[0]

    def check(self) -> None:
        if (
            self.closed
            or time.monotonic() >= self.deadline
            or self.journal.now() >= self.plan.ends_at
        ):
            raise TrialFinished

    def admit(self, host: str) -> None:
        self.check()
        with self.db:
            changed = self.db.execute(
                "UPDATE publication_trials SET turns = turns + 1 WHERE id = ? AND turns < ?",
                (self.plan.id, self.plan.max_requests),
            ).rowcount
        if not changed:
            raise TrialFinished


def _retry_pause(response: Response, now: datetime) -> float:
    raw = response.headers.get("retry-after", "")
    if raw.isdigit():
        return float(raw)
    with suppress(ValueError, TypeError, OverflowError):
        stamp = parsedate_to_datetime(raw)
        return max(0, (stamp.astimezone(UTC) - now).total_seconds())
    return 30


async def _read_text(body: bytes) -> str:
    if body.startswith(b"%PDF-"):
        return await asyncio.to_thread(pdf_text, body) or ""
    text = body.decode("utf-8", errors="replace")
    return html_to_text(text) if "<" in text[:2000] else text


async def run_trials(
    plan: TrialPlan, fetcher: Fetcher, journal: Journal, db: sqlite3.Connection
) -> None:
    """Share this Fetcher with monitoring. A plan's durable request budget survives restart.
    Pattern matches are candidates; only verified full reports are marked usable."""
    budget: TrialBudget | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        # Waits for the start on the journal's trusted clock, checked every 30 s at most
        while journal.now() < plan.starts_at:  # noqa: ASYNC110
            await asyncio.sleep(min(30, (plan.starts_at - journal.now()).total_seconds()))
        if journal.now() >= plan.ends_at:
            journal.write("publication_trial_skipped", trial=plan.id, reason="period already ended")
            return
        budget = TrialBudget(plan, db, journal)
        journal.write(
            "publication_trial_started",
            trial=plan.id,
            turns=budget.turns,
            max_requests=plan.max_requests,
            ends_at=plan.ends_at,
            clock_synced=journal.clock.synced is not None,
        )

        async def observe(route: TrialRoute) -> None:
            absent = False
            first = True
            last_digest: str | None = None
            parts = urlsplit(route.url)
            pause_key = route.pause_key or urlunsplit(parts._replace(query=""))
            pause = fetcher.route_pause(pause_key)
            patterns = [
                re.compile(p, re.IGNORECASE | re.DOTALL)
                for p in (route.issuer_pattern, route.period_pattern, route.result_pattern)
            ]
            assert budget is not None
            while True:
                try:
                    budget.check()
                    if pause.left():
                        await asyncio.sleep(
                            min(pause.left(), max(0, budget.deadline - time.monotonic()))
                        )
                        continue
                    sent = journal.now()
                    started = time.monotonic()
                    response = (
                        await fetcher.browser_request(
                            route.url,
                            route.warmup or route.url,
                            8,
                            cache_mode=route.cache_mode,
                            bypass_service_worker=route.bypass_service_worker,
                            max_bytes=plan.max_response_bytes,
                        )
                        if route.transport == "chrome"
                        else await fetcher.request(
                            "GET",
                            route.url,
                            route.headers,
                            give_up_after=8,
                            allow_redirects=False,
                            max_bytes=plan.max_response_bytes,
                        )
                    )
                    received = journal.now()
                    parsing = time.monotonic()
                    text = await _read_text(response.body)
                    matched = response.status == 200 and all(p.search(text) for p in patterns)
                    usable = (
                        matched
                        and route.kind in {"article", "file"}
                        and is_results_release(text, "")
                    )
                    digest = hashlib.sha256(response.body).hexdigest()
                    journal.write(
                        "publication_trial",
                        trial=plan.id,
                        route=route.name,
                        transport=route.transport,
                        kind=route.kind,
                        url=safe_url(route.url),
                        url_sha256=hashlib.sha256(route.url.encode()).hexdigest(),
                        sent=sent,
                        received=received,
                        status=response.status,
                        bytes=len(response.body),
                        sha256=digest,
                        content_changed=last_digest is not None and digest != last_digest,
                        candidate_visible=matched,
                        usable_report=usable,
                        already_present=first and matched,
                        transition_observed=absent and matched,
                        identifiers=[m[0] for m in re.finditer(route.identifiers_pattern, text)][
                            :100
                        ],
                        network_ms=round(response.elapsed * 1000),
                        queued_ms=round(response.queued * 1000),
                        parse_ms=round((time.monotonic() - parsing) * 1000),
                        total_ms=round((time.monotonic() - started) * 1000),
                        **response.waits,
                        headers={
                            key: value
                            for key, value in response.headers.items()
                            if key in _CACHE_HEADERS
                        },
                    )
                    first = False
                    last_digest = digest
                    if response.status == 200 and not matched:
                        absent = True
                    if response.status == 403:
                        pause.refused()
                    elif response.status == 429 or "retry-after" in response.headers:
                        pause.hold(_retry_pause(response, journal.now()))
                    elif response.status == 200:
                        pause.answered()
                except TrialFinished:
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    journal.write(
                        "publication_trial_error",
                        trial=plan.id,
                        route=route.name,
                        error_type=type(error).__name__,
                    )
                await asyncio.sleep(
                    min(route.cadence(journal.now()), max(0, budget.deadline - time.monotonic()))
                )

        with fetcher.observing(budget.admit, budget.check):
            tasks = [asyncio.create_task(observe(route)) for route in plan.routes]
        async with asyncio.timeout(max(0, budget.deadline - time.monotonic())):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as error:
        journal.write("publication_trial_error", trial=plan.id, error_type=type(error).__name__)
    finally:
        if budget:
            budget.closed = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await fetcher.close_observations()
        if budget:
            journal.write("publication_trial_ended", trial=plan.id, turns=budget.turns)


TRIALS_FOLDER = "trials"
TRIALS_STATE = ".publication-trials.sqlite3"


def load_plans(folder: Path) -> tuple[list[TrialPlan], list[str]]:
    """The record-only trial plans in a schedule folder's `trials/`, and why any file isn't
    one. A plan never posts; it only writes `publication_trial` events to the journal."""
    plans, problems = [], []
    for path in sorted((folder / TRIALS_FOLDER).glob("*.json")):
        try:
            plans.append(TrialPlan.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            problems.append(f"{path.name}: {str(error).splitlines()[0][:200]}")
    return plans, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--journal", type=Path, default=Path("publication-trials.jsonl"))
    parser.add_argument("--state", type=Path, default=Path("publication-trials.sqlite3"))
    args = parser.parse_args()
    plan = TrialPlan.model_validate_json(args.config.read_text(encoding="utf-8"))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "id": plan.id,
                    "routes": [r.name for r in plan.routes],
                    "max_requests": plan.max_requests,
                    "starts_at": plan.starts_at.isoformat(),
                    "ends_at": plan.ends_at.isoformat(),
                    "record_only": True,
                }
            )
        )
        return

    async def run() -> None:
        settings = RecorderSettings()  # pyright: ignore[reportCallIssue]
        with args.journal.open("a", encoding="utf-8") as file, sqlite3.connect(args.state) as db:
            journal = Journal(file)
            await asyncio.to_thread(journal.clock.sync)
            async with Fetcher(settings.sec_user_agent) as fetcher:
                await run_trials(plan, fetcher, journal, db)

    asyncio.run(run())


if __name__ == "__main__":
    main()
