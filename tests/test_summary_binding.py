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
from earnings_bot.summary_binding import bind_brief
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
    "claim,accepted",
    [
        ("The facility opens in 2027.", True),
        ("The facility opens in 2027 and doubles demand.", False),
    ],
)
def test_unsupported_business_consequences_are_held_back(claim: str, accepted: bool) -> None:
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
    assert bool(bind_brief(candidate, document).facts) == accepted
