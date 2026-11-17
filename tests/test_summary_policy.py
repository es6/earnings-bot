from dataclasses import replace

from first_print.summary import EFFORT, MODEL
from first_print.summary_policy import (
    DEFAULT_COMPARISON,
    EMBER,
    SummaryFact,
    evidence_issues,
)
from first_print.summary_request import structured_request


def test_default_writer_and_comparison_follow_the_selected_models() -> None:
    assert [s.name for s in DEFAULT_COMPARISON] == ["Muse Ember low"]
    assert (EMBER.model, "low") == (MODEL, EFFORT)
    assert "EPS is optional" in structured_request("T", "release")["system"]


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


def test_unknown_change_reference_is_flagged_without_guessing_yoy() -> None:
    fact = SummaryFact("Orbital volume", "113M", "quote", period="Q3 2026", change="13%")
    assert evidence_issues(fact, "quote") == ["missing_change_comparison"]
    assert evidence_issues(replace(fact, comparison="YoY"), "quote") == []
    assert evidence_issues(replace(fact, basis="QoQ"), "quote") == []


def test_quarter_and_full_year_in_one_fact_are_flagged_but_not_deleted() -> None:
    fact = SummaryFact("Revenue outlook", "$5B; $20B", "quote", "guide", "Q4 and FY2026")
    assert evidence_issues(fact, "quote") == ["mixed_fact_periods"]
    assert evidence_issues(replace(fact, period="Q4 FY2026"), "quote") == []
    assert evidence_issues(replace(fact, period="2027-2028", kind="update"), "quote") == []
