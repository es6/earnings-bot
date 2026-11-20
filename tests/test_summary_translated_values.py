"""Currency translations and explicit business descriptions preserve value identity."""

from dataclasses import replace
from pathlib import Path

import pytest

from first_print.source_document import document_from_html, document_from_text
from first_print.summary_policy import SummaryFact
from first_print.summary_validation import validate_facts
from first_print.summary_verify import verify_facts

DOCUMENT = document_from_html(
    (Path(__file__).parent / "fixtures/wrd-summary-20261120.html").read_text(encoding="utf-8")
)


def passage(beginning: str) -> str:
    return next(b.text for b in DOCUMENT.blocks if b.text.startswith(beginning))


def test_a_named_business_after_the_measure_is_scope_rather_than_a_prior_value() -> None:
    fact = SummaryFact(
        "Overseas revenue",
        "RMB146.9 million",
        passage("Revenue grew"),
        kind="segment",
        group="Overseas",
        period="Q3 2026",
        currency="CNY",
        change="+171.5%",
        comparison="YoY",
    )

    [shown], _ = verify_facts([fact], DOCUMENT)

    assert shown.group == "Overseas" and shown.change == "+171.5%"
    for wrong in (
        replace(fact, value="RMB321.5 million"),
        replace(fact, currency="USD"),
        replace(fact, group="Domestic"),
        replace(fact, metric="Revenue", group="", kind="actual"),
        replace(fact, period="Q2 2026"),
    ):
        assert not verify_facts([wrong], DOCUMENT)[0]
    wider = document_from_text(
        "Example reports third quarter 2026 results.\nRevenue from overseas operations and "
        "membership fees was RMB146.9 million."
    )
    assert not verify_facts([fact], wider)[0]


def test_a_translated_adjusted_loss_keeps_the_explicit_prior_year_and_currency() -> None:
    fact = SummaryFact(
        "Non-IFRS adjusted loss",
        "RMB336.5 million",
        passage("Non-IFRS adjusted loss"),
        period="Q3 2026",
        currency="CNY",
        basis="Non-IFRS",
        prior="RMB275.6 million",
    )

    [shown], _ = verify_facts([fact], DOCUMENT)

    assert shown.prior == fact.prior and shown.value == fact.value
    for wrong in (
        replace(fact, value="RMB275.6 million", prior="RMB336.5 million"),
        replace(fact, currency="USD"),
        replace(fact, period="Q3 2025"),
        replace(fact, metric="Reported loss", basis="IFRS"),
    ):
        assert not verify_facts([wrong], DOCUMENT)[0]
    [without_prior], _ = verify_facts([replace(fact, prior="RMB310.0 million")], DOCUMENT)
    assert not without_prior.prior


@pytest.mark.parametrize("currency", ["RMB", "EUR", "GBP"])
def test_negative_amounts_keep_their_sign_across_currency_spelling_and_translation(
    currency: str,
) -> None:
    text = (
        "Example reports third quarter 2026 results.\n"
        f"EBITDA was negative {currency}246.4 million (US$36.7 million) "
        "in the third quarter of 2026."
    )
    doc = document_from_text(text)
    fact = SummaryFact(
        "EBITDA",
        f"-{currency}246.4 million",
        text.split("\n")[1],
        period="Q3 2026",
        currency="CNY" if currency == "RMB" else currency,
    )

    assert verify_facts([fact], doc)[0]
    assert not verify_facts([replace(fact, value=f"{currency}246.4 million")], doc)[0]
    assert not verify_facts([replace(fact, currency="USD")], doc)[0]


def test_a_descriptive_exclusion_note_cannot_discard_or_relabel_an_adjusted_measure() -> None:
    fact = SummaryFact(
        "Non-IFRS adjusted loss",
        "RMB336.5 million",
        passage("Non-IFRS adjusted loss"),
        period="Q3 2026",
        currency="CNY",
        basis="Non-IFRS; excluding share-based compensation",
    )

    accepted, notes = validate_facts([fact], DOCUMENT.text, unquoted=True)
    [shown], _ = verify_facts(accepted, DOCUMENT)

    assert shown.metric == fact.metric and not shown.basis
    assert notes and "exclusion" in notes[0]
    invented = replace(fact, metric="Adjusted loss excluding fuel")
    accepted, _ = validate_facts([invented], DOCUMENT.text, unquoted=True)
    assert not verify_facts(accepted, DOCUMENT)[0]


def test_a_versioned_product_count_keeps_its_bound_and_exact_product_identity() -> None:
    text = (
        "Example reports third quarter 2026 results.\n"
        "As of the end of the quarter, cumulative deliveries of the Company's "
        "WRD 3.0 L2++/L3 solution exceeded 100,000 units."
    )
    doc = document_from_text(text)
    fact = SummaryFact(
        "WRD 3.0 cumulative deliveries",
        ">100,000",
        text.split("\n")[1],
        period="Q3 2026",
        unit="units",
    )

    assert verify_facts([fact], doc)[0]
    for wrong in (
        replace(fact, value="100,000"),
        replace(fact, metric="WRD 4.0 cumulative deliveries"),
        replace(fact, metric="XYZ 3.0 cumulative deliveries"),
        replace(fact, period="Q2 2026"),
    ):
        assert not verify_facts([wrong], doc)[0]


def test_an_explicit_cash_and_investments_aggregate_requires_all_components_and_the_sum() -> None:
    fact = SummaryFact(
        "Cash and investments aggregate",
        "RMB4,663.0 million",
        passage("As of September 30"),
        period="September 30, 2026",
        currency="CNY",
    )

    assert verify_facts([fact], DOCUMENT)[0]
    for wrong in (
        replace(fact, metric="Cash aggregate"),
        replace(fact, value="RMB3,846.5 million"),
        replace(fact, currency="USD"),
        replace(fact, period="June 30, 2026"),
    ):
        assert not verify_facts([wrong], DOCUMENT)[0]
    wrong_sum = document_from_text(DOCUMENT.text.replace("RMB797.7 million", "RMB800.0 million"))
    assert not verify_facts([fact], wrong_sum)[0]
    extra_component = document_from_text(DOCUMENT.text.replace("in restricted cash", "in loans"))
    assert not verify_facts([fact], extra_component)[0]
