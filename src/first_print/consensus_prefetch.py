"""Background-only Alpha Vantage cache, with durable shared quota and backoff."""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from first_print.calendar import Tracked
from first_print.consensus import calendar_rows, estimate_rows
from first_print.delivery import DeliveryStore
from first_print.recorder import ET, Target

DAILY_CALLS = 20  # Leave headroom below the provider's free quota, across all servers.
SPACING = 2.1


async def request(key: str, function: str, params: dict[str, str]) -> str:
    # Separate from Fetcher: credential-bearing URLs must never enter its request journal.
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session,
        session.get(
            "https://www.alphavantage.co/query",
            params={"function": function, **params, "apikey": key},
            allow_redirects=False,
        ) as response,
    ):
        if response.status != 200:
            raise ValueError("Estimate provider refused the request")
        body = bytearray()
        async for chunk in response.content.iter_chunked(64_000):
            body.extend(chunk)
            if len(body) > 4_000_000:
                raise ValueError("Estimate provider response is too large")
        return body.decode("utf-8-sig")


class ConsensusPrefetch:
    def __init__(
        self,
        store: DeliveryStore,
        key: str,
        now: Callable[[], datetime],
        paused: Callable[[], bool],
        get: Callable[[str, str, dict[str, str]], Awaitable[str]] = request,
    ) -> None:
        self.db, self.key, self.paused, self.get = store.db, key, paused, get
        self.now = lambda: now().astimezone(UTC)
        self.identity = hashlib.sha256(key.encode()).hexdigest()
        self.lock = store.lock("alpha-vantage")
        self.notes: dict[int, str] = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS consensus_calls (identity TEXT, sent TEXT);
            CREATE TABLE IF NOT EXISTS consensus_cache (
                name TEXT PRIMARY KEY, fetched TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consensus_backoff (
                identity TEXT PRIMARY KEY, until_at TEXT NOT NULL
            );
        """)

    def _cached(self, name: str, now: datetime) -> tuple[datetime, Any] | None:
        row = self.db.execute(
            "SELECT fetched, data FROM consensus_cache WHERE name = ?", (name,)
        ).fetchone()
        if row:
            fetched = datetime.fromisoformat(row[0])
            if timedelta(0) <= now - fetched < timedelta(days=1):
                return fetched, json.loads(row[1])
        return None

    def _save(self, name: str, data: Any) -> datetime:
        fetched = self.now()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO consensus_cache VALUES (?, ?, ?)",
                (name, fetched.isoformat(), json.dumps(data)),
            )
        return fetched

    async def _ask(self, function: str, params: dict[str, str]) -> str | None:
        if self.paused():
            return None
        # Calls from other DB connections/processes are admitted by the same transaction.
        latest = self.db.execute(
            "SELECT MAX(sent) FROM consensus_calls WHERE identity = ?", (self.identity,)
        ).fetchone()[0]
        if latest:
            delay = SPACING - (self.now() - datetime.fromisoformat(latest)).total_seconds()
            if delay > 0:
                await asyncio.sleep(min(delay, SPACING))
        if self.paused():
            return None
        now = self.now()
        with self.db:
            claimed = self.db.execute(
                """INSERT INTO consensus_calls SELECT ?, ?
                WHERE (SELECT COUNT(*) FROM consensus_calls WHERE identity=? AND sent>?) < ?
                AND NOT EXISTS (SELECT 1 FROM consensus_backoff WHERE identity=? AND until_at>?)
                AND NOT EXISTS (SELECT 1 FROM consensus_calls WHERE identity=? AND sent>?)""",
                (
                    self.identity,
                    now.isoformat(),
                    self.identity,
                    (now - timedelta(days=1)).isoformat(),
                    DAILY_CALLS,
                    self.identity,
                    now.isoformat(),
                    self.identity,
                    (now - timedelta(seconds=SPACING)).isoformat(),
                ),
            ).rowcount
        if not claimed:
            return None
        try:
            raw = await self.get(self.key, function, params)
            if raw.lstrip().startswith("{"):
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Estimate provider response identity unavailable")
                if any(k in data for k in ("Note", "Information", "Error Message")):
                    raise ValueError("Estimate provider returned an availability notice")
            return raw
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            # Never log a URL or exception containing the API key; no retries on refusal.
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO consensus_backoff VALUES (?, ?)",
                    (self.identity, (self.now() + timedelta(days=1)).isoformat()),
                )
            return None

    async def ensure(self, row: Tracked, now: datetime) -> None:
        target = row.target
        if target is None or target.period is None or self.paused():
            return
        # Fetch only near an upcoming event, never while any hot window is open.
        if not timedelta(minutes=2) < target.window[0] - now < timedelta(hours=23):
            return
        name = f"snapshot:{target.ticker}:{target.period}:{target.window[0].isoformat()}"
        async with self.lock:
            if cached := self._cached(name, self.now()):
                self.notes[row.cik] = self._note(cached[1])
                return
            self.notes[row.cik] = "Estimates unavailable; no verified pre-release snapshot"
            calendar = self._cached("calendar", self.now())
            if calendar is None:
                raw = await self._ask("EARNINGS_CALENDAR", {"horizon": "12month"})
                if raw is None:
                    return
                try:
                    values = calendar_rows(raw)
                except ValueError:
                    return
                calendar = self._save("calendar", values), values
            matches = [
                r
                for r in calendar[1]
                if r["symbol"] == target.ticker
                and r["reportDate"] == target.expected.astimezone(ET).date().isoformat()
            ]
            if len(matches) != 1:
                self.notes[row.cik] = "Estimate currency/period unverified; estimates hidden"
                return
            if not re.fullmatch(r"[A-Z]{3}", matches[0]["currency"]):
                self.notes[row.cik] = "Estimate currency unverified; estimates hidden"
                return
            if self.paused() or self.now() >= target.window[0]:
                return
            raw = await self._ask("EARNINGS_ESTIMATES", {"symbol": target.ticker})
            if raw is None:
                return
            try:
                estimates = estimate_rows(json.loads(raw), target.ticker, matches[0]["currency"])
            except (ValueError, TypeError):
                return
            # Calendar EPS currency is verified for this exact fiscal period only.
            estimates = [
                r
                for r in estimates
                if r["ending"] == matches[0]["fiscalDateEnding"]
                and r["horizon"] == "fiscal quarter"
            ]
            fetched = self.now()
            if not estimates or fetched >= target.window[0] or self.paused():
                return
            snapshot = {
                "symbol": target.ticker,
                "period": target.period,
                "fetched_at": fetched.isoformat(),
                "window_start": target.window[0].isoformat(),
                "provider": "Alpha Vantage EARNINGS_ESTIMATES + EARNINGS_CALENDAR",
                "calendar_fetched_at": calendar[0].isoformat(),
                "report_date": matches[0]["reportDate"],
                "estimates": estimates,
            }
            self._save(name, snapshot)
            self.notes[row.cik] = self._note(snapshot)

    @staticmethod
    def _note(snapshot: dict[str, Any]) -> str:
        revenue = any(
            r.get("metric") == "revenue" and r.get("currency")
            for r in snapshot.get("estimates", [])
        )
        return (
            "Revenue estimates cached; EPS basis unverified, hidden"
            if revenue
            else "Estimates cached; revenue currency and EPS basis unverified, hidden"
        )

    def context(self, target: Target) -> dict[str, Any]:
        name = f"snapshot:{target.ticker}:{target.period}:{target.window[0].isoformat()}"
        cached = self._cached(name, self.now())
        return {"consensus_snapshot": cached[1]} if cached else {}
