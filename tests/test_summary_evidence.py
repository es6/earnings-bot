"""Typed candidates and local traceability, deliberately separate from claim correctness."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from earnings_bot.source_document import document_from_html, document_from_text
from earnings_bot.summary_evidence import parse_referenced_brief
from earnings_bot.summary_policy import HAIKU
from earnings_bot.summary_request import referenced_request
from scripts import summary_prepare
from scripts.summary_prepare import prepare
from tests.builders import DOCUMENT, evidence, fact, value


def answer(candidate: dict[str, Any]) -> str:
    return json.dumps({"schema_version": "source-values/1", "facts": [candidate]})


def test_values_keep_separate_periods_bases_scales_and_material_comparison_qualifiers() -> None:
    growth = evidence(block_id="p2", cell_ids=[], quote=DOCUMENT.blocks[-1].text)
    candidate = fact(
        prior_year_actual=value(
            amount="80", period="Q3 2025", evidence=[evidence(cell_ids=["t1r1c2"])]
        ),
        reported_growth=value(
            amount="25", unit="%", scale="one", currency="", comparison="YoY", evidence=[growth]
        ),
        comparable_growth=value(
            amount="20",
            unit="%",
            scale="one",
            currency="",
            comparison="YoY",
            comparison_qualifier="excluding acquisitions",
            evidence=[growth],
        ),
        previous_guidance=value(amount="0.09", scale="billion", period="FY2026"),
        new_guidance=value(amount="110", upper="120", period="FY2026"),
    )
    [parsed] = parse_referenced_brief(answer(candidate), DOCUMENT).facts
    assert parsed.current_actual and parsed.current_actual.amount == "100"
    assert parsed.prior_year_actual and parsed.prior_year_actual.period == "Q3 2025"
    assert (
        parsed.comparable_growth
        and parsed.comparable_growth.comparison_qualifier == "excluding acquisitions"
    )
    assert parsed.reported_growth and parsed.reported_growth.amount == "25"
    assert parsed.previous_guidance and parsed.previous_guidance.scale == "billion"
    assert parsed.new_guidance and parsed.new_guidance.upper == "120"
    with pytest.raises(ValidationError, match="frozen"):
        parsed.metric = "mutated"


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 10.5},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "1e3"},
        {"amount": "$10B"},
        {"amount": "1,000"},
        {"amount": ""},
        {"upper": "99"},
        {"scale": "unknown"},
        {"unit": " "},
        {"evidence": []},
        {"invented": "value"},
    ],
)
def test_malformed_numeric_candidates_are_rejected_without_coercion(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        parse_referenced_brief(answer(fact(current_actual=value(**changes))), DOCUMENT)


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"document_sha256": "older-version"}, "different document"),
        ({"block_id": "p999"}, "Unknown source reference"),
        ({"block_id": "t1r1c1"}, "block_id must name"),
        ({"cell_ids": []}, "needs cell"),
        ({"cell_ids": ["t2r1c1"]}, "does not belong"),
        ({"block_id": "p1"}, "paragraph evidence"),
        ({"quote": "Revenue growth was 25%"}, "quote is absent"),
        ({"quote": "  "}, "quote is absent"),
    ],
)
def test_evidence_cannot_escape_its_document_block_or_table(
    changes: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_referenced_brief(
            answer(fact(current_actual=value(evidence=[evidence(**changes)]))), DOCUMENT
        )


def test_every_value_and_every_citation_is_checked_including_old_guidance() -> None:
    candidate = fact(
        previous_guidance=value(evidence=[evidence(), evidence(document_sha256="stale")])
    )
    with pytest.raises(ValueError, match=r"previous_guidance.*different document"):
        parse_referenced_brief(answer(candidate), DOCUMENT)


def test_updates_keep_their_supported_consequence_and_plain_text_has_no_cell_geometry() -> None:
    text = "The new facility opens in 2027, adding 5.3 million square feet of capacity."
    document = document_from_text(text)
    ref = evidence(document_sha256=document.sha256, block_id="p1", cell_ids=[], quote=text)
    candidate = fact(current_actual=None, kind="update", update=text, update_evidence=[ref])
    [parsed] = parse_referenced_brief(answer(candidate), document).facts
    assert parsed.update == text and parsed.current_actual is None
    for changes in ({"update": ""}, {"update_evidence": []}):
        with pytest.raises(ValueError, match="supplied together"):
            parse_referenced_brief(answer(candidate | changes), document)
    with pytest.raises(ValueError, match="paragraph evidence"):
        parse_referenced_brief(
            answer(candidate | {"update_evidence": [ref | {"cell_ids": ["t1r0c0"]}]}), document
        )


def test_nested_outer_layout_table_is_not_independent_cell_evidence() -> None:
    document = document_from_html(
        "<table><tr><td>Layout<table><tr><td>100</td></tr></table></td></tr></table>"
    )
    ref = evidence(
        document_sha256=document.sha256, cell_ids=["t1r0c0"], quote=document.tables[0].text
    )
    with pytest.raises(ValueError, match="nested outer"):
        parse_referenced_brief(answer(fact(current_actual=value(evidence=[ref]))), document)


def test_request_preserves_geometry_and_context_and_uses_ember_low() -> None:
    request = referenced_request("TEST", DOCUMENT, {"advisory": "not evidence"})
    content = json.loads(request["messages"][0]["content"])
    source = content["source_document"]
    assert request["model"] == "muse-ember-1.0" and request["output_config"]["effort"] == "low"
    assert source["sha256"] == DOCUMENT.sha256 and source["format"] == "html"
    assert source["tables"][0]["cells"][3] == ["t1r1c0", 1, 0, 1, 1, True, "row", "Revenue"]
    assert content["issuer_context"] == {"advisory": "not evidence"}


def test_the_request_schema_is_stable_across_documents_and_not_shared_between_requests() -> None:
    request = referenced_request("TEST", DOCUMENT)
    schema = request["output_config"]["format"]["schema"]
    second = referenced_request("OTHER", document_from_text("A different report"))
    assert schema == second["output_config"]["format"]["schema"]
    assert DOCUMENT.sha256 not in json.dumps(schema)
    schema["properties"]["facts"]["type"] = "string"
    fresh = referenced_request("TEST", DOCUMENT)["output_config"]["format"]["schema"]
    assert fresh["properties"]["facts"]["type"] == "array"


def test_the_prompt_keeps_the_rules_that_must_not_be_reworded_away() -> None:
    system = referenced_request("TEST", DOCUMENT)["system"]
    assert "no editorial word or line limit" in system
    assert "prior_year_actual" in system and "Context\nis not evidence" in system


def test_input_limits_reject_rather_than_silently_truncate() -> None:
    with pytest.raises(ValueError, match="200,000"):
        referenced_request("TEST", document_from_text("x" * 200001))
    with pytest.raises(ValueError, match="400,000"):
        referenced_request("TEST", DOCUMENT, {"oversized": "x" * 400001})


@pytest.mark.parametrize(
    "amount,unit,currency,qualifier",
    [
        ("0", "money", "USD", "exact"),
        ("-37.5", "money", "USD", "exact"),
        ("200", "square feet", "", "nearly"),
    ],
)
def test_zero_negative_values_and_qualified_counts_keep_their_explicit_units(
    amount: str,
    unit: str,
    currency: str,
    qualifier: str,
) -> None:
    text = f"Result: {qualifier} {amount} {unit}."
    document = document_from_text(text)
    ref = evidence(document_sha256=document.sha256, block_id="p1", cell_ids=[], quote=text)
    candidate = fact(
        current_actual=value(
            amount=amount,
            unit=unit,
            currency=currency,
            value_qualifier=qualifier,
            period="",
            duration="",
            basis="",
            evidence=[ref],
        )
    )
    parsed = parse_referenced_brief(answer(candidate), document).facts[0].current_actual
    assert parsed and (parsed.amount, parsed.unit, parsed.currency, parsed.value_qualifier) == (
        amount,
        unit,
        currency,
        qualifier,
    )


@pytest.mark.parametrize("changes", [{"current_actual": None}, {"metric": " "}])
def test_empty_facts_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="needs a metric"):
        parse_referenced_brief(answer(fact(**changes)), DOCUMENT)


def test_offline_preparation_uses_html_when_saved_and_plain_blocks_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(summary_prepare, "DEFAULT_COMPARISON", (HAIKU,))
    (tmp_path / "TEST.json").write_text(
        json.dumps({"release": "Plain PDF text", "name": "Test issuer"})
    )
    plain = prepare(["TEST"], tmp_path, references=True)["requests"][0]
    assert plain["request"]["model"] == "muse-ember-1.0"
    content = json.loads(plain["request"]["messages"][0]["content"])
    assert (
        content["source_document"]["format"] == "text"
        and content["source_document"]["tables"] == []
    )
    (tmp_path / "TEST.html").write_text("<table><tr><th>Revenue</th><td>100</td></tr></table>")
    html = prepare(["TEST"], tmp_path, references=True)["requests"][0]
    source = json.loads(html["request"]["messages"][0]["content"])["source_document"]
    assert source["format"] == "html" and source["tables"][0]["cells"][1][-1] == "100"
    assert html["document_sha256"] == source["sha256"] != plain["document_sha256"]
    with pytest.raises(ValueError, match="Ember only"):
        prepare(["TEST"], tmp_path, include_haiku=True, references=True)


def test_compact_requests_preserve_original_coordinates_and_all_nonempty_evidence() -> None:
    document = document_from_html(
        "<p>Q3 2026 results, in millions of USD.</p>"
        "<table><tr><th>Metric</th><th></th><th>Q3 2026</th></tr>"
        "<tr><th>Revenue</th><td></td><td>100</td></tr></table>"
    )
    before = document.to_json()
    content = json.loads(referenced_request("TEST", document)["messages"][0]["content"])
    cells = content["source_document"]["tables"][0]["cells"]
    assert len(cells) == 4 and len(document.tables[0].cells) == 6
    assert cells[-1] == ["t1r1c2", 1, 2, 1, 1, False, "", "100"]
    assert document.resolve(document.sha256, "t1r1c1").text == ""
    assert document.to_json() == before
    assert [cell[0] for cell in cells] == [
        cell.id for cell in document.tables[0].cells if cell.text.strip()
    ]
