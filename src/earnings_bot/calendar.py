"""Persistent issuer subscriptions and the next planned release."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from earnings_bot.delivery import DeliveryStore
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET, Target

DAILY_ADDS = 5
MAX_TRACKED = 50


@dataclass(frozen=True)
class Tracked:
    ticker: str
    cik: int
    target: Target | None
    evidence: str | None


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

    def entries(self) -> list[Tracked]:
        return [
            Tracked(
                row["ticker"],
                row["cik"],
                Target.model_validate_json(row["target"]) if row["target"] else None,
                row["evidence"],
            )
            for row in self.db.execute(
                "SELECT * FROM tracked WHERE channel = ? ORDER BY ticker", (self.channel,)
            )
        ]

    def contains(self, cik: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM tracked WHERE channel = ? AND cik = ?", (self.channel, cik)
            ).fetchone()
            is not None
        )

    def add(self, ticker: str, user_id: int, now: datetime) -> tuple[Tracked, bool]:
        cik = company_cik(ticker)
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
        with self.db:
            self.db.execute(
                "INSERT INTO tracked (channel, cik, ticker) VALUES (?, ?, ?)",
                (self.channel, cik, ticker),
            )
            self.db.execute(
                "INSERT INTO tracking_adds VALUES (?, ?, ?)", (self.channel, user_id, day)
            )
        return Tracked(ticker, cik, None, None), True

    def set_target(self, target: Target, evidence: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE tracked SET target = ?, evidence = ? WHERE channel = ? AND cik = ?",
                (target.model_dump_json(), evidence, self.channel, target.cik),
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
            existing = next((t for t in files.values() if same_release(t, target)), None)
            if existing is None:
                result[target.release_key] = target
            else:
                updates = {}
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


def same_release(first: Target, second: Target) -> bool:
    if first.cik != second.cik:
        return False
    if first.period and second.period:
        return first.period == second.period
    return first.expected.astimezone(ET).date() == second.expected.astimezone(ET).date()


def company_cik(ticker: str) -> int:
    company = known_companies().get(ticker)
    if company is None:
        raise ValueError(f"{ticker or 'That symbol'} is outside the current ticker catalog.")
    return company.cik


def manual_target(ticker: str, when: str, period: str, now: datetime) -> Target:
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
    return to_target(ticker, expected.astimezone(ET), fy, quarter)


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
