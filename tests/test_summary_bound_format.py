"""Presentation preserves bound fields and only computes comparable exact pairs."""

import json
from typing import Any

import pytest

from earnings_bot.source_document import document_from_html
from earnings_bot.summary_binding import BindingReport, BoundFact, bind_brief
from earnings_bot.summary_bound_format import display_facts, render_bound, value_text
from earnings_bot.summary_evidence import ReferencedFact, parse_referenced_brief
from tests.test_summary_binding import TABLE
from tests.test_summary_evidence import fact, value


def trusted(**changes: Any) -> BindingReport:
    candidate = ReferencedFact.model_validate_json(json.dumps(fact(**changes)))
    return BindingReport((BoundFact(candidate, ("test-only",)),), ())


def test_actual_prior_and_material_comparable_growth_survive_together() -> None:
    qualifiers = "excluding foreign exchange, acquisitions and the calendar shift"
    report = trusted(
        prior_year_actual=value(amount="80", period="Q3 2025"),
        comparable_growth=value(
            amount="20",
            scale="one",
            unit="%",
            currency="",
            comparison="YoY",
            comparison_qualifier=qualifiers,
        ),
    )
    shown = render_bound(report)
    assert "$100M" in shown and "+25% YoY" in shown
    assert "+20% YoY " + qualifiers in shown


def test_margin_changes_are_basis_points_and_level_qualifiers_are_not_dropped() -> None:
    shown = render_bound(
        trusted(
            metric="Operating margin",
            current_actual=value(amount="24", unit="%", currency="", scale="one"),
            prior_year_actual=value(
                amount="20", unit="%", currency="", scale="one", period="Q3 2025"
            ),
        )
    )
    assert "+400 bps YoY" in shown and "+20%" not in shown
    qualifier = "excluding the acquired business and foreign exchange effects"
    shown = render_bound(trusted(current_actual=value(comparison_qualifier=qualifier)))
    assert qualifier in shown


@pytest.mark.parametrize("current,prior", [("10", "0"), ("10", "-5"), ("-5", "10")])
def test_zero_negative_prior_or_losses_show_levels_instead_of_misleading_growth(
    current: str,
    prior: str,
) -> None:
    shown = render_bound(
        trusted(
            current_actual=value(amount=current),
            prior_year_actual=value(amount=prior, period="Q3 2025"),
        )
    )
    assert "a year ago" in shown and "%" not in shown


def test_qualified_counts_keep_units_and_do_not_get_precise_growth_from_approximations() -> None:
    shown = render_bound(
        trusted(
            current_actual=value(
                amount="200", currency="", unit="square feet", scale="one", value_qualifier="nearly"
            ),
            prior_year_actual=value(
                amount="100", currency="", unit="square feet", scale="one", period="Q3 2025"
            ),
        )
    )
    assert "nearly 200 square feet" in shown and "$" not in shown
    assert "100 square feet a year ago" in shown and "%" not in shown


def test_guidance_ranges_and_quarter_actuals_keep_their_own_periods() -> None:
    shown = render_bound(
        trusted(
            new_guidance=value(amount="10.123", upper="10.456", scale="billion", period="Q4 2026"),
            previous_guidance=value(amount="9", upper="10", scale="billion", period="Q4 2026"),
        )
    )
    assert "Q3 2026" in shown and "Q4 2026" in shown
    assert "10.123" in shown and "10.456" in shown and "from" in shown
    assert "25%" not in shown


def test_only_bound_values_reach_the_renderer_and_a_rejected_comparison_is_absent() -> None:
    document = document_from_html(TABLE)
    table = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [c.id for c in table.cells],
        "quote": table.text,
    }
    data = fact(
        current_actual=value(basis="", evidence=[ref]),
        prior_year_actual=value(amount="999", period="Q3 2025", basis="", evidence=[ref]),
    )
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    with pytest.raises(TypeError, match="BindingReport"):
        render_bound(candidate)  # type: ignore[arg-type]
    shown = render_bound(bind_brief(candidate, document))
    assert "$100M" in shown and "999" not in shown


def test_no_fixed_word_line_or_fact_limit_and_multiple_growth_periods_are_not_collapsed() -> None:
    report = trusted()
    facts = tuple(
        BoundFact(report.facts[0].fact.model_copy(update={"metric": f"Measure {n}"}), ())
        for n in range(50)
    )
    shown = render_bound(BindingReport(facts, ()))
    assert all(f"Measure {n}" in shown for n in range(50))
    growths = trusted(
        current_actual=None,
        reported_growth=value(amount="20", unit="%", scale="one", currency="", comparison="YoY"),
        comparable_growth=value(
            amount="15",
            unit="%",
            scale="one",
            currency="",
            period="FY2026",
            comparison="YoY",
            comparison_qualifier="ex FX",
        ),
    )
    shown = render_bound(growths)
    assert "Q3 2026" in shown and "FY2026" in shown and "+15% YoY ex FX" in shown


def test_per_share_precision_currency_and_previous_only_guidance_remain_explicit() -> None:
    report = trusted(current_actual=value(amount="0.64123", unit="per share", scale="one"))
    assert "$0.64123/share" in display_facts(report)[0].value
    current = report.facts[0].fact.current_actual
    assert current is not None
    assert "CAD" in value_text(current.model_copy(update={"currency": "CAD"}))
    shown = render_bound(
        trusted(current_actual=None, previous_guidance=value(amount="90", period="Q4 2026"))
    )
    assert "Previous Revenue guidance" in shown
