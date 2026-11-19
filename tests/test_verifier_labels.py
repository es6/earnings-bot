# En dashes join ranges as the summary shows them.
# ruff: noqa: RUF001
"""A label shown on a fact must be stated where its value is: wrong labels the Nov 19 replay
found on correct values (Walmart, Visa, Synopsys, ASML), from short excerpts of the releases."""

from first_print.source_document import SourceDocument, document_from_html, document_from_text
from first_print.summary_format import format_fact
from first_print.summary_policy import SummaryFact
from first_print.summary_verify import verify_facts


def line(fact: SummaryFact, document: SourceDocument) -> str | None:
    kept, _ = verify_facts([fact], document)
    return format_fact(kept[0]) if kept else None


def fact(metric: str, value: str, basis: str = "", **fields: str) -> SummaryFact:
    return SummaryFact(
        metric=metric,
        value=value,
        basis=basis,
        source="",
        period=fields.pop("period", "Q2 FY27"),
        **fields,
    )


WMT = document_from_html(
    "<p>Walmart reports second quarter results</p><p>Q2 FY27 highlights</p>"
    "<p>Dollars in billions, except as noted.</p>"
    "<table><tr><td>Walmart International</td><td>Q2 FY27</td><td>Q2 FY26</td><td>Change</td>"
    "</tr><tr><td>Net sales</td><td>$35.2</td><td>$31.2</td><td>12.8%</td></tr>"
    "<tr><td>Net sales (cc)</td><td>$33.7</td><td>$31.2</td><td>7.9%</td></tr></table>"
)


def test_a_currency_neutral_tag_on_a_reported_row_is_not_shown() -> None:
    sales = fact("Walmart International net sales", "$35.2 billion", "Reported; cc as stated")

    assert (line(sales, WMT) or "").endswith("Walmart International net sales: $35.2B")


def test_a_tag_the_row_states_is_kept() -> None:
    constant = fact("Walmart International net sales (cc)", "$33.7 billion", "constant currency")

    assert "(constant currency): $33.7B" in (line(constant, WMT) or "")


def test_a_currency_neutral_name_on_a_reported_value_is_refused() -> None:
    document = document_from_text(
        "Walmart reports second quarter FY27 results\n"
        "Walmart International net sales were $35.2 billion, an increase of $4.0 billion."
    )
    reported = fact("Walmart International net sales", "$35.2 billion")
    neutral = fact("Walmart International net sales (constant currency)", "$35.2 billion")

    assert line(reported, document)
    assert line(neutral, document) is None


def test_a_name_qualifier_can_be_stated_by_another_passage_that_binds_the_value() -> None:
    document = document_from_text(
        "Walmart reports second quarter results. Fiscal year 2027 outlook\n"
        "For Q3, net sales are expected to grow 3.0% to 3.75%.\n"
        "Net sales for FY27 are expected to grow 4.0% to 5.0%.\n"
        "Net sales (cc) guidance for FY27 is an increase of 4.0% to 5.0%."
    )
    year = fact("Net sales growth (cc) guidance", "4.0% to 5.0%", period="FY27", kind="guide")
    quarter = fact(
        "Net sales growth (cc) guidance", "3.0% to 3.75%", period="Q3 FY27", kind="guide"
    )

    assert line(year, document)
    assert line(quarter, document) is None


def test_all_in_constant_currency_closing_a_sentence_covers_each_number_in_it() -> None:
    document = document_from_text(
        "Walmart reports second quarter results. Fiscal year 2027 outlook\n"
        "Net sales for FY27 are expected to grow 4.0% to 5.0% and adjusted operating income to "
        "grow 7.0% to 8.5%, all in constant currency."
    )
    year = fact("Net sales growth (cc) guidance", "4.0% to 5.0%", period="FY27", kind="guide")

    assert line(year, document)


def test_a_constant_dollar_tag_on_a_nominal_column_is_not_shown() -> None:
    document = document_from_html(
        "<p>Visa fiscal fourth quarter 2026 results</p>"
        "<p>KEY BUSINESS DRIVERS</p><table><tr><td></td><td>Three Months Ended September 30, 2026 "
        "YoY Change Nominal</td></tr>"
        "<tr><td>Cross-border volume excluding intra-Europe</td><td>14%</td></tr></table>"
    )
    volume = fact(
        "Cross-border volume excluding intra-Europe",
        "",
        "Constant-dollar",
        change="+14%",
        comparison="YoY",
        period="Q4 FY2026",
    )

    shown = line(volume, document)

    assert shown and "onstant" not in shown


def test_a_basis_tag_the_targets_table_does_not_state_is_not_shown() -> None:
    document = document_from_html(
        "<p>Synopsys reports third quarter fiscal year 2026 results</p>"
        "<p>Fourth Quarter and Full Fiscal Year 2026 Financial Targets</p>"
        "<p>(in millions, except per share amounts)</p>"
        "<table><tr><td></td><td colspan='2'>Range for Fiscal Year Ending October 31, 2026</td>"
        "</tr><tr><td></td><td>Low</td><td>High</td></tr>"
        "<tr><td>Revenue (1)</td><td>$ 9,690</td><td>$ 9,740</td></tr>"
        "<tr><td>Non-GAAP EPS</td><td>$ 15.04</td><td>$ 15.10</td></tr></table>"
    )
    revenue = fact(
        "Revenue guidance",
        "$9,690-$9,740 million",
        "Non-GAAP guidance",
        period="FY2026",
        kind="guide",
    )
    eps = fact(
        "Non-GAAP EPS guidance", "$15.04-$15.10", "Non-GAAP guidance", period="FY2026", kind="guide"
    )

    assert line(revenue, document) == "FY2026 · Revenue guidance: $9.69B–$9.74B"
    assert "Non-GAAP" in (line(eps, document) or "")


def test_a_title_row_states_the_basis_of_the_rows_under_it() -> None:
    document = document_from_html(
        "<p>GE Aerospace announces third quarter 2026 results</p>"
        "<table><tr><td colspan='2'>Dollars in millions</td><td>2026</td><td>2025</td></tr>"
        "<tr><td colspan='2'>GAAP Metrics</td><td></td><td></td></tr>"
        "<tr><td colspan='2'>Profit</td><td>2,805</td><td>2,515</td></tr>"
        "<tr><td colspan='2'>Non-GAAP Metrics</td><td></td><td></td></tr>"
        "<tr><td colspan='2'>Operating Profit</td><td>2,988</td><td>2,299</td></tr></table>"
    )
    adjusted = fact("Operating profit", "$2,988 million", "Non-GAAP", period="Q3 2026")
    profit = fact("Profit", "$2,805 million", "Non-GAAP", period="Q3 2026")

    assert line(adjusted, document) == "Q3 2026 · Operating profit (Non-GAAP): $2.99B"
    assert line(profit, document) == "Q3 2026 · Profit: $2.81B"


def test_a_basis_the_release_declares_for_everything_holds_for_each_value() -> None:
    text = (
        "JPMorgan Chase reports third-quarter 2026 results\n"
        "In the discussion below, information is presented on a managed basis, which is a "
        "non-GAAP financial measure, unless otherwise noted.\n"
        "Net interest income was $27.1 billion, up 13%."
    )
    income = fact("Net interest income", "$27.1 billion", "managed", period="3Q26")

    assert (line(income, document_from_text(text)) or "").endswith("income (managed): $27.1B")
    undeclared = document_from_text(text.replace(", unless otherwise noted", ""))
    assert (line(income, undeclared) or "").endswith("Net interest income: $27.1B")


def test_an_approximation_the_source_states_is_shown_and_a_number_in_a_tag_is_not() -> None:
    document = document_from_text(
        "ASML reports third quarter 2026 results\n"
        "We expect R&D costs of around €1.2 billion in Q4 2026."
    )
    costs = SummaryFact(
        metric="R&D costs guidance",
        value="1.2",
        unit="billion",
        currency="EUR",
        basis="Guidance; up 9%",
        source="",
        period="Q4 2026",
        kind="guide",
    )

    assert line(costs, document) == "Q4 2026 · R&D costs guidance: ~€1.2B"


def test_a_range_is_labelled_by_its_own_sentence_not_one_sharing_an_end() -> None:
    document = document_from_text(
        "Walmart reports second quarter results. Fiscal year 2027 outlook\n"
        "For Q3, net sales are expected to grow 3.0% to 3.75% and adjusted operating income "
        "to grow 2.0% to 4.0%. Net sales for FY27 are expected to grow 4.0% to 5.0%, all in "
        "constant currency."
    )
    year = fact(
        "FY27 net sales growth guidance",
        "4.0% to 5.0%",
        "Adjusted; constant currency",
        period="FY27",
        kind="guide",
    )

    shown = line(year, document) or ""

    assert "constant currency" in shown and "Adjusted" not in shown


def test_a_reported_tag_on_an_adjusted_value_is_not_shown() -> None:
    document = document_from_text(
        "Palantir reports third quarter 2026 results\nAdjusted EPS of $0.44, and GAAP EPS of $0.28."
    )
    eps = fact("Adjusted EPS, diluted", "$0.44", "GAAP", period="Q3 2026")

    assert (line(eps, document) or "").endswith("Adjusted EPS, diluted: $0.44")
