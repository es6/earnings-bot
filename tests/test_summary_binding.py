"""Adversarial source bindings, always with supported controls."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from first_print.source_document import (
    SourceDocument,
    SourceTable,
    document_from_html,
    document_from_text,
)
from first_print.summary_binding import bind_answer, bind_brief
from first_print.summary_evidence import parse_referenced_brief
from tests.builders import TABLE, fact, value


def html_table() -> SourceDocument:
    return document_from_html(TABLE)


def plain_text() -> SourceDocument:
    return document_from_text(TEXT)


def report(document: SourceDocument, metric: str = "Revenue", **changes: Any):
    block = document.tables[0] if document.tables else document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [c.id for c in block.cells] if isinstance(block, SourceTable) else [],
        "quote": block.text,
    }
    data = fact(metric=metric, current_actual=value(**({"basis": "", "evidence": [ref]} | changes)))
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    return bind_brief(candidate, document)


TEXT = "Q3 2026 USD\nRevenue was $10 billion."
TEXT_CONTROL = {"amount": "10", "scale": "billion"}


@pytest.mark.parametrize(
    ("make_document", "control", "metric", "changes"),
    [
        pytest.param(html_table, {}, "Operating income", {}, id="table-wrong-row"),
        pytest.param(html_table, {}, "Revenue", {"period": "Q4 2026"}, id="table-future-period"),
        pytest.param(html_table, {}, "Revenue", {"period": "Q3 2025"}, id="table-prior-period"),
        pytest.param(html_table, {}, "Revenue", {"amount": "-100"}, id="table-wrong-sign"),
        pytest.param(html_table, {}, "Revenue", {"scale": "one"}, id="table-wrong-scale"),
        pytest.param(
            html_table, {}, "Revenue", {"unit": "%", "currency": ""}, id="table-wrong-unit"
        ),
        pytest.param(html_table, {}, "Revenue", {"currency": "EUR"}, id="table-wrong-currency"),
        pytest.param(plain_text, TEXT_CONTROL, "Operating income", {}, id="text-wrong-metric"),
        pytest.param(
            plain_text, TEXT_CONTROL, "Revenue", {"period": "Q4 2026"}, id="text-wrong-period"
        ),
    ],
)
def test_a_value_that_the_source_does_not_support_is_rejected_but_the_right_one_binds(
    make_document: Callable[[], SourceDocument],
    control: dict[str, str],
    metric: str,
    changes: dict[str, str],
) -> None:
    document = make_document()
    assert len(report(document, **control).facts) == 1

    result = report(document, metric, **(control | changes))

    assert not result.facts and result.rejected


def test_a_comparative_table_without_column_periods_is_not_assigned_the_headline_period() -> None:
    document = document_from_html(
        TABLE.replace("Q3 2026</th>", "Current</th>").replace("Q3 2025</th>", "Prior</th>")
    )
    assert not report(document, amount="80").facts


@pytest.mark.parametrize("comparison", ["up 28% from", "compared with", "versus"])
def test_a_prior_year_comparison_does_not_relabel_the_current_amount(comparison: str) -> None:
    document = document_from_text(
        "Third quarter 2026 results\n"
        f"Sales in the quarter were $8.9 billion, {comparison} the third quarter of 2025."
    )
    assert report(document, "Sales", amount="8.9", scale="billion").facts
    for changes in (
        {"period": "Q3 2025"},
        {"period": "Q4 2026"},
        {"duration": "year"},
        {"currency": "EUR"},
    ):
        assert not report(document, "Sales", amount="8.9", scale="billion", **changes).facts


def test_an_explicit_old_period_before_the_amount_is_never_replaced_by_the_release_year() -> None:
    document = document_from_text(
        "Third quarter 2026 results\n"
        "Sales in the third quarter of 2025 were $8.9 billion, "
        "up 28% from the third quarter of 2024."
    )
    assert not report(document, "Sales", amount="8.9", scale="billion").facts


def test_a_comparator_without_a_numeric_year_cannot_be_called_the_current_amount() -> None:
    document = document_from_text(
        "Q3 2026 USD\nRevenue was $100 million compared with $80 million last year."
    )
    assert report(document, amount="100").facts
    assert not report(document, amount="80").facts


def test_a_new_metric_does_not_inherit_the_previous_metrics_year_ago_label() -> None:
    document = document_from_text(
        "Q3 2026 USD\nGAAP EPS was $0.40 compared to $0.51 a year ago, "
        "and adjusted EPS was $0.73 for the period, compared to $0.75 a year ago."
    )
    assert report(
        document, "Adjusted EPS", amount="0.73", scale="one", unit="per share", basis="adjusted"
    ).facts
    assert not report(
        document, "Adjusted EPS", amount="0.75", scale="one", unit="per share", basis="adjusted"
    ).facts


def test_a_prior_year_amount_needs_its_own_source_period_and_never_borrows_the_current_amount() -> (
    None
):
    document = document_from_text(
        "Q3 2026 USD\nRevenue was $100 million compared with $80 million last year."
    )
    block = document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [],
        "quote": block.text,
    }
    for amount, kept in [("80", True), ("100", False)]:
        data = fact(
            current_actual=value(evidence=[ref]),
            prior_year_actual=value(
                amount=amount,
                period="Q3 2025",
                basis="",
                evidence=[ref],
            ),
        )
        candidate = parse_referenced_brief(
            json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
        )
        bound = bind_brief(candidate, document)
        assert bound.facts
        assert (bound.facts[0].fact.prior_year_actual is not None) == kept
    document = document_from_text(
        "Q3 2026 USD\nRevenue rose from $80 million last year to $100 million."
    )
    assert report(document, amount="100").facts
    assert not report(document, amount="80").facts
    document = document_from_text(
        "Sales in the quarter were $8.9 billion, up 28% from the third quarter of 2025."
    )
    assert not report(document, "Sales", amount="8.9", scale="billion").facts


def test_split_accounting_signs_are_kept_and_cannot_turn_a_loss_positive() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th colspan="4">Q3 2026</th></tr>
    <tr><td>Operating income</td><td>$</td><td>(</td><td>37.5</td><td>)</td></tr></table>""")
    assert report(document, "Operating income", amount="-37.5").facts
    assert not report(document, "Operating income", amount="37.5").facts


def test_qualified_physical_units_do_not_inherit_the_money_caption() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th>Q3 2026</th></tr>
    <tr><td>Capacity (square feet)</td><td>nearly 200</td></tr></table>""")
    assert report(
        document,
        "Capacity",
        amount="200",
        unit="square feet",
        scale="one",
        currency="",
        value_qualifier="nearly",
    ).facts
    assert not report(
        document, "Capacity", amount="200", unit="square feet", scale="one", currency=""
    ).facts


def test_a_business_update_cannot_remove_a_negative_sign() -> None:
    document = document_from_text("The adjustment was -$10 million.")
    ref = {
        "document_sha256": document.sha256,
        "block_id": "p1",
        "cell_ids": [],
        "quote": document.text,
    }
    data = fact(
        current_actual=None,
        kind="update",
        update="The adjustment was $10 million.",
        update_evidence=[ref],
    )
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    assert not bind_brief(candidate, document).facts


def test_a_number_from_a_different_metric_in_the_same_sentence_does_not_bind() -> None:
    document = document_from_text(
        "Q3 2026 USD\nRevenue was $10 billion; operating income was $2 billion."
    )
    assert report(document, amount="10", scale="billion").facts
    assert report(document, "Operating income", amount="2", scale="billion").facts
    assert not report(document, "Operating income", amount="10", scale="billion").facts


def test_a_metric_name_embedded_in_a_margin_does_not_bind_the_absolute_measure() -> None:
    document = document_from_text("Q3 2026\nFree cash flow margin was 20%.")
    assert report(
        document, "Free cash flow margin", amount="20", scale="one", unit="%", currency=""
    ).facts
    assert not report(
        document, "Free cash flow", amount="20", scale="one", unit="%", currency=""
    ).facts


def test_guidance_ranges_keep_old_and_new_columns_separate() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th>Q4 2026 previous guidance</th><th>Q4 2026 new guidance</th></tr>
    <tr><td>Revenue</td><td>90 to 95</td><td>100 to 110</td></tr></table>""")
    table = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [c.id for c in table.cells],
        "quote": table.text,
    }
    new = value(amount="100", upper="110", period="Q4 2026", basis="", evidence=[ref])
    old = value(amount="90", upper="95", period="Q4 2026", basis="", evidence=[ref])
    for previous, accepted in [(old, True), (new, False)]:
        data = fact(kind="guide", current_actual=None, new_guidance=new, previous_guidance=previous)
        candidate = parse_referenced_brief(
            json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
        )
        bound = bind_brief(candidate, document)
        assert len(bound.facts) == 1 and bool(bound.facts[0].fact.previous_guidance) == accepted


def test_growth_columns_are_not_actuals_and_percent_headers_supply_the_unit() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th>Q3 2026 actual</th><th>Q3 2026 YoY growth %</th></tr>
    <tr><td>Revenue</td><td>100</td><td>25</td></tr></table>""")
    table = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [c.id for c in table.cells],
        "quote": table.text,
    }
    change = value(
        amount="25", scale="one", unit="%", currency="", comparison="YoY", basis="", evidence=[ref]
    )
    data = fact(current_actual=value(basis="", evidence=[ref]), reported_growth=change)
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    bound = bind_brief(candidate, document)
    assert bound.facts[0].fact.reported_growth and not bound.rejected
    assert not report(document, amount="25", unit="%", scale="one", currency="").facts


def test_a_bound_but_noncomparable_prior_quarter_cannot_become_a_yoy_pair() -> None:
    document = document_from_html(TABLE.replace("Q3 2025", "Q2 2025"))
    table = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [c.id for c in table.cells],
        "quote": table.text,
    }
    data = fact(
        current_actual=value(basis="", evidence=[ref]),
        prior_year_actual=value(amount="80", period="Q2 2025", basis="", evidence=[ref]),
    )
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    bound = bind_brief(candidate, document)
    assert bound.facts[0].fact.prior_year_actual is None
    assert "not comparable" in bound.rejected[-1].reasons[0]


def test_growth_for_a_different_accounting_basis_is_not_attached_to_the_actual() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th>Q3 2026 actual</th><th>Q3 2026 YoY growth %</th></tr>
    <tr><td>GAAP Revenue</td><td>100</td><td></td></tr>
    <tr><td>Non-GAAP Revenue</td><td></td><td>25</td></tr></table>""")
    table = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [c.id for c in table.cells],
        "quote": table.text,
    }
    data = fact(
        current_actual=value(basis="GAAP", evidence=[ref]),
        reported_growth=value(
            amount="25",
            unit="%",
            scale="one",
            currency="",
            comparison="YoY",
            basis="non-GAAP",
            evidence=[ref],
        ),
    )
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    bound = bind_brief(candidate, document)
    assert bound.facts[0].fact.current_actual and bound.facts[0].fact.reported_growth is None
    assert "basis" in bound.rejected[-1].reasons[0]


def test_column_duration_and_reported_basis_are_bound_without_global_row_search() -> None:
    document = document_from_html("""<p>Q3 2026 USD (in millions)</p><table>
    <tr><th></th><th colspan="2">Three months ended</th><th>Nine months ended</th></tr>
    <tr><th></th><th>Q3 2026</th><th>Q3 2025</th><th>2026</th></tr>
    <tr><th>Non-GAAP free cash flow</th><td>822</td><td>592</td><td>3121</td></tr></table>""")
    assert report(document, "Free cash flow", amount="822", basis="non-GAAP").facts
    assert not report(document, "Free cash flow", amount="822", basis="GAAP").facts
    assert not report(document, "Free cash flow", amount="3121", basis="non-GAAP").facts
    assert report(
        document,
        "Free cash flow",
        amount="3121",
        basis="non-GAAP",
        period="Nine months ended September 30 2026",
        duration="nine months",
    ).facts


def test_group_attribution_is_checked_against_its_source_section() -> None:
    document = document_from_html("""<p>Q3 2026 USD millions</p><table>
    <tr><th>Metric</th><th>Q3 2026</th></tr><tr><td>Asset management</td><td></td></tr>
    <tr><td>Capital deployed</td><td>13</td></tr>
    <tr><td>Wealth solutions</td><td></td></tr>
    <tr><td>Capital deployed</td><td>2</td></tr></table>""")
    block = document.tables[0]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [c.id for c in block.cells],
        "quote": block.text,
    }
    for group, accepted in [("Asset management", True), ("Wealth solutions", False), ("", False)]:
        data = fact(
            metric="Capital deployed",
            group=group,
            current_actual=value(amount="13", basis="", evidence=[ref]),
        )
        candidate = parse_referenced_brief(
            json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
        )
        assert bool(bind_brief(candidate, document).facts) == accepted


def test_an_invalid_optional_prior_is_dropped_while_the_bound_current_value_survives() -> None:
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
        prior_year_actual=value(amount="10", period="Q3 2025", basis="", evidence=[ref]),
    )
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    result = bind_brief(candidate, document)
    assert len(result.facts) == 1 and result.facts[0].fact.prior_year_actual is None
    assert result.rejected[0].field == "prior_year_actual"


@pytest.mark.parametrize(
    "claim,shown",
    [
        ("The facility opens in 2027.", "The facility opens in 2027."),
        # The consequence the source doesn't state is never shown: the source's own sentence is
        ("The facility opens in 2027 and doubles demand.", "The facility opens in 2027."),
        ("A new facility in Texas opens in 2028.", None),
    ],
)
def test_unsupported_business_consequences_are_held_back(claim: str, shown: str | None) -> None:
    document = document_from_text("The facility opens in 2027.")
    ref = {
        "document_sha256": document.sha256,
        "block_id": "p1",
        "cell_ids": [],
        "quote": document.text,
    }
    data = fact(current_actual=None, kind="update", update=claim, update_evidence=[ref])
    candidate = parse_referenced_brief(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    facts = bind_brief(candidate, document).facts
    assert (facts[0].fact.update if facts else None) == shown


# Patterns from the six frozen Ember-low outputs of November 13 (ADBE, CMG, GE, JPM, PLTR,
# WMT), reduced to their shape. Each pairs a supported claim with a false one.


def bound(
    document: SourceDocument,
    metric: str,
    field: str = "current_actual",
    *,
    group: str = "",
    kind: str = "actual",
    **changes: Any,
):
    """Bind one value of a fact, cited by the document's last block, through bind_answer."""
    block = document.tables[0] if document.tables else document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [c.id for c in block.cells] if isinstance(block, SourceTable) else [],
        "quote": block.text,
    }
    data = fact(metric=metric, current_actual=None, group=group, kind=kind)
    data[field] = value(**({"basis": "", "evidence": [ref]} | changes))
    report = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    kept = [getattr(b.fact, field) for b in report.facts if getattr(b.fact, field)]
    return kept[0] if kept else None


RELEASE = "Adobe reports third quarter fiscal 2026 results\n"


def test_a_measure_is_named_by_its_key_words_not_its_exact_label() -> None:
    document = document_from_text(
        RELEASE + "Adobe achieved record revenue of $6.76 billion in its third quarter of FY2026."
    )
    claim = {"amount": "6.76", "scale": "billion", "unit": "dollars", "period": "Q3 FY2026"}
    assert bound(document, "Total revenue", **claim)
    assert not bound(document, "Total operating income", **claim)
    # A rounding of the stated number is that number; a different number is not
    assert bound(document, "Total revenue", **(claim | {"amount": "6.8"}))
    assert not bound(document, "Total revenue", **(claim | {"amount": "6.67"}))
    assert not bound(document, "Total revenue", **(claim | {"amount": "6.761"}))


def test_each_basis_in_one_sentence_binds_to_its_own_number() -> None:
    document = document_from_text(
        RELEASE + "Diluted earnings per share was $4.62 on a GAAP basis and $6.13 on a "
        "non-GAAP basis."
    )
    eps = {"scale": "one", "unit": "dollars per share", "period": "Q3 FY2026"}
    assert bound(document, "Diluted EPS (non-GAAP)", **eps, amount="6.13")
    assert bound(document, "Diluted EPS (GAAP)", **eps, amount="4.62")
    assert not bound(document, "Diluted EPS (non-GAAP)", **eps, amount="4.62")
    assert not bound(document, "Diluted EPS (GAAP)", **eps, amount="6.13")


def test_year_over_year_is_not_read_as_more_than() -> None:
    document = document_from_text(
        RELEASE + "Revenue was $6.76 billion, which represents 13% year-over-year growth."
    )
    growth = {
        "amount": "13",
        "scale": "one",
        "unit": "percent",
        "currency": "",
        "comparison": "YoY",
    }
    assert bound(document, "Revenue", "reported_growth", **growth, period="Q3 FY2026")
    assert not bound(
        document,
        "Revenue",
        "reported_growth",
        **growth,
        period="Q3 FY2026",
        value_qualifier="more_than",
    )


def test_a_decrease_is_a_negative_growth_rate() -> None:
    document = document_from_text(
        "Third quarter 2026 results\nDiluted earnings per share was $0.27, a 6.9% decrease "
        "from $0.29."
    )
    growth = {"scale": "one", "unit": "%", "currency": "", "period": "Q3 2026"}
    assert bound(document, "Diluted EPS", "reported_growth", **growth, amount="-6.9")
    assert not bound(document, "Diluted EPS", "reported_growth", **growth, amount="6.9")


def test_a_fiscal_label_is_compared_with_calendar_columns_through_the_release_offset() -> None:
    document = document_from_html(
        "<p>Walmart reports second quarter of fiscal year 2027 results for the three months "
        "ended July 31, 2026 (amounts in millions).</p><table>"
        "<tr><th></th><th>Three Months Ended July 31, 2026</th>"
        "<th>Three Months Ended July 31, 2025</th></tr>"
        "<tr><td>Operating income</td><td>$9,383</td><td>$7,286</td></tr></table>"
    )
    claim = {"amount": "9383", "scale": "million", "unit": "money"}
    assert bound(document, "Operating income", **claim, period="Q2 FY27")
    assert not bound(document, "Operating income", **claim, period="Q2 FY26")
    assert bound(
        document,
        "Operating income",
        "prior_year_actual",
        amount="7286",
        scale="million",
        period="Q2 FY26",
    )


def test_a_change_column_is_headed_by_the_year_it_compares_with() -> None:
    document = document_from_html(
        "<p>Third quarter 2026 results ($ in millions)</p><table>"
        "<tr><th></th><th>3Q26</th><th>3Q25</th><th>3Q25 O/(U) %</th></tr>"
        "<tr><td>Net revenue</td><td>$46,400</td><td>$43,000</td><td>8</td></tr></table>"
    )
    growth = {"amount": "8", "scale": "one", "unit": "%", "currency": "", "comparison": "YoY"}
    assert bound(document, "Net revenue", "reported_growth", **growth, period="3Q26")
    assert not bound(document, "Net revenue", "reported_growth", **growth, period="3Q27")


def test_a_count_can_name_what_it_counts_after_the_number() -> None:
    document = document_from_text(
        "Third quarter 2026 results\nWe repurchased approximately 9.5 million shares during "
        "the quarter."
    )
    claim = {
        "amount": "9.5",
        "scale": "million",
        "unit": "shares",
        "currency": "USD",
        "period": "Q3 2026",
        "value_qualifier": "approximately",
    }
    shares = bound(document, "Shares repurchased", **claim)
    assert shares and shares.currency == "" and shares.unit == "shares"
    assert not bound(document, "Restaurants opened", **claim)


def test_text_laid_out_in_a_table_cell_is_read_as_text() -> None:
    document = document_from_html(
        "<p>Second quarter fiscal 2027 results</p><table><tr><td>Highlights</td><td>"
        "Free cash flow of $5.5 billion for the six months ended July 31, 2026, a decrease of "
        "$1.4 billion.</td></tr></table>"
    )
    claim = {
        "amount": "5.5",
        "scale": "billion",
        "unit": "money",
        "period": "H1 FY27",
        "duration": "six months",
    }
    assert bound(document, "Free cash flow", **claim)
    assert not bound(document, "Operating cash flow", **claim)


@pytest.mark.parametrize(("period", "accepted"), [("Q3 2026", True), ("Q4 2026", False)])
def test_a_layout_table_before_the_reporting_title_does_not_erase_its_period(
    period: str, accepted: bool
) -> None:
    document = document_from_html(
        "<table><tr><td>Issuer logo</td></tr></table>"
        "<p>Acme Reports Third Quarter 2026 Results</p>"
        "<table><tr><td>Other information</td></tr></table>"
        "<p>Revenue was $10 billion.</p>"
    )
    block = document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [],
        "quote": block.text,
    }
    candidate = fact(
        current_actual=value(amount="10", scale="billion", period=period, basis="", evidence=[ref])
    )
    result = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [candidate]}), document
    )
    assert bool(result.facts) == accepted


@pytest.mark.parametrize("table", [False, True])
def test_a_reporting_title_cannot_turn_an_outlook_value_into_an_actual(table: bool) -> None:
    document = (
        document_from_html(
            "<p>Acme Reports Third Quarter 2026 Results (USD billions)</p>"
            "<p>Full Year 2026 Guidance</p><table><tr><th>Revenue</th>"
            "<td>$12 billion to $13 billion</td></tr></table>"
        )
        if table
        else document_from_text(
            "Acme Reports Third Quarter 2026 Results\nFull Year 2026 Guidance\n"
            "Revenue between $12 billion and $13 billion."
        )
    )
    guide = {"amount": "12", "upper": "13", "scale": "billion", "duration": "full year"}
    assert bound(document, "Revenue", "new_guidance", **guide, period="FY2026")
    assert not bound(document, "Revenue", **guide, period="Q3 2026")


def test_a_full_year_label_cannot_hide_a_quarter_guidance_duration() -> None:
    document = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nQ4 2026 Outlook\n"
        "Revenue between $12 billion and $13 billion."
    )
    guide = {"amount": "12", "upper": "13", "scale": "billion", "duration": "three months"}
    assert bound(document, "Revenue", "new_guidance", **guide, period="Q4 2026")
    assert not bound(document, "Revenue", "new_guidance", **guide, period="FY2026")


def test_unstated_labels_are_dropped_and_a_bad_fact_does_not_sink_the_brief() -> None:
    document = document_from_text(
        "Third quarter 2026 results\nComparable restaurant sales increased 2.1%."
    )
    block = document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [],
        "quote": block.text,
    }
    growth = value(
        amount="2.1", scale="one", unit="%", currency="", comparison="YoY", basis="", evidence=[ref]
    )
    good = fact(
        metric="Comparable restaurant sales",
        group="Profitability",
        current_actual=None,
        comparable_growth=growth,
    )
    broken = fact(metric="Store count", update="New stores", update_evidence=[])
    report = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [broken, good]}), document
    )
    [kept] = [b.fact for b in report.facts if b.fact.comparable_growth]
    # The source says "increased", not year over year, and names no "Profitability" segment
    assert kept.comparable_growth and kept.comparable_growth.comparison == ""
    assert kept.group == ""


def test_positions_alone_never_bind_a_value() -> None:
    """Guards for the looser clause matching above: a flattened table row or a "respectively"
    list ties numbers to items only by position, which proves nothing."""
    row = document_from_text("Third quarter 2026 results\nNet income $ 1,331 $ 1,438 $ 448 $ 421")
    claim = {"scale": "million", "unit": "money", "period": "Q3 2026"}
    assert not bound(row, "Net income", **claim, amount="448")
    listed = document_from_text(
        "Third quarter 2026 results\nBasic and diluted earnings per share were $4.31 and "
        "$4.30, respectively."
    )
    eps = {"scale": "one", "unit": "per share", "period": "Q3 2026"}
    assert not bound(listed, "Diluted EPS", **eps, amount="4.30")
    assert not bound(listed, "Diluted EPS", **eps, amount="4.31")
    sentence = document_from_text(
        "Third quarter 2026 results\nDiluted earnings per share was $4.30, up from $3.90."
    )
    assert bound(sentence, "Diluted EPS", **eps, amount="4.30")


SEGMENTS = """<p>Acme Reports Third Quarter 2026 Results</p>
<p>Net sales for the third quarter by business:</p><table>
<tr><th>(in millions)</th><th>Q3 2026</th><th>Q3 2025</th></tr>
<tr><td>Neuromodulation</td><td>331</td><td>293</td></tr>
<tr><td>Net sales</td><td>5,138</td><td>5,065</td></tr></table>"""


def test_a_segment_row_is_measured_by_the_sentence_that_introduces_its_table() -> None:
    document = document_from_html(SEGMENTS)
    assert bound(document, "Neuromodulation net sales", amount="331")
    # The introduction names the measure, never a different one or the whole company
    assert not bound(document, "Neuromodulation operating income", amount="331")
    assert not bound(document, "Net sales", amount="331")
    # A row that names its own measure keeps it
    assert bound(document, "Net sales", amount="5138")


def test_a_segment_row_is_measured_by_its_value_s_column_header() -> None:
    document = document_from_html("""<p>Acme Reports Third Quarter 2026 Results</p>
    <p>Segment results</p><table>
    <tr><th>(in millions)</th><th colspan="2">Net sales</th>
    <th colspan="2">Operating income</th></tr>
    <tr><th></th><th>Q3 2026</th><th>Q3 2025</th><th>Q3 2026</th><th>Q3 2025</th></tr>
    <tr><td>Americas</td><td>100</td><td>80</td><td>10</td><td>8</td></tr></table>""")
    assert bound(document, "Americas net sales", amount="100")
    assert bound(document, "Americas operating income", amount="10")
    assert not bound(document, "Americas operating income", amount="100")
    assert not bound(document, "Americas net sales", amount="10")


def test_an_abbreviation_the_release_defines_names_its_segment() -> None:
    table = """<table><tr><th>(in millions)</th><th>Q3 2026</th><th>Q3 2025</th></tr>
    <tr><td>MA Revenue</td><td>936</td><td>909</td></tr></table>"""
    defined = document_from_html(
        "<p>Acme Corporation reports third quarter 2026 results for Moody's Analytics "
        '("MA").</p>' + table
    )
    assert bound(defined, "Moody's Analytics Revenue", amount="936")
    assert bound(defined, "Revenue", group="Moody's Analytics", amount="936")
    assert not bound(defined, "Moody's Investors Service Revenue", amount="936")
    undefined = document_from_html("<p>Acme reports third quarter 2026 results.</p>" + table)
    assert not bound(undefined, "Moody's Analytics Revenue", amount="936")


def test_net_can_be_left_out_of_a_measure_unless_the_release_lists_both() -> None:
    sales = {"amount": "8.9", "scale": "billion"}
    document = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nSales in the quarter were a record $8.9 billion."
    )
    assert bound(document, "Net sales", **sales)
    narrower = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nOrganic sales were $8.9 billion."
    )
    assert not bound(narrower, "Net sales", **sales)
    both = document_from_html("""<p>Acme Reports Third Quarter 2026 Results</p><table>
    <tr><th>(in millions)</th><th>Q3 2026</th></tr>
    <tr><td>Sales</td><td>120</td></tr>
    <tr><td>Net sales</td><td>100</td></tr></table>""")
    assert bound(both, "Net sales", amount="100")
    assert not bound(both, "Net sales", amount="120")


def test_guidance_and_growth_name_a_value_s_slot_not_its_measure() -> None:
    document = document_from_html("""<p>Acme Reports Third Quarter 2026 Results</p>
    <p>Full year 2026 guidance</p><table>
    <tr><th>Metric</th><th>FY 2026 guidance</th></tr>
    <tr><td>Adjusted Diluted EPS (1)</td><td>$16.50 to $17.00</td></tr></table>""")
    guide = {
        "amount": "16.50",
        "upper": "17.00",
        "scale": "one",
        "unit": "per share",
        "period": "FY 2026",
        "duration": "twelve months",
        "basis": "adjusted",
    }
    assert bound(document, "Adjusted Diluted EPS Guidance", "new_guidance", **guide)
    assert not bound(document, "Adjusted Diluted EPS Guidance", **guide)
    assert not bound(document, "Operating margin guidance", "new_guidance", **guide)


def test_a_measure_named_right_after_its_number_binds_only_as_that_measure() -> None:
    revenue = {"amount": "1.1", "scale": "billion"}
    document = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nAcme delivered $1.1 billion in revenue."
    )
    assert bound(document, "Revenue", **revenue)
    synergies = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nAcme expects $1.1 billion in revenue synergies."
    )
    assert not bound(synergies, "Revenue", **revenue)
    growth = {"scale": "one", "unit": "%", "currency": "", "amount": "19"}
    organic = document_from_text(
        "Acme Reports Third Quarter 2026 Results\n"
        "The sales increase consisted of 19% growth in organic sales."
    )
    assert bound(organic, "Net sales", "comparable_growth", **growth, basis="organic")
    assert not bound(organic, "Operating income", "comparable_growth", **growth, basis="organic")


def test_earnings_per_share_can_follow_the_net_income_it_divides() -> None:
    eps = {"amount": "0.40", "scale": "one", "unit": "per share"}
    document = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nAcme reported net income attributable to "
        "common stockholders of $582 million, or $0.40 per diluted share."
    )
    assert bound(document, "Diluted EPS", **eps)
    basic = document_from_text(
        "Acme Reports Third Quarter 2026 Results\nAcme reported net income of $582 million, "
        "or $0.40 per basic share."
    )
    assert not bound(basic, "Diluted EPS", **eps)


def test_a_group_naming_the_issuer_or_the_fact_s_role_is_no_segment() -> None:
    document = document_from_html("""<p>Acme Corporation (NYSE: ACME) Reports Third Quarter
    2026 Results</p><table><tr><th>(in millions)</th><th>Q3 2026</th><th>Q3 2025</th></tr>
    <tr><td>ACME Revenue</td><td>1,850</td><td>1,700</td></tr></table>""")
    for group in ("ACME", "Acme Corporation", "headline", "guidance"):
        kept = bound(document, "Revenue", group=group, amount="1850")
        assert kept, group
    assert not bound(document, "Revenue", group="Acme Analytics", amount="1850")


def test_a_heading_row_above_a_segment_names_its_measure() -> None:
    document = document_from_html("""<p>Acme Reports Third Quarter 2026 Results</p><table>
    <tr><th>(in millions)</th><th>Q3 2026</th><th>Q3 2025</th></tr>
    <tr><td colspan="3">Revenue:</td></tr>
    <tr><td>Product</td><td>319</td><td>191</td></tr>
    <tr><td colspan="3">Cost of revenue:</td></tr>
    <tr><td>Service</td><td>46</td><td>39</td></tr></table>""")
    assert bound(document, "Product revenue", amount="319")
    # The nearest heading decides: these service costs are no service revenue
    assert not bound(document, "Service revenue", amount="46")
    assert bound(document, "Service cost of revenue", amount="46")


def test_a_count_of_people_or_things_is_never_bound_as_money() -> None:
    """Duolingo's Q3 2026 users and subscribers were once claimed in USD (Nov 12 study), and the
    binder kept them: a dollar sign elsewhere in their table read as their currency."""
    document = document_from_html(
        "<p>Third quarter 2026 results. Revenue was $271.7 million.</p>"
        "<p>Daily active users (DAUs) grew 29% year-over-year to 64.9 million, and paid "
        "subscribers grew 14% to 13.1 million.</p>"
    )
    per_user = document_from_html(
        "<p>Third quarter 2026 results.</p>"
        "<p>Average revenue per user was $4.19 in the third quarter of 2026.</p>"
    )
    users = {"amount": "64.9", "scale": "million"}
    subscribers = {"amount": "13.1", "scale": "million"}

    assert bound(document, "Daily active users", **users) is None
    assert bound(document, "Paid subscribers", **subscribers) is None
    # The same numbers as what they are
    assert bound(document, "Daily active users", **users, unit="DAUs", currency="")
    assert bound(document, "Paid subscribers", **subscribers, unit="paid subscribers", currency="")
    # And money about users stays money, as do Interactive Brokers' customer credits
    assert bound(per_user, "Average revenue per user", amount="4.19", scale="one")
    credits = document_from_html(
        "<p>Third quarter 2026 results.</p><p>Customer credits were $186.2 billion.</p>"
    )
    assert bound(credits, "Customer credits", amount="186.2", scale="billion")


# Right values the binder refused in the Nov 12 live answers, reduced to their phrasing (Nov 16
# replay). Each binds now; the safety cases after them stay refused.
QUARTER = "Acme reports third quarter 2026 results\n"
BILLION = {"scale": "billion"}
PERCENT = {"unit": "%", "scale": "one", "currency": ""}


@pytest.mark.parametrize(
    ("text", "metric", "field", "group", "kind", "changes"),
    [
        pytest.param(
            "Fourth quarter 2026 outlook:\n•Revenue of between $2.649 - $2.653 billion.",
            "Revenue",
            "new_guidance",
            "",
            "guide",
            BILLION | {"amount": "2.649", "upper": "2.653", "period": "Q4 2026"},
            id="range-with-a-dash-and-one-scale",
        ),
        pytest.param(
            "•Closed $1.463 billion of U.S. commercial TCV, up 11% year-over-year",
            "U.S. commercial TCV closed",
            "current_actual",
            "",
            "actual",
            BILLION | {"amount": "1.463"},
            id="measure-after-of",
        ),
        pytest.param(
            "•Closed total contract value (“TCV”) of $2.918 billion, up 6% year-over-year",
            "Total contract value closed",
            "current_actual",
            "",
            "actual",
            BILLION | {"amount": "2.918"},
            id="verb-first",
        ),
        pytest.param(
            "Pretax income was $5.0 billion.",
            "Pre-tax income",
            "current_actual",
            "",
            "actual",
            BILLION | {"amount": "5.0"},
            id="fused-prefix",
        ),
        pytest.param(
            "Net interest income increased 19% to $1.15 billion.",
            "Net interest income",
            "current_actual",
            "Total net interest income",
            "segment",
            BILLION | {"amount": "1.15"},
            id="group-repeats-the-measure",
        ),
        pytest.param(
            "Customer margin loans increased 36% to $105.2 billion.",
            "Customer margin loans",
            "current_actual",
            "Customer balances",
            "actual",
            BILLION | {"amount": "105.2"},
            id="category-the-release-never-says",
        ),
        pytest.param(
            "Commission revenue increased 12% to $602 million.",
            "Commission revenue",
            "current_actual",
            "Commissions",
            "segment",
            {"amount": "602"},
            id="segment-in-another-form",
        ),
        pytest.param(
            "U.S. revenue grew 119% year-over-year to $1.937 billion.",
            "U.S. revenue",
            "current_actual",
            "U.S. total",
            "segment",
            BILLION | {"amount": "1.937"},
            id="segment-with-a-filler-word",
        ),
        pytest.param(
            "• Adobe AI-first ARR grew more than 150% year over year",
            "AI-first ARR growth",
            "reported_growth",
            "Adobe AI-first ARR",
            "actual",
            PERCENT | {"amount": "150", "value_qualifier": "more_than", "comparison": "YoY"},
            id="group-is-the-whole-measure",
        ),
        pytest.param(
            "Total MAUs grew 11% Y/Y to 788 million.",
            "Monthly active users",
            "current_actual",
            "",
            "actual",
            {"amount": "788", "unit": "users", "currency": ""},
            id="common-abbreviation",
        ),
    ],
)
def test_a_right_value_binds_however_the_release_phrases_it(
    text: str, metric: str, field: str, group: str, kind: str, changes: dict[str, str]
) -> None:
    document = document_from_text(QUARTER + text)

    assert bound(document, metric, field, group=group, kind=kind, **changes)


def test_a_measure_abbreviation_the_release_defines_names_that_measure() -> None:
    """Netflix's "Free cash flow (FCF)" made "FCF forecast" its forecast. The abbreviation is
    expanded everywhere but where it is defined, which would name the measure twice."""
    defined = document_from_text(
        QUARTER + "We define free cash flow (FCF) as operating cash flow less capital "
        "expenditures.\nIn the third quarter of 2026, FCF was $2.9 billion."
    )
    at_definition = document_from_text(
        QUARTER + "Remaining performance obligations (RPO) of $29.7 billion as of Q3 2026"
    )

    assert bound(defined, "Free cash flow", amount="2.9", scale="billion")
    assert bound(
        at_definition, "Remaining performance obligations (RPO)", amount="29.7", scale="billion"
    )


def test_a_phrase_after_of_must_be_the_measure_itself() -> None:
    document = document_from_text(QUARTER + "We realized $300 million of revenue synergies.")

    assert bound(document, "Revenue", amount="300") is None
    assert bound(document, "Revenue synergies", amount="300")
    for part in ("Cloud accounted for", "Cloud contributed", "Cloud represented"):
        share = document_from_text(QUARTER + f"{part} $4.5 billion of revenue.")
        assert bound(share, "Revenue", amount="4.5", scale="billion") is None, part


def test_a_segment_the_release_never_names_is_still_required() -> None:
    """Only a fact the model doesn't call a segment loses a category label the release never
    uses; a segment has to be established."""
    document = document_from_text(
        QUARTER + "Customer margin loans increased 36% to $105.2 billion."
    )
    loans = {"amount": "105.2", "scale": "billion"}

    assert not bound(
        document, "Customer margin loans", group="Asia Pacific", kind="segment", **loans
    )
    assert bound(document, "Customer margin loans", group="Asia Pacific", **loans)


def test_currency_neutral_growth_keeps_its_label_and_is_never_shown_as_reported() -> None:
    """Visa's payments volume grew 9% on a constant-dollar basis (Nov 13 held-out answer). The
    label was dropped, which shows it as the reported growth, like Netflix's F/X-neutral 11%."""
    document = document_from_text(
        QUARTER + "Payments volume for the three months ended September 30, 2026 increased 9% "
        "over the prior year on a constant-dollar basis."
    )
    growth = {"amount": "9", "unit": "%", "scale": "one", "currency": "", "comparison": "YoY"}

    kept = bound(document, "Payments volume", "reported_growth", **growth, basis="Constant-dollar")
    assert kept and "constant" in kept.comparison_qualifier.casefold()
    assert bound(document, "Payments volume", "reported_growth", **growth) is None


def test_an_amount_written_as_money_is_shown_as_money() -> None:
    """Booking's gross bookings were claimed in a unit named after the measure and shown without
    their dollar sign."""
    document = document_from_text(QUARTER + "Gross bookings were $54.5 billion.")
    claim = {"amount": "54.5", "scale": "billion", "unit": "gross bookings", "currency": ""}

    kept = bound(document, "Gross bookings", **claim)

    assert kept and kept.unit == "money" and kept.currency == "USD"


# Headline facts the binder refused in Walmart's and Chipotle's Nov 12 live answers (Nov 16)
WALMART = (
    "Walmart reports second quarter results\n"
    "•Revenue of $187.9 billion, up 5.9% •GAAP EPS of $0.80; Adjusted EPS of $0.81 "
    "•Company issues guidance for Q3; raises outlook for FY27"
)


def test_a_quarter_announced_without_a_year_is_the_reporting_years() -> None:
    """Walmart's title gives the quarter but no year; its fiscal year is stated further on.
    A bullet glued to the next ("$0.81 •Company issues guidance for Q3") is its own clause."""
    document = document_from_text(WALMART)
    eps = {"amount": "0.81", "unit": "per share", "scale": "one", "basis": "adjusted"}

    assert bound(document, "Adjusted EPS", **eps, period="Q2 FY27")
    assert bound(document, "Revenue", amount="187.9", scale="billion", period="Q2 FY27")
    assert not bound(document, "Adjusted EPS", **eps, period="Q3 FY27")


def test_comparable_sales_growth_is_the_measure_itself() -> None:
    document = document_from_text(
        QUARTER + "Comparable restaurant sales increased 2.1%, including a 1.3% increase in "
        "average check and a 0.8% increase in transactions."
    )
    rate = {"unit": "%", "scale": "one", "currency": ""}

    assert bound(document, "Comparable restaurant sales", amount="2.1", **rate)
    # A rate that isn't itself a growth measure still can't be an actual
    assert not bound(document, "Average check", amount="1.3", **rate)


def test_comparable_sales_claim_the_currency_basis_the_source_states() -> None:
    document = document_from_text(
        QUARTER + "Comparable sales increased 4.6%, or 4.1% in constant currency."
    )
    rate = {"unit": "%", "scale": "one", "currency": ""}

    assert bound(document, "Comparable sales", amount="4.6", **rate)
    assert not bound(document, "Comparable sales", amount="4.1", **rate)
    assert bound(document, "Comparable sales", amount="4.1", **rate, basis="constant currency")


def test_a_year_to_date_amount_needs_the_source_to_say_so() -> None:
    """Walmart's "Operating cash flow of $19.7 billion" sits in a highlights list with no
    period; a six-month label there was the model's, as a quarterly one would have been."""
    # Walmart's layout: the title well above, under lines that state no period
    second = (
        "Acme reports second quarter 2026 results\nDollars in billions\n"
        "Dollars in billions, except per share data\n"
    )
    unstated = document_from_text(second + "•Operating cash flow of $19.7 billion")
    stated = document_from_text(
        second + "Operating cash flow for the six months ended June 30, 2026 was $19.7 billion."
    )
    flow = {"amount": "19.7", "scale": "billion", "duration": "six months"}

    assert not bound(unstated, "Operating cash flow", **flow, period="Six months ended Q2 2026")
    assert bound(stated, "Operating cash flow", **flow, period="Six months ended Q2 2026")


def test_a_segment_table_names_its_segment_when_the_quote_is_only_the_value() -> None:
    """Walmart's segment tables carry the segment in their header ("Walmart U.S. | Q2 FY27 |
    Q2 FY26"). The model quoted only "3.5%", and the segment was dropped: Walmart U.S. net
    sales growth was shown as the company's."""
    document = document_from_html(
        "<p>Walmart reports second quarter fiscal 2027 results</p><table>"
        "<tr><th>Walmart U.S.</th><th>Q2 FY27</th><th>Q2 FY26</th><th>Change</th></tr>"
        "<tr><td>Net sales</td><td>$125.2</td><td>$120.9</td><td>3.5%</td></tr></table>"
    )
    table = document.tables[0]
    cell = next(c for c in table.cells if c.text == "3.5%")
    growth = value(
        amount="3.5",
        unit="%",
        scale="one",
        currency="",
        basis="",
        period="Q2 FY27",
        comparison="YoY",
        evidence=[
            {
                "document_sha256": document.sha256,
                "block_id": table.id,
                "cell_ids": [cell.id],
                "quote": "3.5%",
            }
        ],
    )
    data = fact(
        metric="Net sales",
        kind="segment",
        group="Walmart U.S.",
        current_actual=None,
        reported_growth=growth,
    )

    report = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )

    assert [(b.fact.group, b.fact.reported_growth is not None) for b in report.facts] == [
        ("Walmart U.S.", True)
    ]


# Guidance the binder refused in the Nov 12 live answers (Nov 16)
GUIDE = {"unit": "%", "scale": "one", "currency": ""}


def test_guidance_is_named_anywhere_in_its_clause_before_the_number() -> None:
    """ASML's "ASML expects Q4 2026 total net sales between..." and Adobe's "Targets assume
    non-GAAP operating margin of ~44.0%" were refused: only two words before the measure's
    name were read, and "fourth-quarter" wasn't a quarter."""
    expects = document_from_text(
        QUARTER + "ASML expects Q4 2026 total net sales between €14.2 billion and €15.2 billion."
    )
    hyphenated = document_from_text(
        QUARTER + "We expect fourth-quarter 2026 total net sales between €14.2 billion and "
        "€15.2 billion."
    )
    sales = {"amount": "14.2", "upper": "15.2", "scale": "billion", "currency": "EUR"}

    for document in (expects, hyphenated):
        assert bound(
            document, "Total net sales", "new_guidance", kind="guide", **sales, period="Q4 2026"
        )
    assert not bound(expects, "Total net sales", **sales, period="Q3 2026")


def test_an_approximate_source_amount_is_shown_as_approximate() -> None:
    document = document_from_text(
        QUARTER + "We expect 2026 stock-based compensation expense of approximately $1.2 billion."
    )
    claim = {"amount": "1.2", "scale": "billion", "period": "FY 2026", "duration": "twelve months"}

    kept = bound(
        document, "Stock-based compensation expense", "new_guidance", kind="guide", **claim
    )

    assert kept and kept.value_qualifier == "approximately"
    # A different bound is a different claim
    assert not bound(
        document,
        "Stock-based compensation expense",
        "new_guidance",
        kind="guide",
        **claim,
        value_qualifier="more_than",
    )


def test_in_excess_of_is_more_than() -> None:
    document = document_from_text(
        QUARTER + "We are raising our U.S. commercial revenue guidance to in excess of $3.559 "
        "billion."
    )
    claim = {
        "amount": "3.559",
        "scale": "billion",
        "period": "FY 2026",
        "duration": "twelve months",
    }

    assert bound(
        document,
        "U.S. commercial revenue",
        "new_guidance",
        kind="guide",
        **claim,
        value_qualifier="more_than",
    )
    assert not bound(document, "U.S. commercial revenue", "new_guidance", kind="guide", **claim)


GUIDANCE_TABLE = (
    "<p>Walmart reports second quarter fiscal 2027 results</p><table>"
    "<tr><td colspan='3'>The Company's third quarter fiscal 2027 guidance is based on the "
    "following Q3 FY26 figures: net sales of $177.8 billion.</td></tr>"
    "<tr><td>Consolidated metric</td><td>Q3 FY27</td></tr>"
    "<tr><td>Net sales (cc)</td><td>Increase 3.0% to 3.75%</td></tr>"
    "<tr><td>Operating income (cc)</td><td>Decrease 1.0% to 2.0%</td></tr>"
    "</table>"
    "<table><tr><td colspan='4'>Fiscal year 2027</td></tr>"
    "<tr><td colspan='4'>The Company's fiscal year guidance is based on the following FY26 "
    "figures: net sales of $706.4 billion.</td></tr>"
    "<tr><td>Consolidated metric</td><td>Original from 2.19.2026</td>"
    "<td>As of 5.21.2026</td><td>As of 8.20.2026</td></tr>"
    "<tr><td>Net sales (cc)</td><td>Increase 3.5% to 4.5%</td><td>Unchanged</td>"
    "<td>Increase 4.0% to 5.0%</td></tr>"
    "</table>"
)


def guidance_cell(document: SourceDocument, text: str, metric: str, field: str, **changes: Any):
    table = next(t for t in document.tables if any(c.text == text for c in t.cells))
    cell = next(c for c in table.cells if c.text == text)
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [cell.id],
        "quote": text,
    }
    data = fact(metric=metric, kind="guide", current_actual=None)
    data[field] = value(**(GUIDE | {"basis": "", "evidence": [ref]} | changes))
    report = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    return next((getattr(b.fact, field) for b in report.facts if getattr(b.fact, field)), None)


def test_a_guidance_table_states_growth_ranges_as_increases() -> None:
    """Walmart's outlook table: "Net sales (cc) | Increase 3.0% to 3.75%", under a paragraph
    laid out in the table that says it is guidance and names last year's figures."""
    document = document_from_html(GUIDANCE_TABLE)
    q3 = {"amount": "3.0", "upper": "3.75", "period": "Q3 FY27"}
    cc = {"basis": "constant currency"}

    assert guidance_cell(
        document, "Increase 3.0% to 3.75%", "Net sales growth", "new_guidance", **q3, **cc
    )
    # The row is constant-currency growth, so a claim without that label is refused
    assert not guidance_cell(
        document, "Increase 3.0% to 3.75%", "Net sales growth", "new_guidance", **q3
    )
    # A decrease is never read as a plain number, which would lose its sign
    assert not guidance_cell(
        document,
        "Decrease 1.0% to 2.0%",
        "Operating income growth",
        "new_guidance",
        amount="1.0",
        upper="2.0",
        period="Q3 FY27",
        **cc,
    )


def test_original_guidance_is_never_new_guidance() -> None:
    document = document_from_html(GUIDANCE_TABLE)
    cc = {"basis": "constant currency", "period": "FY27", "duration": "twelve months"}

    assert guidance_cell(
        document,
        "Increase 4.0% to 5.0%",
        "Net sales growth",
        "new_guidance",
        amount="4.0",
        upper="5.0",
        **cc,
    )
    assert not guidance_cell(
        document,
        "Increase 3.5% to 4.5%",
        "Net sales growth",
        "new_guidance",
        amount="3.5",
        upper="4.5",
        **cc,
    )


def test_guidance_takes_the_period_of_the_nearest_outlook_heading() -> None:
    """Palantir lists its Q4 outlook, then "For full year 2026:"; the full-year bullet is not
    a quarter's because a Q4 heading sits above it, nor because the title names a quarter."""
    document = document_from_text(
        QUARTER + "For Q4 2026, we expect:\n•Revenue of between $2.649 - $2.653 billion.\n"
        "For full year 2026:\n•We are raising our revenue guidance to between $8.530 - $8.534 "
        "billion."
    )
    full_year = {"amount": "8.530", "upper": "8.534", "scale": "billion", "period": "FY 2026"}

    assert bound(
        document, "Revenue", "new_guidance", kind="guide", **full_year, duration="twelve months"
    )
    assert not bound(
        document, "Revenue", "new_guidance", kind="guide", **full_year, duration="three months"
    )
    # Right under the title, whose "third quarter" is the results' period, not the outlook's
    under_title = document_from_text(
        QUARTER + "We are raising our revenue guidance to between $8.530 - $8.534 billion."
    )
    assert bound(
        under_title, "Revenue", "new_guidance", kind="guide", **full_year, duration="twelve months"
    )


def test_a_prior_column_is_never_new_guidance() -> None:
    """Coca-Cola's outlook table: "Organic revenues | At least 5% growth | Approx. 5% growth"
    under "Current | Prior"."""
    document = document_from_html(
        "<p>Coca-Cola reports third quarter 2026 results</p><p>2026 Outlook</p><table>"
        "<tr><td></td><td>Current</td><td>Prior</td></tr>"
        "<tr><td>Organic revenues growth</td><td>At least 5% growth</td>"
        "<td>Approx. 5% growth</td></tr></table>"
    )
    year = {"period": "FY 2026", "duration": "twelve months", "amount": "5"}

    assert guidance_cell(
        document,
        "At least 5% growth",
        "Organic revenues growth",
        "new_guidance",
        value_qualifier="at_least",
        **year,
    )
    assert not guidance_cell(
        document,
        "Approx. 5% growth",
        "Organic revenues growth",
        "new_guidance",
        value_qualifier="approximately",
        **year,
    )


# Headline facts in tables and segment sections (Nov 16)
SEGMENT_TABLE = (
    "<p>JPMorganChase reports third-quarter 2026 results</p><p>NYSE symbol: JPM</p>"
    "<p>Commercial & Investment Bank (CIB) results follow.</p>"
    "<table><tr><td>Results for CIB</td><td>2Q26</td><td>3Q25</td></tr>"
    "<tr><td>($ millions)</td><td>3Q26</td><td>2Q26</td><td>3Q25</td></tr>"
    "<tr><td>Net revenue</td><td>22,734</td><td>24,853</td><td>19,878</td></tr></table>"
    "<table><tr><td>Results for JPM</td><td>2Q26</td><td>3Q25</td></tr>"
    "<tr><td>($ millions, except per share data)</td><td>3Q26</td><td>2Q26</td><td>3Q25</td></tr>"
    "<tr><td>Net income</td><td>15,276</td><td>14,987</td><td>14,393</td></tr></table>"
)


def table_cell(document: SourceDocument, text: str, metric: str, group: str = "", **changes: Any):
    table = next(t for t in document.tables if any(c.text == text for c in t.cells))
    cell = next(c for c in table.cells if c.text == text)
    ref = {
        "document_sha256": document.sha256,
        "block_id": table.id,
        "cell_ids": [cell.id],
        "quote": text,
    }
    data = fact(metric=metric, group=group, current_actual=value(**({"evidence": [ref]} | changes)))
    report = bind_answer(
        json.dumps({"schema_version": "source-values/1", "facts": [data]}), document
    )
    return report.facts[0].fact if report.facts else None


def test_a_section_titled_by_a_table_names_its_rows() -> None:
    """JPM's "Results for CIB | 2Q26 | 3Q25" titles a segment table; "Results for JPM" is the
    whole company, its ticker given only as "NYSE symbol: JPM"."""
    document = document_from_html(SEGMENT_TABLE)
    cib = {"amount": "22.7", "scale": "billion", "basis": "", "period": "3Q26"}

    assert table_cell(document, "22,734", "CIB net revenue", **cib)
    kept = table_cell(
        document, "22,734", "Net revenue", group="Commercial & Investment Bank (CIB)", **cib
    )
    assert kept and kept.group == "Commercial & Investment Bank (CIB)"
    # Not the company's net revenue, another segment's, or the prior quarter's
    assert not table_cell(document, "22,734", "Net revenue", **cib)
    assert not table_cell(document, "22,734", "CCB net revenue", **cib)
    assert table_cell(
        document, "15,276", "Net income", amount="15.3", scale="billion", basis="", period="3Q26"
    )


def test_a_company_named_by_its_legal_name_is_no_segment() -> None:
    document = document_from_text(
        QUARTER + "JPMorgan Chase & Co. reported:\nNet income was $15.3 billion, up 6%."
    )
    income = {"amount": "15.3", "scale": "billion"}

    assert bound(document, "Net income", group="JPMorgan Chase & Co.", **income)


def test_a_column_header_states_the_scale_nearest_its_value() -> None:
    """ServiceNow's reconciliation: "Amount ($ millions)" above the value, and "Amount ($
    billions)" for another section further down."""
    document = document_from_html(
        "<p>ServiceNow reports third quarter 2026 results</p><table>"
        "<tr><td></td><td>Third Quarter 2026 GAAP Results</td>"
        "<td>Third Quarter 2026 Non-GAAP Results</td></tr>"
        "<tr><td></td><td>Amount ($ millions)</td><td>Amount ($ millions)</td></tr>"
        "<tr><td>Income from operations</td><td>385</td><td>1,338</td></tr>"
        "<tr><td></td><td>Amount ($ billions)</td><td>Amount ($ billions)</td></tr>"
        "<tr><td>cRPO</td><td>13.65</td><td>13.65</td></tr></table>"
    )
    operating = {"scale": "million", "period": "Q3 2026"}

    assert table_cell(
        document,
        "1,338",
        "Non-GAAP income from operations",
        amount="1338",
        basis="non-GAAP",
        **operating,
    )
    assert table_cell(
        document, "385", "Income from operations", amount="385", basis="", **operating
    )
    assert not table_cell(
        document, "1,338", "Income from operations", amount="1338", basis="", **operating
    )


def test_a_reconciliation_heading_names_no_segment() -> None:
    document = document_from_html(
        "<p>Interactive Brokers reports third quarter 2026 results</p><table>"
        "<tr><td>(in millions)</td><td>Three Months Ended September 30, 2026</td></tr>"
        "<tr><td>Non-GAAP adjustments</td></tr>"
        "<tr><td>Adjusted net revenues</td><td>$1,872</td></tr></table>"
    )

    assert table_cell(
        document,
        "$1,872",
        "Adjusted net revenues",
        amount="1872",
        basis="adjusted",
        period="Q3 2026",
    )


def test_a_categorys_label_names_no_segment() -> None:
    document = document_from_text(
        QUARTER + "Key operating metrics\nBookings increased 12% to $316.1 million."
    )

    assert bound(document, "Total bookings", group="Operating Metrics", amount="316.1")


def test_managed_basis_is_checked_where_the_clause_names_it() -> None:
    """JPMorgan reports revenue on a managed basis: "managed revenue of $50.9 billion"."""
    named = document_from_text(
        QUARTER + "Reported revenue of $50.2 billion and managed revenue of $50.9 billion"
    )
    unnamed = document_from_text(QUARTER + "Net revenue was $50.9 billion, up 8%.")
    revenue = {"amount": "50.9", "scale": "billion"}

    assert bound(named, "Net revenue - managed", **revenue)
    assert not bound(unnamed, "Net revenue - managed", **revenue)


def test_an_amount_the_source_states_excluding_items_is_not_the_reported_one() -> None:
    """JPM's "ROTCE of 21%, excluding discretionary investment securities losses": reported
    ROTCE was 20%."""
    document = document_from_text(
        QUARTER + "The Firm generated an ROTCE of 21%, excluding discretionary investment "
        "securities losses."
    )
    rate = {"amount": "21", "unit": "%", "scale": "one", "currency": ""}

    assert not bound(document, "ROTCE", **rate)
    assert bound(document, "ROTCE", **rate, basis="excluding investment securities losses")


def test_a_definition_in_parentheses_is_not_part_of_the_measure() -> None:
    document = document_from_text(QUARTER + "Operating margin was 13.7%, a decrease from 15.9%.")
    margin = {"amount": "13.7", "unit": "%", "scale": "one", "currency": ""}

    assert bound(
        document,
        "Operating margin (income from operations as a percentage of total revenue)",
        **margin,
    )
    # A parenthetical that qualifies the number, or states numbers, stays required
    assert not bound(document, "Operating margin (excluding litigation)", **margin)
    assert not bound(document, "Operating margin (down from 15.9% a year ago)", **margin)


def test_bank_return_measures_are_named_by_their_abbreviations() -> None:
    document = document_from_text(QUARTER + "ROE of 17% and ROTCE of 20%.")
    rate = {"unit": "%", "scale": "one", "currency": ""}

    assert bound(document, "Return on tangible common equity", amount="20", **rate)
    assert bound(document, "Return on equity", amount="17", **rate)
    assert not bound(document, "Return on equity", amount="20", **rate)


# Guidance in prose and outlook tables, as releases lay it out (Nov 16)
YEAR = {"period": "FY 2026", "duration": "twelve months"}


def test_a_forecast_is_read_before_its_number_not_in_the_reasons_after_it() -> None:
    """Netflix: "We have adjusted our expected 2026 FCF forecast to approximately $12.0B (vs.
    our prior projection of $12.5B) due primarily to ... a real estate transaction in Q4." The
    prior projection is the comparator's, the Q4 the reason's; the forecast is the year's."""
    document = document_from_text(
        QUARTER + "Free cash flow (FCF) in Q3'26 totaled $2.9B vs. $2.7B in Q3'25. We have "
        "adjusted our expected 2026 FCF forecast to approximately $12.0B (vs. our prior "
        "projection of $12.5B) due primarily to the expected closing of a real estate "
        "transaction in Q4."
    )
    flow = {"scale": "billion", "value_qualifier": "approximately", **YEAR}

    assert bound(document, "Free cash flow", "new_guidance", kind="guide", amount="12.0", **flow)
    assert not bound(
        document, "Free cash flow", "new_guidance", kind="guide", amount="12.5", **flow
    )


def test_guidance_words_glued_to_a_footnote_or_an_ad_still_count() -> None:
    targets = document_from_text(
        QUARTER + "1Targets assume non-GAAP operating margin of ~44.0% for fourth quarter FY2026."
    )
    ads = document_from_text(QUARTER + "We currently expect 2026 ad revenue of ~$2.7B.")

    assert bound(
        targets,
        "Non-GAAP operating margin",
        "new_guidance",
        kind="guide",
        amount="44.0",
        unit="%",
        scale="one",
        currency="",
        value_qualifier="approximately",
        period="Q4 FY2026",
        basis="non-GAAP",
    )
    assert bound(
        ads,
        "Advertising revenue",
        "new_guidance",
        kind="guide",
        amount="2.7",
        scale="billion",
        value_qualifier="approximately",
        **YEAR,
    )


def test_an_outlook_heading_dates_the_list_below_it() -> None:
    """Chipotle's "For 2027, management is anticipating the following:" and a count whose
    measure is named in full after it, "in the U.S. and Canada"."""
    document = document_from_text(
        QUARTER + "For 2027, management is anticipating the following:\n340 to 360 new "
        "company-owned restaurant openings in the U.S. and Canada"
    )
    openings = {
        "amount": "340",
        "upper": "360",
        "unit": "restaurants",
        "scale": "one",
        "currency": "",
        "period": "FY 2027",
        "duration": "twelve months",
    }

    assert bound(
        document,
        "New company-owned restaurant openings in U.S. and Canada",
        "new_guidance",
        kind="guide",
        **openings,
    )


def test_an_outlook_heading_stays_in_view_below_the_rows_of_its_table() -> None:
    """Spotify's outlook, read from its PDF: "Outlook for Q4'26", "...reflect Spotify's
    expectations for Q4 2026", then a row per measure, each naming "the quarter"."""
    document = document_from_text(
        QUARTER + "Outlook for Q4'26\nThe following forward-looking statements reflect "
        "Spotify's expectations for Q4 2026\nTotal MAUs 818 million Implies the addition of "
        "approximately 30 million net new MAUs in the quarter\nTotal Premium Subscribers 316 "
        "million Implies the addition of approximately 10 million net new subscribers in the "
        "quarter\nGross Margin 33.8% Primarily driven by Y/Y favorability"
    )

    assert bound(
        document,
        "Gross margin",
        "new_guidance",
        kind="guide",
        amount="33.8",
        unit="%",
        scale="one",
        currency="",
        period="Q4 2026",
    )


OUTLOOK_TABLE = (
    "<p>ServiceNow reports third quarter 2026 results</p><table>"
    "<tr><td></td><td>Fourth Quarter 2026 GAAP Guidance</td>"
    "<td>Fourth Quarter 2026 Non-GAAP Guidance</td></tr>"
    "<tr><td></td><td>Year/Year Growth (%)</td><td>Constant Currency Year/Year Growth (%)</td>"
    "</tr><tr><td>cRPO</td><td>18.5%</td><td>18.0%</td></tr>"
    "<tr><td></td><td></td><td>Margin (%)</td></tr>"
    "<tr><td>Income from operations</td><td></td><td>32%</td></tr></table>"
)


def test_a_column_header_says_what_a_rate_in_an_outlook_table_is() -> None:
    """ServiceNow: "cRPO" under "Year/Year Growth (%)" is cRPO growth, under the constant
    currency column its constant currency growth; "Income from operations" under "Margin (%)",
    below those growth columns, is the operating margin and no currency-neutral rate."""
    document = document_from_html(OUTLOOK_TABLE)
    q4 = {"period": "Q4 2026"}

    assert guidance_cell(document, "18.5%", "cRPO growth", "new_guidance", amount="18.5", **q4)
    assert not guidance_cell(document, "18.0%", "cRPO growth", "new_guidance", amount="18.0", **q4)
    assert guidance_cell(
        document,
        "32%",
        "Non-GAAP operating margin",
        "new_guidance",
        amount="32",
        basis="non-GAAP",
        **q4,
    )


def test_a_transposed_outlook_table_names_its_measure_in_the_column() -> None:
    """DoorDash: "Period | Marketplace GOV | Adjusted EBITDA" over "Q4 2026 | $35.4 billion -
    $36.4 billion | $900 million - $1,100 million"."""
    document = document_from_html(
        "<p>DoorDash reports third quarter 2026 results</p><p>Outlook</p><table>"
        "<tr><td>Period</td><td>Marketplace GOV</td><td>Adjusted EBITDA</td></tr>"
        "<tr><td>Q4 2026</td><td>$35.4 billion - $36.4 billion</td>"
        "<td>$900 million - $1,100 million</td></tr></table>"
    )
    gov = {
        "amount": "35.4",
        "upper": "36.4",
        "unit": "money",
        "scale": "billion",
        "currency": "USD",
    }

    assert guidance_cell(
        document,
        "$35.4 billion - $36.4 billion",
        "Marketplace GOV",
        "new_guidance",
        period="Q4 2026",
        **gov,
    )
    assert not guidance_cell(
        document,
        "$35.4 billion - $36.4 billion",
        "Adjusted EBITDA",
        "new_guidance",
        period="Q4 2026",
        **gov,
    )
    assert not guidance_cell(
        document,
        "$35.4 billion - $36.4 billion",
        "Marketplace GOV",
        "new_guidance",
        period="Q1 2027",
        **gov,
    )


# Tuned on the unseen issuers (KO, MCO, ETN, BSX), measured on the untouched held-out set
def test_a_percent_written_once_is_both_ends_of_a_range() -> None:
    """Eaton's "Organic growth of 13-15%", Boston Scientific's "decline ... by approximately
    (3.0) to (1.0) percent"."""
    eaton = document_from_text(
        "Eaton reports third quarter 2026 results\nFor the full year 2026, the company "
        "anticipates:\n•Organic growth of 13-15%"
    )
    boston = document_from_text(
        QUARTER + "The company estimates net sales to decline in the fourth quarter of 2026, "
        "versus the prior year period, by approximately (3.0) to (1.0) percent on a reported "
        "basis."
    )
    rate = {"unit": "%", "scale": "one", "currency": ""}

    assert bound(
        eaton,
        "Organic growth",
        "new_guidance",
        kind="guide",
        amount="13",
        upper="15",
        period="FY 2026",
        duration="twelve months",
        **rate,
    )
    assert bound(
        boston,
        "Net sales growth",
        "new_guidance",
        kind="guide",
        amount="-3.0",
        upper="-1.0",
        value_qualifier="approximately",
        period="Q4 2026",
        duration="three months",
        **rate,
    )


KO_OUTLOOK = (
    "<p>Coca-Cola reports third quarter 2026 results</p><p>2026 Outlook</p><table>"
    "<tr><td></td><td>Current</td><td>Prior</td></tr>"
    "<tr><td>Comparable EPS (non-GAAP)</td><td>At least 11% growth, which includes: "
    "Approx. 3% currency tailwind</td><td>9% to 10% growth, which includes: Approx. 3% "
    "currency tailwind</td></tr>"
    "<tr><td>Free cash flow (non-GAAP)</td><td>At least $13.0 billion, consisting of: At least "
    "$15.2 billion of cash from operations</td><td>Approx. $12.4 billion, consisting of: "
    "Approx. $14.6 billion of cash from operations</td></tr></table>"
)


def test_an_outlook_cell_states_its_value_before_its_breakdown() -> None:
    """Coca-Cola's "Current | Prior" outlook: each cell gives the value, then what it
    includes; the columns are guidance versions, not periods."""
    document = document_from_html(KO_OUTLOOK)
    year = {"period": "FY 2026", "duration": "twelve months", "basis": "non-GAAP"}
    eps = {"unit": "%", "scale": "one", "currency": ""}
    flow = {"scale": "billion", "unit": "money", "currency": "USD"}

    assert guidance_cell(
        document,
        KO_CELL_EPS,
        "Comparable EPS growth",
        "new_guidance",
        amount="11",
        value_qualifier="at_least",
        **eps,
        **year,
    )
    assert guidance_cell(
        document,
        KO_CELL_PRIOR_EPS,
        "Comparable EPS growth",
        "previous_guidance",
        amount="9",
        upper="10",
        **eps,
        **year,
    )
    assert guidance_cell(
        document,
        KO_CELL_PRIOR_FLOW,
        "Free cash flow",
        "previous_guidance",
        amount="12.4",
        value_qualifier="approximately",
        **flow,
        **year,
    )
    # The prior column is never the new guidance
    assert not guidance_cell(
        document,
        KO_CELL_PRIOR_FLOW,
        "Free cash flow",
        "new_guidance",
        amount="12.4",
        value_qualifier="approximately",
        **flow,
        **year,
    )


KO_CELL_EPS = "At least 11% growth, which includes: Approx. 3% currency tailwind"
KO_CELL_PRIOR_EPS = "9% to 10% growth, which includes: Approx. 3% currency tailwind"
KO_CELL_PRIOR_FLOW = (
    "Approx. $12.4 billion, consisting of: Approx. $14.6 billion of cash from operations"
)


def test_a_period_written_year_first_or_with_its_comparison_is_read() -> None:
    """Moody's and Coca-Cola answers wrote "2026Q3", and a growth's period as "Q3 2026 vs Q3
    2025"."""
    document = document_from_text(QUARTER + "Revenue was $2,023 million, up 1%.")

    assert bound(document, "Revenue", amount="2023", period="2026Q3")
    growth = {"unit": "%", "scale": "one", "currency": "", "comparison": "YoY"}
    assert bound(
        document, "Revenue", "reported_growth", amount="1", period="Q3 2026 vs Q3 2025", **growth
    )
    assert not bound(document, "Revenue", amount="2023", period="2025Q3")


def test_a_qualifier_is_stated_by_its_words_in_any_order_and_form() -> None:
    """Moody's "3% on an organic constant currency basis" for a qualifier "constant currency,
    organic"; Eaton's "up 22% organically" for "organic"."""
    moody = document_from_text(
        QUARTER + "Revenue of $2.0 billion increased 1% from the prior-year period and 3% on an "
        "organic constant currency basis."
    )
    eaton = document_from_text(QUARTER + "Sales were $4.2 billion, up 22% organically.")
    growth = {"unit": "%", "scale": "one", "currency": "", "comparison": "YoY"}

    assert bound(
        moody,
        "Revenue",
        "comparable_growth",
        amount="3",
        comparison_qualifier="constant currency, organic",
        **growth,
    )
    assert bound(
        eaton, "Sales", "comparable_growth", amount="22", comparison_qualifier="organic", **growth
    )
    assert not bound(
        moody,
        "Revenue",
        "comparable_growth",
        amount="3",
        comparison_qualifier="organic, excluding acquisitions",
        **growth,
    )


def test_a_forecast_decline_is_never_bound_as_a_rise() -> None:
    document = document_from_text(
        QUARTER + "The company estimates net sales to decline in the fourth quarter of 2026 by "
        "approximately 3 to 1 percent."
    )
    rate = {"unit": "%", "scale": "one", "currency": "", "value_qualifier": "approximately"}
    q4 = {"period": "Q4 2026", "duration": "three months"}

    assert not bound(
        document,
        "Net sales growth",
        "new_guidance",
        kind="guide",
        amount="1",
        upper="3",
        **rate,
        **q4,
    )
    # Stated with its sign it binds as the decline it is, and only so
    stated = document_from_text(
        QUARTER + "The company estimates net sales to decline in the fourth quarter of 2026 by "
        "approximately (3.0) to (1.0) percent."
    )
    decline = {"metric": "Net sales growth", "field": "new_guidance", "kind": "guide"}
    assert bound(stated, **decline, amount="-3.0", upper="-1.0", **rate, **q4)
    assert not bound(stated, **decline, amount="1.0", upper="3.0", **rate, **q4)
