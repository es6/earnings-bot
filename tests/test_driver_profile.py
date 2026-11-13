import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from earnings_bot.delivery import DeliveryStore
from earnings_bot.driver_profile import (
    parse_profile,
    profile_request,
    request_reservation_micros,
)
from earnings_bot.driver_profile_store import DriverProfileStore

TODAY = date(2026, 11, 13)
SOURCE = (
    "Acme sells subscriptions to its software. Subscription revenue increased 20%. "
    "The company expects full-year subscription revenue growth of 18% to 20%."
)
KPI = {"label": "Subscription revenue", "evidence": "Subscription revenue increased 20%."}


def content(**updates: object) -> str:
    return json.dumps(
        {
            "business": {
                "label": "software",
                "evidence": "Acme sells subscriptions to its software.",
            },
            "segments": [],
            "kpis": [KPI],
            "guidance": [],
            "themes": [],
        }
        | updates
    )


def parsed(answer: str | None = None, published: date = TODAY):
    return parse_profile(
        answer or content(),
        SOURCE,
        source_url="https://issuer.example/results",
        document_sha256="a" * 64,
        source_period="FY2026Q3",
        source_published=published,
        today=TODAY,
    )


def test_profile_keeps_evidence_for_every_category_and_dates_from_code() -> None:
    profile = parsed(
        content(
            business={"label": "advertising", "evidence": "Acme earns money from advertising."},
            guidance=[
                {"label": "subscription revenue", "evidence": SOURCE.split(". ")[-1]},
                {"label": "2027", "evidence": "The company guides for 2027."},
            ],
        )
    )
    assert profile.business is None
    assert len(profile.guidance) == 1 and profile.kpis[0].label == "Subscription revenue"
    assert profile.reviewed == TODAY and profile.source_period == "FY2026Q3"
    context = profile.context()["advisory_prior_release_profile"]
    assert context["kpis"][0]["evidence"] == KPI["evidence"]
    assert context["document_sha256"] == "a" * 64
    assert "New material developments outrank" in context["use"]


@pytest.mark.parametrize(
    "kpi",
    [
        {"label": "Operating income", "evidence": KPI["evidence"]},
        {"label": "Subscription revenue", "evidence": "Subscription revenue increased 90%."},
    ],
)
def test_an_existing_quote_does_not_support_an_invented_driver_label(kpi: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="No evidenced"):
        parsed(content(kpis=[kpi]))


def test_a_new_review_date_cannot_freshen_an_old_source() -> None:
    with pytest.raises(ValueError, match="stale"):
        parsed(published=TODAY - timedelta(days=121))
    with pytest.raises(ValueError, match="future"):
        parsed(published=TODAY + timedelta(days=1))
    assert parsed().stale(TODAY - timedelta(days=1))
    assert parsed().stale(TODAY + timedelta(days=121))


def test_profile_request_uses_only_ember_low_and_rejects_oversized_input() -> None:
    request = profile_request("ACME", SOURCE)
    assert request["model"] == "muse-ember-1.0"
    assert request["output_config"]["effort"] == "low" and request["max_tokens"] == 4000
    assert "reviewed" not in request["output_config"]["format"]["schema"]["properties"]
    # The reservation includes worst-case input bytes and higher-tier output prices.
    assert request_reservation_micros(request) > 4000 * 1.5
    with pytest.raises(ValueError, match="no truncation"):
        profile_request("ACME", "x" * 200_001)


def test_a_sent_profile_call_is_not_repeated_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        profiles = DriverProfileStore(store)
        assert profiles.claim(1, "a" * 64, TODAY, 30_000)
    with DeliveryStore(db, 7) as store:
        profiles = DriverProfileStore(store)
        assert not profiles.claim(1, "a" * 64, TODAY + timedelta(days=1), 30_000)
        assert profiles.get(1, TODAY) is None  # an uncertain outcome is not a cached profile
        profiles.finish(1, "a" * 64, 2000, parsed())
    with DeliveryStore(db, 99) as store:
        profiles = DriverProfileStore(store)
        assert profiles.get(1, TODAY) == parsed()  # public issuer evidence survives a channel move
        assert profiles.get(1, TODAY + timedelta(days=121)) is None
        with pytest.raises(ValueError, match="outstanding"):
            profiles.finish(1, "a" * 64, 2000, parsed())


def test_profile_reservations_bound_both_money_and_call_count(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        profiles = DriverProfileStore(store)
        assert profiles.claim(1, "a", TODAY, 250_000)
        assert profiles.claim(2, "b", TODAY, 250_000)
        assert not profiles.claim(3, "c", TODAY, 1)
        next_day = TODAY + timedelta(days=1)
        for cik in range(5):
            assert profiles.claim(cik, "new", next_day, 1000)
        assert not profiles.claim(99, "new", next_day, 1)
        profiles.finish(0, "new", 0, None)  # rejection still consumes a call
        assert not profiles.claim(99, "new", next_day, 1)
        with pytest.raises(ValueError, match="positive"):
            profiles.claim(99, "new", next_day, 0)
