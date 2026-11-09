"""Persistent issuer subscriptions and the next planned release."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from earnings_bot.delivery import DeliveryStore
from earnings_bot.ir_research import IR_LISTINGS, quarter_headline
from earnings_bot.known import KnownCompany, known_companies, to_target
from earnings_bot.recorder import ET, Target

DAILY_ADDS = 5
MAX_TRACKED = 50


@dataclass(frozen=True)
class Tracked:
    ticker: str
    cik: int
    target: Target | None
    evidence: str | None
    company: KnownCompany


class Calendar:
    def __init__(self, store: DeliveryStore) -> None:
        self.db = store.db
        self.channel = store.channel_id
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tracked (
                channel INTEGER NOT NULL,
                cik INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                target TEXT,
                evidence TEXT,
                profile TEXT,
                PRIMARY KEY (channel, cik)
            );
            CREATE TABLE IF NOT EXISTS tracking_reviews (
                channel INTEGER NOT NULL,
                cik INTEGER NOT NULL,
                checked TEXT NOT NULL,
                result TEXT NOT NULL,
                PRIMARY KEY (channel, cik)
            );
            CREATE TABLE IF NOT EXISTS tracking_adds (
                channel INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tracked)")}
        if "profile" not in columns:  # a calendar saved before /track took any ticker
            with self.db:
                self.db.execute("ALTER TABLE tracked ADD COLUMN profile TEXT")

    def entries(self) -> list[Tracked]:
        return [
            Tracked(
                row["ticker"],
                row["cik"],
                _read_target(row["target"]),
                row["evidence"],
                _company(row["ticker"], row["cik"], row["profile"]),
            )
            for row in self.db.execute(
                "SELECT * FROM tracked WHERE channel = ? ORDER BY ticker", (self.channel,)
            )
        ]

    def find(self, ticker: str) -> Tracked | None:
        return next((row for row in self.entries() if row.ticker == ticker), None)

    def contains(self, cik: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM tracked WHERE channel = ? AND cik = ?", (self.channel, cik)
            ).fetchone()
            is not None
        )

    def add(
        self, ticker: str, user_id: int, now: datetime, company: KnownCompany | None = None
    ) -> tuple[Tracked, bool]:
        """Tracks a catalog company, or any other with its company facts given."""
        company = company or known_companies()[ticker]
        cik = company.cik
        entries = self.entries()
        if existing := next((row for row in entries if row.cik == cik), None):
            return existing, False
        if len(entries) >= MAX_TRACKED:
            raise ValueError(f"This calendar already tracks {MAX_TRACKED} companies.")
        day = now.astimezone(ET).date().isoformat()
        count = self.db.execute(
            "SELECT count(*) FROM tracking_adds WHERE channel = ? AND user_id = ? AND day = ?",
            (self.channel, user_id, day),
        ).fetchone()[0]
        if count >= DAILY_ADDS:
            raise ValueError(f"You can add up to {DAILY_ADDS} tickers per Eastern calendar day.")
        # The catalog's entry is read fresh each time, so only other companies' facts are saved
        profile = None if ticker in known_companies() else company.model_dump_json()
        with self.db:
            self.db.execute(
                "INSERT INTO tracked (channel, cik, ticker, profile) VALUES (?, ?, ?, ?)",
                (self.channel, cik, ticker, profile),
            )
            self.db.execute(
                "INSERT INTO tracking_adds VALUES (?, ?, ?)", (self.channel, user_id, day)
            )
        return Tracked(ticker, cik, None, None, company), True

    def set_target(self, target: Target, evidence: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE tracked SET target = ?, evidence = ? WHERE channel = ? AND cik = ?",
                (target.model_dump_json(), evidence, self.channel, target.cik),
            )

    def save_profile(self, cik: int, company: KnownCompany) -> None:
        """Keeps what the bot found out about a company outside the catalog."""
        with self.db:
            self.db.execute(
                "UPDATE tracked SET profile = ? WHERE channel = ? AND cik = ?",
                (company.model_dump_json(), self.channel, cik),
            )

    def review(self, cik: int) -> tuple[datetime | None, str | None]:
        row = self.db.execute(
            "SELECT checked, result FROM tracking_reviews WHERE channel = ? AND cik = ?",
            (self.channel, cik),
        ).fetchone()
        return (datetime.fromisoformat(row["checked"]), row["result"]) if row else (None, None)

    def save_review(self, cik: int, checked: datetime, result: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO tracking_reviews VALUES (?, ?, ?, ?)",
                (self.channel, cik, checked.isoformat(), result),
            )

    def clear_review(self, cik: int) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM tracking_reviews WHERE channel = ? AND cik = ?", (self.channel, cik)
            )

    def remove(self, cik: int) -> bool:
        with self.db:
            self.db.execute(
                "DELETE FROM tracking_reviews WHERE channel = ? AND cik = ?", (self.channel, cik)
            )
            return (
                self.db.execute(
                    "DELETE FROM tracked WHERE channel = ? AND cik = ?", (self.channel, cik)
                ).rowcount
                > 0
            )

    def plan(self, files: dict[str, Target]) -> dict[str, Target]:
        """Use one window per release, keeping the source settings of existing schedule files."""
        result = files.copy()
        for row in self.entries():
            if (target := row.target) is None:
                continue
            if row.company.sec_form == "6-K" and target.sec_form != "6-K":
                target = target.model_copy(update={"sec_form": "6-K"})
            existing = next((t for t in files.values() if same_release(t, target)), None)
            if existing is None:
                result[target.release_key] = target
            else:
                updates = {}
                if row.company.sec_form == "6-K" and "sec_form" not in existing.model_fields_set:
                    updates["sec_form"] = "6-K"
                if row.evidence == "Manual override":
                    updates.update(
                        expected=target.expected,
                        before_minutes=target.before_minutes,
                        after_minutes=target.after_minutes,
                    )
                if target.trial.ir and not existing.trial.ir and not existing.trial.post:
                    updates["trial"] = existing.trial.model_copy(update={"ir": target.trial.ir})
                if updates:
                    result[existing.release_key] = existing.model_copy(update=updates)
        return result


def _read_target(saved: str | None) -> Target | None:
    if saved is None:
        return None
    target = Target.model_validate_json(saved)
    browser = target.trial.ir
    if (
        target.period
        and browser
        and browser.verify_text
        and browser.listing == IR_LISTINGS.get(target.ticker)
    ):
        headline = quarter_headline(int(target.period[2:6]), int(target.period[-1]))
        # Only repair the exact zero-width pattern stored by the original planner.
        if browser.headline == headline.removesuffix(".*"):
            browser.headline = headline
    return target


def same_release(first: Target, second: Target) -> bool:
    if first.cik != second.cik:
        return False
    if first.period and second.period:
        return first.period == second.period
    return first.expected.astimezone(ET).date() == second.expected.astimezone(ET).date()


def _company(ticker: str, cik: int, profile: str | None) -> KnownCompany:
    if known := known_companies().get(ticker):
        return known
    if profile:
        return KnownCompany.model_validate_json(profile)
    # Tracked from the catalog, which has since dropped it. EDGAR still identifies it by CIK.
    return KnownCompany(
        name=ticker,
        cik=cik,
        platform="unknown",
        reachable=False,
        wire="none",
        fastest="unknown",
        evidence="No longer in the catalog.",
        checked=date.min,
    )


def manual_target(
    ticker: str, when: str, period: str, now: datetime, company: KnownCompany | None = None
) -> Target:
    try:
        if "T" not in when and " " not in when:
            raise ValueError
        expected = datetime.fromisoformat(when)
        fy, quarter = int(period[2:6]), int(period[7:])
        if (
            period != f"FY{fy:04d}Q{quarter}"
            or quarter not in range(1, 5)
            or not 2000 <= fy <= 2099
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Use an ISO date and time like 2026-11-17T16:20 and a period like FY2027Q3."
        ) from None
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=ET)
    if expected <= now:
        raise ValueError("Choose a future earnings release time.")
    return to_target(ticker, expected.astimezone(ET), fy, quarter, company)


def describe(target: Target, evidence: str) -> str:
    stamp = target.expected.astimezone(ET)
    start = target.window[0].astimezone(ET)
    period = f" {target.period}" if target.period else ""
    return (
        f"**{target.ticker}{period}** · {stamp:%b %d, %H:%M} ET · {evidence}"
        f" · watches from {start:%H:%M} ET"
    )


def upcoming(tracked: list[Tracked], planned: dict[str, Target], now: datetime) -> list[str]:
    end = now + timedelta(days=7)
    rows = []
    for target in sorted(planned.values(), key=lambda item: item.expected):
        if now <= target.expected <= end:
            evidence = next(
                (
                    row.evidence
                    for row in tracked
                    if row.target
                    and same_release(row.target, target)
                    and row.target.expected == target.expected
                ),
                "Schedule file",
            )
            rows.append(describe(target, evidence or "Tracked"))
    planned_ciks = {target.cik for target in planned.values() if target.window[1] > now}
    if pending := [row.ticker for row in tracked if row.cik not in planned_ciks]:
        rows.append(f"Awaiting dates: {', '.join(pending)}")
    return rows
