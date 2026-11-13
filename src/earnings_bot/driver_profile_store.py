"""Profiles and sent-before-call reservations in the bot's existing SQLite database."""

from datetime import date

from pydantic import ValidationError

from earnings_bot.delivery import DeliveryStore
from earnings_bot.driver_profile import DriverProfile

DAILY_PROFILE_CALLS = 5
DAILY_PROFILE_MICROS = 500_000  # $0.50, independent of the alert summary allowance


class DriverProfileStore:
    def __init__(self, store: DeliveryStore) -> None:
        self.db = store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS issuer_driver_profiles (
                cik INTEGER PRIMARY KEY,
                profile TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS driver_profile_calls (
                cik INTEGER NOT NULL,
                document TEXT NOT NULL,
                day TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_micros INTEGER NOT NULL,
                PRIMARY KEY (cik, document)
            );
        """)

    def get(self, cik: int, today: date) -> DriverProfile | None:
        row = self.db.execute(
            "SELECT profile FROM issuer_driver_profiles WHERE cik = ?", (cik,)
        ).fetchone()
        if row is None:
            return None
        try:
            profile = DriverProfile.model_validate_json(row[0])
        except ValidationError:
            return None
        return None if profile.stale(today) else profile

    def claim(self, cik: int, document: str, today: date, reservation: int) -> bool:
        if reservation <= 0:
            raise ValueError("A profile request needs a positive cost reservation")
        # A single INSERT with its admission predicate is atomic, including when separate
        # connections race. Unknown outcomes retain their full reservation and cannot retry.
        with self.db:
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO driver_profile_calls
                SELECT ?, ?, ?, 'sent', ?
                WHERE (SELECT COUNT(*) FROM driver_profile_calls WHERE day = ?) < ?
                  AND (SELECT COALESCE(SUM(reserved_micros), 0) FROM driver_profile_calls
                       WHERE day = ?) + ? <= ?
                """,
                (
                    cik,
                    document,
                    today.isoformat(),
                    reservation,
                    today.isoformat(),
                    DAILY_PROFILE_CALLS,
                    today.isoformat(),
                    reservation,
                    DAILY_PROFILE_MICROS,
                ),
            )
        return cursor.rowcount == 1

    def finish(
        self,
        cik: int,
        document: str,
        cost_micros: int,
        profile: DriverProfile | None,
    ) -> None:
        if cost_micros < 0 or (profile and profile.document_sha256 != document):
            raise ValueError("Profile result does not match its reservation")
        with self.db:
            cursor = self.db.execute(
                "UPDATE driver_profile_calls SET status = ?, reserved_micros = ? "
                "WHERE cik = ? AND document = ? AND status = 'sent'",
                ("complete" if profile else "rejected", cost_micros, cik, document),
            )
            if cursor.rowcount != 1:
                raise ValueError("No outstanding profile call for this result")
            if profile:
                self.db.execute(
                    "INSERT OR REPLACE INTO issuer_driver_profiles VALUES (?, ?)",
                    (cik, profile.model_dump_json()),
                )
