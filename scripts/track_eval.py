"""Run /track's research on many tickers without Discord, and report what it found.

    uv run python scripts/track_eval.py TDG NBIS AZTA --workers 3 --speed --out eval.json

Each ticker goes through the same steps as /track: SEC lookup, investigation, date
discovery and (with --speed) the speed review. It runs against the live internet, so it
paces SEC more slowly than the bot does, leaving the running bot room under SEC's limit.
"""

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from earnings_bot import fetch
from earnings_bot.browser import DesktopChrome
from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.issuers import Issuers
from earnings_bot.recorder import ET
from earnings_bot.source_audit import audit_sources


class EvalFetcher(fetch.Fetcher):
    """One Chrome per research, without the bot's one-at-a-time lock, so workers overlap."""

    @asynccontextmanager
    async def research_browser(self) -> AsyncIterator[DesktopChrome]:
        browser = DesktopChrome()
        try:
            yield browser
        finally:
            await browser.close()


@dataclass
class Outcome:
    ticker: str
    seconds: float = 0.0
    date: str = ""
    evidence: str = ""
    phase: str = ""
    error: str = ""
    form: str = ""
    wire: str = ""
    watch: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    speed: list[str] = field(default_factory=list)
    speed_day: str = ""


async def evaluate(
    ticker: str, fetcher: fetch.Fetcher, fmp: str | None, speed: bool, directory: Path
) -> Outcome:
    outcome = Outcome(ticker)
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        store = DeliveryStore(Path(folder) / "state.sqlite3", 1)
        calendar = Calendar(store)
        discovery = DateDiscovery(
            calendar, fetcher, lambda: datetime.now(ET), lambda: False, lambda _: None, fmp
        )
        try:
            today = discovery.now().date()
            company = await Issuers(fetcher).company(ticker, today)
            row, _ = calendar.add(ticker, company)
            await discovery.run_once(row)
            row = calendar.find(ticker)
            assert row is not None
            outcome.form = row.company.sec_form or ""
            outcome.wire = row.company.wire
            outcome.watch = [f"{c.source}: {c.why}" for c in row.company.checks if c.watch]
            outcome.rejected = [f"{c.source}: {c.why}" for c in row.company.checks if not c.watch]
            if row.target and row.evidence:
                outcome.date = f"{row.target.expected:%a %b %d %H:%M} {row.target.period}"
                outcome.evidence = row.evidence
            else:
                outcome.phase = discovery.phases.get(row.cik, "")
            if speed:
                result = await audit_sources(
                    fetcher, directory, ticker, row.company, discovery.now(), None
                )
                basis, timings = result.fastest()
                outcome.speed_day = str(result.day or "")
                outcome.speed = [f"{t.source} {t.at.astimezone(ET):%H:%M:%S}" for t in timings]
                if basis:
                    outcome.speed.insert(0, basis)
        except Exception as error:  # the report shows every failure
            outcome.error = f"{type(error).__name__}: {error}"
        finally:
            store.db.close()
    outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--speed", action="store_true", help="also run the speed review")
    parser.add_argument("--out", type=Path, help="write every outcome here as JSON")
    parser.add_argument("--sec-gap", type=float, default=0.25, help="seconds between SEC requests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    fetch._HOST_GAPS["sec.gov"] = args.sec_gap
    settings = Settings()
    fmp = settings.fmp_api_key.get_secret_value() if settings.fmp_api_key else None
    pending: asyncio.Queue[str] = asyncio.Queue()
    for ticker in args.tickers:
        pending.put_nowait(ticker.upper())
    outcomes: list[Outcome] = []
    async with EvalFetcher(settings.sec_user_agent) as fetcher:

        async def worker() -> None:
            while not pending.empty():
                ticker = pending.get_nowait()
                outcome = await evaluate(ticker, fetcher, fmp, args.speed, Path("schedules"))
                outcomes.append(outcome)
                found = outcome.date or outcome.error or "NO DATE"
                print(f"{ticker:6} {outcome.seconds:6.1f}s  {found}", flush=True)

        await asyncio.gather(*(worker() for _ in range(args.workers)))
    found = [o for o in outcomes if o.date]
    print(f"\nDates found: {len(found)}/{len(outcomes)}")
    for o in sorted(outcomes, key=lambda item: item.ticker):
        print(f"\n== {o.ticker} ({o.form}, wire {o.wire}) {o.seconds}s")
        print(f"   date: {o.date or '-'}  {o.evidence or o.phase or o.error}")
        if o.speed_day or o.speed:
            print(f"   speed {o.speed_day}: {', '.join(o.speed) or '-'}")
        for line in o.watch:
            print(f"   + {line}")
        for line in o.rejected:
            print(f"   - {line}")
    if args.out:
        args.out.write_text(json.dumps([o.__dict__ for o in outcomes], indent=1), "utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
