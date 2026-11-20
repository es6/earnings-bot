"""Matched estimates are local display metadata; unknown EPS basis always stays hidden."""

import copy
import json
from dataclasses import asdict, replace
from typing import Any

import pytest

from first_print.consensus import attach_estimates, calendar_rows, estimate_rows
from first_print.source_document import document_from_text
from first_print.summary import _fact
from first_print.summary_duplicates import combine_duplicates
from first_print.summary_format import format_fact
from first_print.summary_policy import SummaryFact
from tests.fakes import summarizer_answering

RELEASE = (
    "Example reports third quarter fiscal 2027 results for the quarter ended October 31, 2026.\n"
    "Revenue was $43.4 billion, up 5% year-over-year.\n"
    "Adjusted diluted EPS was $3.86 for the third quarter of fiscal 2027."
)
DOC = document_from_text(RELEASE)
FACT = SummaryFact(
    "Revenue",
    "$43.4 billion",
    "Revenue was $43.4 billion, up 5% year-over-year.",
    period="Q3 FY27",
    currency="USD",
    unit="USD",
    change="+5%",
    comparison="YoY",
)
SNAPSHOT: dict[str, Any] = {
    "symbol": "EX",
    "period": "FY2027Q3",
    "fetched_at": "2026-11-19T22:00:00+00:00",
    "window_start": "2026-11-20T08:50:00+00:00",
    "estimates": [
        {
            "metric": "revenue",
            "amount": "42000000000",
            "ending": "2026-10-31",
            "horizon": "fiscal quarter",
            "currency": "USD",
            "basis": "reported",
            "unit": "money",
        },
        {
            "metric": "eps",
            "amount": "3.83",
            "ending": "2026-10-31",
            "horizon": "fiscal quarter",
            "currency": "USD",
            "basis": "",
            "unit": "",
        },
    ],
}


def attach(fact: SummaryFact = FACT, snapshot: dict[str, Any] | None = None) -> SummaryFact:
    return attach_estimates([fact], DOC, snapshot or SNAPSHOT, ticker="EX")[0]


def test_comparison_is_exact_currency_period_and_scope_with_no_inferred_eps_basis() -> None:
    shown = attach()
    assert "🟢 Est. $42B" in format_fact(shown)
    assert format_fact(shown).endswith("; +5% YoY")
    for wrong in (
        replace(FACT, currency="CNY"),
        replace(FACT, period="Q2 FY27"),
        replace(FACT, period="Nine months ended October 31, 2026"),
        replace(FACT, group="Cloud"),
        replace(FACT, metric="Organic revenue"),
        replace(FACT, basis="constant currency"),
        replace(FACT, value="5%"),
        replace(FACT, metric="Adjusted revenue"),
    ):
        assert not attach(wrong).estimate
    eps = replace(FACT, metric="Adjusted EPS", value="$3.86", unit="per share", basis="adjusted")
    assert not attach(eps).estimate  # Supplied AV schema has no EPS basis or share unit.
    verified = copy.deepcopy(SNAPSHOT)
    verified["estimates"][1].update(basis="adjusted", unit="per share")
    assert attach(eps, verified).estimate == "$3.83"
    for wrong in (
        replace(eps, basis="GAAP", metric="EPS"),
        replace(eps, basis="", metric="EPS"),
        replace(eps, unit="per ADS"),
    ):
        assert not attach(wrong, verified).estimate


def test_snapshot_cutoff_identity_duplicates_and_staleness_fail_closed() -> None:
    assert attach().estimate
    for patch in (
        {"symbol": "OTHER"},
        {"period": "FY2026Q3"},
        {"fetched_at": SNAPSHOT["window_start"]},
        {"fetched_at": "2026-11-20T09:00:00+00:00"},
        {"fetched_at": "2026-11-18T08:00:00+00:00"},
        {"fetched_at": "2026-11-19T22:00:00"},
        {"estimates": [SNAPSHOT["estimates"][0]] * 2},
        {"estimates": [{**SNAPSHOT["estimates"][0], "amount": "NaN"}]},
    ):
        assert not attach(snapshot={**SNAPSHOT, **patch}).estimate


def test_units_ranges_approximations_zero_and_foreign_currency_are_not_guessed() -> None:
    assert attach(replace(FACT, value="$43.4", unit="$ in billions")).estimate_result == "above"
    assert attach(replace(FACT, value="$40 billion")).estimate_result == "below"
    assert attach(replace(FACT, value="$41\u2013$43 billion")).estimate_result == "inline"
    assert attach(replace(FACT, value="approximately $43.4 billion")).estimate_result == ""
    zero = {**SNAPSHOT, "estimates": [{**SNAPSHOT["estimates"][0], "amount": "0"}]}
    assert attach(snapshot=zero).estimate == "$0"
    cny = {**SNAPSHOT, "estimates": [{**SNAPSHOT["estimates"][0], "currency": "CNY"}]}
    foreign = attach(replace(FACT, value="43.4 billion", currency="CNY", unit="CNY"), cny)
    assert foreign.estimate == "CNY 42B" and "$" not in foreign.estimate


def test_future_guidance_keeps_its_own_period_and_requires_that_estimate() -> None:
    future = replace(FACT, kind="guide", period="Q4 FY27", change="", comparison="")
    snapshot = {**SNAPSHOT, "estimates": [{**SNAPSHOT["estimates"][0], "ending": "2027-01-31"}]}
    assert attach(future, snapshot).estimate
    assert "Q4 FY2027" in format_fact(attach(future, snapshot))
    assert not attach(future).estimate


def test_provider_parsing_preserves_absent_basis_and_requires_known_currency() -> None:
    raw = {
        "symbol": "EX",
        "estimates": [
            {
                "date": "2026-10-31",
                "horizon": "fiscal quarter",
                "revenue_estimate_average": "42000000000",
                "revenue_estimate_analyst_count": "12",
                "eps_estimate_average": "3.83",
                "eps_estimate_analyst_count": "12",
            }
        ],
    }
    rows = estimate_rows(raw, "EX", "USD")
    assert len(rows) == 2 and rows[1]["basis"] == rows[1]["unit"] == ""
    assert not attach(snapshot={**SNAPSHOT, "estimates": rows}).estimate
    raw["estimates"][0]["revenue_currency"] = "USD"
    verified = estimate_rows(raw, "EX", "USD")
    assert attach(snapshot={**SNAPSHOT, "estimates": verified}).estimate
    for symbol, currency in (("OTHER", "USD"), ("EX", "")):
        with pytest.raises(ValueError):
            estimate_rows(raw, symbol, currency)
    with pytest.raises(ValueError):
        calendar_rows('{"Information":"quota"}')


def test_duplicate_merge_preserves_trusted_comparison_and_refuses_conflicting_estimates() -> None:
    shown = attach()
    [merged] = combine_duplicates([FACT, shown])
    assert merged.estimate == shown.estimate
    assert len(combine_duplicates([shown, replace(shown, estimate="$41B")])) == 2
    forged = _fact({**asdict(FACT), "estimate": "$1", "estimate_result": "above"})
    assert not forged.estimate and not forged.estimate_result


async def test_only_verified_summary_gets_cached_estimates_model_never_sees_them() -> None:
    line = {**asdict(FACT), "estimate": "$1", "estimate_result": "above"}
    writer, asked = summarizer_answering("end_turn", json.dumps({"lines": [line]}))
    writer.verified = True
    result = await writer.write_document("EX", DOC, context={"consensus_snapshot": SNAPSHOT})
    assert "🟢 Est. $42B" in result.text
    assert "consensus_snapshot" not in json.dumps(asked)
    assert json.loads(result.evidence or "{}")["consensus_snapshot"] == SNAPSHOT
    writer.verified = False
    legacy = await writer.write_document("EX", DOC, context={"consensus_snapshot": SNAPSHOT})
    assert "Est." not in legacy.text
