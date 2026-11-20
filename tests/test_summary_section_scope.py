"""Business headings give the scope of the prose and tables directly below them."""

from dataclasses import replace

import pytest

from first_print.source_document import document_from_html, document_from_text
from first_print.summary_policy import SummaryFact
from first_print.summary_verify import verify_facts


def revenue(group: str, value: str, metric: str = "Revenue") -> SummaryFact:
    return SummaryFact(
        metric, value, "", kind="segment", group=group, period="Q3 2026", currency="USD"
    )


@pytest.mark.parametrize("heading_table", [False, True])
def test_a_named_business_section_scopes_its_paragraphs_and_ends_at_the_next_heading(
    heading_table: bool,
) -> None:
    # JPM's headings and abbreviated metric names, with another business's figure beside it.
    heading = "COMMERCIAL & INVESTMENT BANK (CIB)"
    title = f"<table><tr><td>{heading}</td></tr></table>" if heading_table else f"<p>{heading}</p>"
    document = document_from_html(
        "<p>JPMorgan Chase reports third-quarter 2026 results</p>"
        + title
        + "<p>Discussion of Results:</p><p>Net revenue was $22.7 billion, up 14%.</p>"
        "<p>ASSET & WEALTH MANAGEMENT (AWM)</p><p>Net revenue was $6.9 billion.</p>"
        "<p>Capital return</p><p>Revenue was $1.2 billion.</p>"
    )
    wanted = revenue("Commercial & Investment Bank", "$22.7 billion", "CIB net revenue")
    other = revenue("Asset & Wealth Management", "$6.9 billion", "AWM net revenue")

    kept, _ = verify_facts([wanted, other], document)

    assert [f.group for f in kept] == [wanted.group, other.group]
    for wrong in (
        replace(wanted, value="$6.9 billion"),
        replace(wanted, value="$1.2 billion"),
        replace(wanted, group="Asset & Wealth Management"),
        replace(wanted, group="", metric="Net revenue", kind="actual"),
    ):
        assert verify_facts([wrong], document)[0] == []


def test_a_business_caption_scopes_its_table_without_inheriting_an_earlier_basis() -> None:
    # GE's segment tables have their own period headers and a separate business caption.
    document = document_from_html(
        "<p>GE Aerospace announces third quarter 2026 results.</p>"
        "<p>Non-GAAP adjustments</p><table><tr><td>Other costs</td><td>1</td></tr></table>"
        "<p>Commercial Engines & Services (CES)</p>"
        "<table><tr><td>Dollars in millions</td><td colspan='2'>Three months ended "
        "September 30</td><td>Nine months ended September 30</td></tr>"
        "<tr><td></td><td>2026</td><td>2025</td><td>2026</td></tr>"
        "<tr><td>Revenue</td><td>10,342</td><td>8,521</td><td>28,992</td></tr></table>"
        "<p>Defense & Propulsion Technologies (DPT)</p>"
        "<table><tr><td>Dollars in millions</td><td>Q3 2026</td></tr>"
        "<tr><td>Revenue</td><td>3,469</td></tr></table>"
    )
    actual = revenue("Commercial Engines & Services", "$10.342 billion")

    kept, _ = verify_facts([actual], document)

    assert len(kept) == 1 and kept[0].group == actual.group
    for wrong in (
        replace(actual, value="$28.992 billion"),
        replace(actual, value="$8.521 billion"),
        replace(actual, value="$3.469 billion"),
        replace(actual, period="Q2 2026"),
        replace(actual, metric="Adjusted revenue"),
        replace(actual, group="", kind="actual"),
    ):
        assert verify_facts([wrong], document)[0] == []


def test_another_business_heading_prevents_a_previous_scope_from_leaking() -> None:
    document = document_from_text(
        "Example reports third quarter 2026 results\n"
        "Northern Operations (NO)\nRevenue was $12 million.\n"
        "Southern Operations (SO)\nRevenue was $7 million.\n"
        "Consolidated results\nRevenue was $20 million."
    )
    correct = revenue("Southern Operations", "$7 million")

    kept, _ = verify_facts([correct], document)

    assert len(kept) == 1 and kept[0].group == correct.group
    for value in ("$12 million", "$20 million"):
        assert verify_facts([replace(correct, value=value)], document)[0] == []
