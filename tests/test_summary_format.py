# En dashes are the intended rendered range separators.
# ruff: noqa: RUF001

import pytest

from earnings_bot.summary_evaluation import display_lines
from earnings_bot.summary_format import (
    format_fact,
    format_label,
    format_period,
    format_value,
    render_facts,
)
from earnings_bot.summary_policy import SummaryFact


@pytest.mark.parametrize(
    ("value", "unit", "currency", "expected"),
    [
        ("$2,963.0 to $2,964.0 million", "millions", "USD", "$2.963B–$2.964B"),
        ("$2,963 to $2,964 million", "", "", "$2.963B–$2.964B"),
        ("$2,365-$2,395 million", "USD millions", "USD", "$2.365B–$2.395B"),
        ("845 to 846", "USD millions", "USD", "$845M–$846M"),
        ("€5.01–€5.02 billion", "EUR billion", "EUR", "€5.01B–€5.02B"),
        ("$900M to $1.1B", "USD", "USD", "$0.9B–$1.1B"),
        ("-3% to -1%", "percent", "EUR", "-3%–-1%"),
        ("10750", "USD millions", "USD", "$10.75B"),
        ("4.44", "EUR per share", "EUR", "€4.44"),
        ("0", "USD million", "USD", "$0"),
        ("7000", "", "", "7000"),
        ("1,2", "USD millions", "USD", "1,2"),
        ("around 57", "percent", "", "around 57%"),
        ("~12.0", "billion", "USD", "~$12B"),
        ("993 million", "million orders", "USD", "993M"),
        ("19", "count", "USD", "19"),
        ("680", "customers", "USD", "680"),
        ("1.5", "million pages", "EUR", "1.5M"),
        ("$5M to €6M", "", "", "$5M to €6M"),
        ("€5M to €6M", "EUR million", "USD", "€5M to €6M"),
        ("164; 1.54", "EUR bn; ratio", "EUR", "164; 1.54"),
        ("Profit $5B; margin 15%", "USD", "USD", "Profit $5B; margin 15%"),
    ],
)
def test_units_and_ranges_keep_their_meaning(
    value: str, unit: str, currency: str, expected: str
) -> None:
    assert format_value(value, unit, currency) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full year fiscal 2026", "FY2026"),
        ("FYFY2026", "FY2026"),
        ("Q4 fiscal 2026 ended September 30, 2026", "Q4 FY2026"),
        ("Q3 2026", "Q3 2026"),
        ("Q4 2026 and FY 2026", "Q4 2026 and FY 2026"),
        ("FY2030", "FY2030"),
        ("Quarter ended September 30, 2026", "Quarter ended September 30, 2026"),
    ],
)
def test_fiscal_periods_are_explicit_without_duplicate_prefixes(value: str, expected: str) -> None:
    assert format_period(value) == expected


def test_duplicate_acronyms_are_removed_without_dropping_the_measure() -> None:
    assert format_label("Fee Related Earnings (FRE)") == "FRE"
    assert format_label("Total Operating Earnings (TOE)") == "TOE"
    assert format_label("EPS (GAAP)") == "EPS (GAAP)"


def test_compound_legacy_values_keep_their_unit_note_without_guessing() -> None:
    fact = SummaryFact("Backlog and ratio", "164; 1.54", "source", unit="EUR bn; ratio")
    assert "164; 1.54 [EUR bn; ratio]" in format_fact(fact)


def test_model_audit_reason_is_separate_from_the_display() -> None:
    fact = SummaryFact(
        "FY2027 Revenue",
        "$500M",
        "source",
        "guide",
        "FY2027",
        role="guidance",
        relevance="This drives next year's profit",
    )
    assert format_fact(fact) == "FY2027 · Revenue: $500M"
    assert "drives" not in format_fact(fact)


@pytest.mark.parametrize(
    ("metric", "basis", "expected"),
    [
        ("EPS", "non-GAAP", "EPS (non-GAAP)"),
        ("Adjusted EPS", "non-GAAP, diluted", "Adjusted EPS (diluted)"),
        ("Non-GAAP EPS", "GAAP", "Non-GAAP EPS (GAAP)"),
        ("GAAP EPS", "non-GAAP", "GAAP EPS (non-GAAP)"),
    ],
)
def test_basis_is_preserved_and_only_equivalent_labels_are_deduplicated(
    metric: str, basis: str, expected: str
) -> None:
    assert format_fact(SummaryFact(metric, "$1", "source", basis=basis)) == f"{expected}: $1"


def test_all_selected_kpis_and_guides_survive_a_long_brief() -> None:
    facts = [
        SummaryFact(f"Useful measure {i}", "a detailed verified value " * 8, "source")
        for i in range(20)
    ]
    facts += [
        SummaryFact("Orbital transfers", "300M", "source", role="driver"),
        SummaryFact("Revenue outlook", "$5M to $6M", "source", "guide", "FY2027"),
    ]
    text = render_facts("UNSEEN", facts)
    assert len(text.split()) > 90
    assert "Orbital transfers: 300M" in text
    assert "FY2027 · Revenue outlook: $5M–$6M" in text
    assert len(text.splitlines()) == len(facts) + 1


def test_the_replay_uses_safe_formatting_and_preserves_late_kpis() -> None:
    lines = [
        {"metric": "Long context", "value": "word " * 150},
        {
            "metric": "Forward revenue",
            "value": "$2,963 to $2,964 million",
            "kind": "guide",
            "period": "Full year fiscal 2026",
            "currency": "USD",
        },
        {"metric": "Leading volume", "value": "345M", "role": "driver"},
    ]
    text = display_lines("UNSEEN", lines)
    assert "$2.963B–$2.964B" in text
    assert "FYFY" not in text
    assert "Leading volume: 345M" in text
