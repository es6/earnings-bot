from dataclasses import replace
from decimal import Decimal

import pytest

from earnings_bot.summary import EFFORT, MODEL, SYSTEM
from earnings_bot.summary_policy import (
    DEFAULT_COMPARISON,
    SONNET,
    MetricKey,
    Observation,
    SummaryFact,
    cash_priority,
    evidence_issues,
    yoy,
)

# Fictional/unseen metrics and businesses: these cases must not need ticker switches.
KEY = MetricKey("Orbital freight revenue", "reported", "money", "USD")
NOW = Observation(KEY, Decimal(120), 2026, 3)
THEN = Observation(KEY, Decimal(100), 2025, 3)


def test_default_writer_and_comparison_follow_the_selected_models() -> None:
    assert [s.name for s in DEFAULT_COMPARISON] == ["Sonnet low", "Muse Ember low"]
    assert (SONNET.model, "low") == (MODEL, EFFORT)
    assert "EPS is optional" in SYSTEM


def test_yoy_only_uses_the_same_fiscal_period_and_metric_identity() -> None:
    assert yoy(NOW, THEN) == Decimal(20)
    assert yoy(NOW, replace(THEN, year=2024)) is None
    assert yoy(NOW, replace(THEN, quarter=2)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Orbital freight EBITDA"),
        ("basis", "adjusted"),
        ("unit", "thousands"),
        ("currency", "EUR"),
        ("scope", "year to date"),
        ("group", "new vessels"),
    ],
)
def test_close_values_do_not_override_incompatible_context(field: str, value: str) -> None:
    prior = replace(THEN, key=replace(KEY, **{field: value}))
    assert yoy(NOW, prior) is None


@pytest.mark.parametrize("value", ["0", "-10", "NaN", "Infinity"])
def test_unsafe_growth_is_left_missing(value: str) -> None:
    assert yoy(NOW, replace(THEN, value=Decimal(value))) is None


def test_unknown_currency_and_basis_cannot_support_computed_growth() -> None:
    for key in (replace(KEY, currency=""), replace(KEY, basis=""), replace(KEY, basis="unknown")):
        assert yoy(replace(NOW, key=key), replace(THEN, key=key)) is None


def test_annuals_compare_without_a_quarter_but_unlabeled_quarters_do_not() -> None:
    assert yoy(replace(NOW, quarter=None), replace(THEN, quarter=None)) is None
    annual = replace(KEY, scope="annual")
    assert (
        yoy(replace(NOW, key=annual, quarter=None), replace(THEN, key=annual, quarter=None)) == 20
    )
    assert yoy(replace(NOW, key=annual), replace(THEN, key=annual)) is None


def test_margin_change_is_bps_and_does_not_match_profit_amount() -> None:
    key = MetricKey("Operating margin", "adjusted", "%")
    assert (
        yoy(Observation(key, Decimal("26.4"), 2026, 3), Observation(key, Decimal("24.9"), 2025, 3))
        == 150
    )


def test_cash_priority_handles_burners_without_capex_and_unknown_data() -> None:
    assert cash_priority(None, None, Decimal(-1)) == "high"
    assert cash_priority(Decimal(100), Decimal(-15), None) == "high"
    assert cash_priority(Decimal(100), Decimal("14.99"), None) == "normal"
    assert cash_priority(Decimal(100), None, Decimal(1)) == "unknown"
    assert cash_priority(Decimal(0), Decimal(1), None) == "unknown"


def test_equal_sized_businesses_and_accounting_bases_remain_distinct() -> None:
    first = SummaryFact("Shuttle revenue", "$100M", "", kind="segment")
    second = replace(first, metric="Refueling revenue")
    assert first.identity != second.identity
    assert first.identity != replace(first, basis="adjusted").identity
    assert first.identity != replace(first, group="product split").identity


def test_source_check_rejects_empty_or_partially_matching_quotes() -> None:
    document = "Revenue grew 20%. " + "a" * 120 + " end of source."
    fact = SummaryFact("Revenue", "$120M", "")
    assert evidence_issues(fact, document) == ["missing_source_passage"]
    assert evidence_issues(replace(fact, source="a" * 120 + " invented"), document)
    assert evidence_issues(replace(fact, source="REVENUE  grew 20%."), document) == []


def test_guidance_never_infers_fiscal_period_from_a_year_or_dollar_value() -> None:
    fact = SummaryFact("Revenue outlook", "$2027M", "We expect revenue of $2027M.", "guide")
    assert evidence_issues(fact, fact.source) == ["missing_guidance_period"]
    assert evidence_issues(replace(fact, metric="Q4 revenue outlook"), fact.source) == []
    assert evidence_issues(replace(fact, period="FY2027"), fact.source) == []


def test_an_update_requires_status_or_detail_even_when_its_title_is_long() -> None:
    fact = SummaryFact("New orbital travel agent for private clients", "", "Announced", "update")
    assert evidence_issues(fact, "Announced") == ["bare_update"]
    assert not evidence_issues(replace(fact, value="Launched for 12 customers"), "Announced")
