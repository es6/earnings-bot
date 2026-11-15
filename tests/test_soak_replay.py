from tests.soak_replay import Baseline, failures


def test_soak_fails_on_delivery_pacing_and_post_warmup_growth() -> None:
    good = {
        "missed_alerts": [],
        "duplicate_alerts": [],
        "no_body": [],
        "sec": {"most_in_any_second": 8},
    }
    used = {"bot_private_mb": 100, "tasks": 2, "threads": 6, "handles": 200}
    baseline = Baseline(100, 2, 6, 200)
    assert failures(good, used, baseline) == []
    for field in ("missed_alerts", "duplicate_alerts", "no_body"):
        assert "delivery invariant" in failures(good | {field: ["EXAMPLE"]}, used, baseline)
    assert "SEC pacing" in failures(good | {"sec": {"most_in_any_second": 9}}, used, baseline)
    for changed in ({"bot_private_mb": 357}, {"tasks": 11}, {"threads": 15}, {"handles": 329}):
        assert failures(good, used | changed, baseline)
    assert (
        failures(good, {"tasks": 2, "threads": 6, "handles": None}, Baseline(None, 2, 6, None))
        == []
    )
