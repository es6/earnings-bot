import copy

from first_print.recorder import Target


def test_old_schedule_normalizes_without_losing_sources_or_mutating_input() -> None:
    data = {
        "ticker": "EXAMPLE",
        "cik": 1,
        "expected": "2026-11-17T16:20:00-05:00",
        "trial": {
            "post": True,
            "ir": {"listing": "https://ir.example.com/news", "headline": "Q3 results"},
            "globenewswire": ["Example"],
        },
    }
    original = copy.deepcopy(data)
    target = Target.model_validate(data)
    assert data == original
    assert target.ir and target.ir.listing == "https://ir.example.com/news"
    assert target.trial.globenewswire == ["Example"]
    normalized = target.model_dump()
    assert "post" not in normalized["trial"] and "ir" not in normalized["trial"]
    assert Target.model_validate(normalized).model_dump() == normalized


def test_explicit_current_ir_wins_over_an_old_trial_ir() -> None:
    target = Target.model_validate(
        {
            "ticker": "EXAMPLE",
            "cik": 1,
            "expected": "2026-11-17T16:20:00-05:00",
            "ir": {"listing": "https://ir.example.com/current", "headline": "current"},
            "trial": {
                "ir": {"listing": "https://ir.example.com/old", "headline": "old"},
                "globenewswire": ["Example"],
            },
        }
    )
    assert target.ir and target.ir.headline == "current"
    assert target.ir.listing == "https://ir.example.com/current"
    assert target.trial.globenewswire == ["Example"]
