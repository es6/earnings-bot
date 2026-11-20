"""Important selected facts must bind by units and grammar, not by numeric occurrence."""

from dataclasses import replace

import pytest

from first_print.source_document import document_from_text
from first_print.summary_policy import SummaryFact
from first_print.summary_verify import verify_facts


@pytest.mark.parametrize("unit", ["$ in billions", "$ billion", "USD billions", "in billions"])
def test_money_unit_spelling_preserves_the_explicit_scale(unit: str) -> None:
    source = "Revenue was $115.8 billion in the third quarter of fiscal 2027."
    doc = document_from_text("Example reports third quarter fiscal 2027 results.\n" + source)
    fact = SummaryFact("Revenue", "$115.8", source, period="Q3 FY27", currency="USD", unit=unit)
    assert verify_facts([fact], doc)[0]
    assert not verify_facts(
        [replace(fact, value="$115.8 billion", unit="USD", period="Q2 FY27")], doc
    )[0]
    assert not verify_facts([replace(fact, unit="USD millions")], doc)[0]
    assert not verify_facts([replace(fact, currency="EUR")], doc)[0]


def test_an_amount_added_to_a_measure_is_not_the_remaining_or_total_balance() -> None:
    source = (
        "In September 2026, Example's Board approved an increase of $150 billion "
        "to the company's share repurchase authorization, without expiration."
    )
    doc = document_from_text("Example reports third quarter fiscal 2027 results.\n" + source)
    fact = SummaryFact(
        "Share repurchase authorization increase",
        "$150 billion",
        source,
        kind="update",
        period="September 2026",
        currency="USD",
    )
    assert verify_facts([fact], doc)[0]
    for wrong in (
        replace(fact, metric="Remaining share repurchase authorization"),
        replace(fact, metric="Share repurchase authorization"),
        replace(fact, metric="Acquisition consideration increase"),
        replace(fact, period="September 2025"),
    ):
        assert not verify_facts([wrong], doc)[0]


def test_a_remaining_amount_under_a_named_measure_keeps_its_approximation() -> None:
    source = (
        "As of the end of the third quarter, the company had approximately $227.3 billion "
        "remaining under its share repurchase authorization."
    )
    doc = document_from_text("Example reports third quarter fiscal 2027 results.\n" + source)
    fact = SummaryFact(
        "Remaining share repurchase authorization",
        "$227.3 billion",
        source,
        period="Q3 FY27",
        currency="USD",
    )
    [shown], _ = verify_facts([fact], doc)
    assert shown.value.startswith("~")
    assert not verify_facts([replace(fact, metric="Share repurchase authorization increase")], doc)[
        0
    ]
    assert not verify_facts([replace(fact, metric="Remaining acquisition funding")], doc)[0]
