"""/track debug mode: everything /track does for a company, from scratch, changing nothing live.

The owner runs it over and over with Claude, fixing what /track gets wrong, until a fresh run
gets every fact right on the first try (docs/track-testing.md). A run identifies the company,
investigates its sources, finds its next release date, and audits its release history and
speed, against a throwaway calendar. It opens no window, posts no scorecard and makes no paid
model call. Its report lists every fact as found, unsure or missing, with how long each step
took, and is saved as JSON and Markdown for Claude to read. When a company has an answer key
(tests/track_cases/<SYMBOL>.toml), the run also lists every fact that differs from it.

    uv run python -m earnings_bot.track_test NVDA LCID --out schedules/track-tests
"""

import argparse
import asyncio
import io
import json
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery, covering_release_times
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher
from earnings_bot.issuers import Issuers, usual_release_time
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.recorder import ET, Target
from earnings_bot.sec_results import SixKResults
from earnings_bot.source_audit import audit_sources

FOLDER = "track-tests"  # under the schedule directory
KEEP = 20  # saved reports per symbol
# Answer keys checked by a person, one per company; absent from the Docker image
CASES = Path(__file__).resolve().parents[2] / "tests" / "track_cases"
FOUND, UNSURE, MISSING = "✅", "⚠️", "❌"


@dataclass
class Step:
    name: str
    seconds: float
    outcome: str


@dataclass
class TrackReport:
    symbol: str
    started: str  # ISO time, UTC
    seconds: float = 0.0
    steps: list[Step] = field(default_factory=list)
    # Each fact as the run found it; None when it found none
    facts: dict[str, Any] = field(default_factory=dict)
    # What research said while it worked, for the facts it didn't find
    notes: list[str] = field(default_factory=list)
    error: str = ""
    # Every fact that differs from the answer key, when there is one
    mismatches: list[str] | None = None

    def checklist(self) -> list[tuple[str, str, str]]:
        """(mark, fact, value) for each fact: found, unsure (found without confirmation), or
        missing."""
        rows = []
        for name, label in LABELS.items():
            value = self.facts.get(name)
            if value in (None, "", [], "unknown"):
                rows.append((MISSING, label, "not found"))
            else:
                unsure = name in self.facts.get("unsure", [])
                rows.append((UNSURE if unsure else FOUND, label, _show(value)))
        return rows

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, default=str)

    def to_markdown(self) -> str:
        lines = [
            f"# /track debug · {self.symbol}",
            "",
            f"Run {self.started}, {self.seconds:.0f} s.",
            "",
        ]
        if self.error:
            lines += [f"**Stopped:** {self.error}", ""]
        lines += ["| | Fact | Value |", "|---|---|---|"]
        lines += [f"| {mark} | {label} | {value} |" for mark, label, value in self.checklist()]
        lines += ["", "| Step | Seconds | Outcome |", "|---|---|---|"]
        lines += [f"| {s.name} | {s.seconds:.1f} | {s.outcome} |" for s in self.steps]
        if self.mismatches is not None:
            lines += ["", "## Answer key", ""]
            lines += [f"- {m}" for m in self.mismatches] or ["- Every fact matches: one-shot."]
        if self.notes:
            lines += ["", "## What research said", ""] + [f"- {n}" for n in self.notes]
        lines += [
            "",
            "## Every fact",
            "",
            "```json",
            json.dumps(self.facts, indent=1, default=str),
            "```",
        ]
        return "\n".join(lines) + "\n"


# The facts a run reports, in order, and how the report names them
LABELS = {
    "name": "Company",
    "cik": "SEC ID (CIK)",
    "sec_form": "Results filing",
    "fiscal_year_end": "Fiscal year end",
    "wire": "Newswire",
    "ir_site": "IR site",
    "routes": "Routes watched",
    "next_release": "Next release",
    "period": "Period",
    "evidence": "Date evidence",
    "window": "Window",
    "usual_time": "Usual release time",
    "release_times": "Release times covered",
    "history": "Recent release times",
}


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value[:6]) + (" …" if len(value) > 6 else "")
    return str(value)


async def run_track_test(
    symbol: str,
    fetcher: Fetcher,
    directory: Path,
    now: Callable[[], datetime],
    fmp_api_key: str | None = None,
    *,
    sixk: SixKResults | None = None,
    busy: Callable[[], bool] = lambda: False,
    windows_open: Callable[[], bool] = lambda: False,
    chrome_live: Callable[[], bool] = lambda: False,
    limit: float | None = None,
) -> TrackReport:
    """Runs /track's research for `symbol` from scratch against a throwaway calendar, as for a
    company the bot has never seen: a company in the catalog (companies.toml) is looked up on
    SEC and researched like any other. In the bot, `busy`, `windows_open` and `chrome_live` are
    the live watcher's, so a test gives way to live windows as all research does. A run that
    takes over `limit` seconds stops and reports what it found by then."""
    report = TrackReport(symbol, datetime.now(UTC).isoformat(timespec="seconds"))
    started = time.monotonic()
    folder = Path(tempfile.mkdtemp(prefix="track-test-"))
    store = DeliveryStore(folder / "state.sqlite3", 1)
    calendar = Calendar(store, catalog=False)
    history: list[datetime] = []
    review: str | None = None
    budget = asyncio.timeout(limit)
    try:
        async with budget:
            await _run(
                symbol,
                fetcher,
                directory,
                now,
                fmp_api_key,
                report,
                calendar,
                history,
                sixk,
                busy,
                windows_open,
                chrome_live,
            )
    except Exception as error:  # the report shows every failure
        report.error = (
            f"Stopped at its {limit:.0f} s limit; the facts are what it found by then"
            if budget.expired()
            else f"{type(error).__name__}: {error}"
        )
    try:
        # What it found, even when it stopped early
        if row := calendar.find(symbol):
            review = calendar.review(row.cik)[1]
            target = covering_release_times(row.target, review) if row.target else None
            report.facts = _facts(row.company, target, row.evidence, history)
    finally:
        store.db.close()
        shutil.rmtree(folder, ignore_errors=True)
    report.seconds = round(time.monotonic() - started, 1)
    return report


async def _run(
    symbol: str,
    fetcher: Fetcher,
    directory: Path,
    now: Callable[[], datetime],
    fmp_api_key: str | None,
    report: TrackReport,
    calendar: Calendar,
    history: list[datetime],
    sixk: SixKResults | None,
    busy: Callable[[], bool],
    windows_open: Callable[[], bool],
    chrome_live: Callable[[], bool],
) -> None:
    """The steps, each timed in `report`, filling `calendar` and `history` as they go."""
    discovery = DateDiscovery(
        calendar,
        fetcher,
        now,
        busy,
        report.notes.append,
        fmp_api_key,
        sixk=sixk,
        windows_open=windows_open,
        chrome_live=chrome_live,
    )
    today = now().astimezone(ET).date()

    step = time.monotonic()
    company = await Issuers(fetcher).company(symbol, today, catalog=False)
    report.steps.append(Step("Identify", time.monotonic() - step, company.name))
    if symbol in known_companies():
        report.notes.append("In the catalog (companies.toml); researched as if it weren't")
    row, _ = calendar.add(symbol, company)

    step = time.monotonic()
    await discovery.run_once(row)
    row = calendar.find(symbol) or row
    found = f"{row.target.expected.astimezone(ET):%a %b %d %H:%M} ET" if row.target else "no date"
    report.steps.append(Step("Research", time.monotonic() - step, found))
    if not row.target and (phase := discovery.phases.get(row.cik)):
        report.notes.append(phase)

    step = time.monotonic()
    # Reads the bot's own journals for past measurements, as /track does; writes nothing
    speed = await audit_sources(fetcher, directory, symbol, row.company, now(), None, sixk)
    calendar.save_review(row.cik, now(), speed.to_json())
    history.extend(speed.history or [])
    report.steps.append(Step("Speed audit", time.monotonic() - step, f"{len(history)} releases"))


def _facts(
    company: KnownCompany, target: Target | None, evidence: str | None, history: list[datetime]
) -> dict[str, Any]:
    ir_site = (
        company.ir_listing
        or company.ir_home
        or (f"https://{company.q4_host}" if company.q4_host else None)
        or (company.rss[0] if company.rss else None)
    )
    routes = [check.source for check in company.checks if check.watch]
    facts: dict[str, Any] = {
        "name": company.name,
        "cik": company.cik,
        "sec_form": company.sec_form,
        "fiscal_year_end": company.fiscal_year_end,
        "wire": company.wire,
        "ir_site": ir_site,
        "routes": routes,
        # Every source the investigation checked, and why it was watched or turned down
        "checks": [
            f"{'watched' if check.watch else 'not watched'}: {check.source} ({check.why})"
            for check in company.checks
        ],
        "history": [f"{at.astimezone(ET):%Y-%m-%d %H:%M}" for at in history[:8]],
        "usual_time": usual_release_time(history),
    }
    if target is not None:
        start, end = target.window
        facts.update(
            next_release=f"{target.expected.astimezone(ET):%Y-%m-%d %H:%M} ET",
            period=target.period,
            evidence=evidence,
            window=f"{start.astimezone(ET):%a %b %d %H:%M} to {end.astimezone(ET):%H:%M} ET",
            # Every time the window covers, the expected one too: none other is an answer
            release_times=[
                f"{t.astimezone(ET):%H:%M}"
                for t in sorted({target.expected, *target.release_times})
            ],
        )
    # A date only an outside calendar estimated isn't confirmed by the company
    if evidence and "calendar" in evidence.lower():
        facts["unsure"] = ["next_release", "period"]
    return facts


def load_key(symbol: str, folder: Path = CASES) -> dict[str, Any] | None:
    path = folder / f"{symbol.replace(':', '_')}.toml"
    if not path.is_file():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8"))


def compare(report: TrackReport, key: dict[str, Any]) -> list[str]:
    """Every fact that differs from a person's answer key. Text matches ignoring case; the IR
    site and evidence match when the key's text appears in what was found; a release matches
    on its day, and on its time when the key gives one."""
    found = report.facts
    differences = []
    for name, expected in key.items():
        if name == "symbol":
            continue
        actual = found.get(name)
        if name == "next_release":
            day = str(expected.get("day", "")) if isinstance(expected, dict) else str(expected)
            hhmm = expected.get("time") if isinstance(expected, dict) else None
            ok = (
                bool(actual)
                and str(actual).startswith(day)
                and (not hhmm or f" {hhmm} " in f"{actual} ")
            )
        elif name in ("ir_site", "evidence"):
            ok = bool(actual) and str(expected).lower() in str(actual).lower()
        elif isinstance(expected, list):
            ok = isinstance(actual, list) and all(item in actual for item in expected)
        elif isinstance(expected, str):
            ok = isinstance(actual, str) and expected.lower() == actual.lower()
        else:
            ok = expected == actual
        if not ok:
            differences.append(f"{LABELS.get(name, name)}: expected {expected!r}, found {actual!r}")
    return differences


def save(report: TrackReport, directory: Path) -> Path:
    """Saves the report as JSON and Markdown and keeps the last KEEP per symbol. Returns the
    Markdown file."""
    folder = directory / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{report.symbol.replace(':', '_')}-{report.started.replace(':', '').replace('+0000', 'Z')}"
    )
    (folder / f"{stem}.json").write_text(report.to_json(), encoding="utf-8")
    markdown = folder / f"{stem}.md"
    markdown.write_text(report.to_markdown(), encoding="utf-8")
    prefix = f"{report.symbol.replace(':', '_')}-"
    older = sorted(folder.glob(f"{prefix}*.md"))[:-KEEP]
    for path in older:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
    return markdown


async def _main() -> None:
    from earnings_bot import fetch
    from earnings_bot.config import Settings

    if isinstance(sys.stdout, io.TextIOWrapper):  # a Windows console or file is cp1252
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--out", type=Path, default=Path("schedules"), help="schedule directory")
    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    agent = settings.sec_user_agent or ""
    fmp = settings.fmp_api_key.get_secret_value() if settings.fmp_api_key else None
    passed = 0
    async with fetch.Fetcher(agent) as fetcher:
        for symbol in args.symbols:
            report = await run_track_test(
                symbol.upper(), fetcher, args.out, lambda: datetime.now(ET), fmp
            )
            if (key := load_key(report.symbol)) is not None:
                report.mismatches = compare(report, key)
                passed += not report.mismatches
            path = save(report, args.out)
            print(f"{report.symbol}: {report.seconds:.0f} s, report {path}")
            for mark, label, value in report.checklist():
                print(f"  {mark} {label}: {value}")
            for mismatch in report.mismatches or []:
                print(f"  ≠ {mismatch}")
    keyed = sum(1 for s in args.symbols if load_key(s.upper()) is not None)
    if keyed:
        print(f"One-shot: {passed} of {keyed} with answer keys")


if __name__ == "__main__":
    asyncio.run(_main())
