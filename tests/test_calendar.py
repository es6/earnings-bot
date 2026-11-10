from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from earnings_bot.browser_sources import IRSource
from earnings_bot.calendar import Calendar, manual_target, upcoming
from earnings_bot.delivery import DeliveryStore
from earnings_bot.ir_research import IR_LISTINGS, quarter_headline
from earnings_bot.issuers import profile
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET, Trial
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def test_subscription_and_override_survive_restart_and_preserve_file_trials(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        row, created = calendar.add("NVDA")
        assert created and row.target is None
        assert calendar.add("NVDA") == (row, False)
        target = manual_target("NVDA", "2026-11-12T16:20", "FY2027Q3", NOW)
        calendar.set_target(target, "Manual override")

    file = to_target("NVDA", datetime(2026, 11, 13, 16, 20, tzinfo=ET), 2027, 3)
    file.trial = Trial(globenewswire=["NVIDIA"])
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        targets = calendar.plan({file.release_key: file})
        [effective] = targets.values()
        assert effective.expected == target.expected
        assert effective.trial == file.trial
        assert "Manual override" in upcoming(calendar.entries(), targets, NOW)[0]
        assert calendar.remove(row.cik)
        assert calendar.plan({file.release_key: file}) == {file.release_key: file}


def test_tracking_and_untracking_can_be_repeated(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        for _ in range(6):
            row, added = calendar.add("NVDA")
            assert added
            assert calendar.remove(row.cik)


def test_override_requires_a_valid_fiscal_period_and_future_time() -> None:
    with pytest.raises(ValueError, match=r"fiscal|period"):
        manual_target("NVDA", "2026-11-17T16:20", "FY2027Q0", NOW)
    with pytest.raises(ValueError, match="future"):
        manual_target("NVDA", "2026-11-01T16:20", "FY2027Q3", NOW)


def test_auto_browser_source_preserves_existing_schedule_choices(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA")
        target = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3)
        target.ir = IRSource(
            listing="https://investor.nvidia.com/news/", headline="Q3", verify_text=True
        )
        calendar.set_target(target, "Issuer notice")
        file = target.model_copy(update={"ir": None, "trial": Trial(globenewswire=["NVIDIA"])})
        [effective] = calendar.plan({file.release_key: file}).values()
        assert effective.ir == target.ir
        assert effective.trial.globenewswire == ["NVIDIA"]
        assert not effective.trial.post
        file.trial.post = True
        [effective] = calendar.plan({file.release_key: file}).values()
        assert effective.ir == target.ir
        assert effective.trial.post


def test_saved_automatic_browser_headline_is_repaired_after_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    target = to_target("CRWD", datetime(2026, 12, 1, 16, 5, tzinfo=ET), 2027, 3)
    headline = quarter_headline(2027, 3)
    target.ir = IRSource(
        listing=IR_LISTINGS["CRWD"], headline=headline.removesuffix(".*"), verify_text=True
    )
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        calendar.add("CRWD")
        calendar.set_target(target, "Issuer notice")
    with DeliveryStore(db, 7) as store:
        [effective] = Calendar(store).plan({}).values()
        assert effective.ir and effective.ir.headline == headline


def test_a_calendar_saved_before_profiles_is_upgraded_in_place(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.db.executescript("""
            CREATE TABLE tracked (
                channel INTEGER NOT NULL,
                cik INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                target TEXT,
                evidence TEXT,
                PRIMARY KEY (channel, cik)
            );
            INSERT INTO tracked (channel, cik, ticker) VALUES (7, 1045810, 'NVDA');
        """)

        calendar = Calendar(store)

        [nvda] = calendar.entries()
        assert nvda.company == known_companies()["NVDA"]
        lucid = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, date(2026, 11, 9))
        calendar.add("LCID", lucid)
        assert [row.company for row in Calendar(store).entries()] == [lucid, nvda.company]


def test_a_company_the_catalog_dropped_is_still_watched_on_edgar(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.db.execute(
            "CREATE TABLE tracked (channel INTEGER NOT NULL, cik INTEGER NOT NULL, "
            "ticker TEXT NOT NULL, target TEXT, evidence TEXT, profile TEXT, "
            "PRIMARY KEY (channel, cik))"
        )
        store.db.execute("INSERT INTO tracked (channel, cik, ticker) VALUES (7, 99, 'GONE')")

        [row] = Calendar(store).entries()

    target = to_target("GONE", datetime(2027, 1, 5, 16, tzinfo=ET), 2026, 4, row.company)
    assert [source.name for source in target.sources()] == ["edgar", "edgar_latest"]
    assert target.cik == 99
