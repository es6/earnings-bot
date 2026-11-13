"""Adversarial source bindings, always with supported controls."""

import json
from typing import Any

import pytest

from earnings_bot.source_document import (
    SourceDocument,
    SourceTable,
    document_from_html,
    document_from_text,
)
from earnings_bot.summary_binding import bind_answer, bind_brief
from earnings_bot.summary_evidence import parse_referenced_brief
from tests.test_summary_evidence import fact, value


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


TABLE = """<p>Q3 2026 (USD millions)</p><table>
<tr><th>Metric</th><th>Q3 2026</th><th>Q3 2025</th></tr>
<tr><th scope="row">Revenue</th><td>100</td><td>80</td></tr>
<tr><th scope="row">Operating income</th><td>10</td><td>8</td></tr></table>"""


def test_wrong_row_wrong_period_wrong_sign_wrong_scale_and_wrong_unit_are_rejected() -> None:
    document = document_from_html(TABLE)
    assert len(report(document).facts) == 1
    for metric, changes in [
        ("Operating income", {}),
        ("Revenue", {"period": "Q4 2026"}),
        ("Revenue", {"period": "Q3 2025"}),
        ("Revenue", {"amount": "-100"}),
        ("Revenue", {"scale": "one"}),
        ("Revenue", {"unit": "%", "currency": ""}),
        ("Revenue", {"currency": "EUR"}),
    ]:
        result = report(document, metric, **changes)
        assert not result.facts and result.rejected, (metric, changes)


def test_the_two_reproduced_semantic_holes_are_closed_with_a_valid_control() -> None:
    document = document_from_text("Q3 2026 USD\nRevenue was $10 billion.")
    assert report(document, amount="10", scale="billion").facts
    assert not report(document, "Operating income", amount="10", scale="billion").facts
    assert not report(document, amount="10", scale="billion", period="Q4 2026").facts


def test_a_comparative_table_without_column_periods_is_not_assigned_the_headline_period() -> None:
    document = document_from_html(
        TABLE.replace("Q3 2026</th>", "Current</th>").replace("Q3 2025</th>", "Prior</th>")
    )
    assert not report(document, amount="80").facts


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


def bound(document: SourceDocument, metric: str, field: str = "current_actual", **changes: Any):
    """Bind one value of a fact, cited by the document's last block, through bind_answer."""
    block = document.tables[0] if document.tables else document.blocks[-1]
    ref = {
        "document_sha256": document.sha256,
        "block_id": block.id,
        "cell_ids": [c.id for c in block.cells] if isinstance(block, SourceTable) else [],
        "quote": block.text,
    }
    data = fact(metric=metric, current_actual=None)
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
