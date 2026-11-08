from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from earnings_bot.calendar import Calendar, manual_target, upcoming
from earnings_bot.delivery import DeliveryStore
from earnings_bot.known import to_target
from earnings_bot.recorder import ET, Trial

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def test_subscription_and_override_survive_restart_and_preserve_file_trials(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        row, created = calendar.add("NVDA", 1, NOW)
        assert created and row.target is None
        assert calendar.add("NVDA", 2, NOW) == (row, False)
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


def test_daily_limit_counts_adds_even_when_an_admin_removes_them(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        for _ in range(5):
            row, added = calendar.add("NVDA", 1, NOW)
            assert added
            assert calendar.remove(row.cik)
        with pytest.raises(ValueError, match="5 tickers"):
            calendar.add("NVDA", 1, NOW)
        assert calendar.add("NVDA", 1, NOW + timedelta(days=1))[1]


def test_override_requires_a_valid_fiscal_period_and_future_time() -> None:
    with pytest.raises(ValueError, match=r"fiscal|period"):
        manual_target("NVDA", "2026-11-17T16:20", "FY2027Q0", NOW)
    with pytest.raises(ValueError, match="future"):
        manual_target("NVDA", "2026-11-01T16:20", "FY2027Q3", NOW)
