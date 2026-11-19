# En dashes join ranges as the releases and the live answers write them.
# ruff: noqa: RUF001
"""Correct facts the verifier refused on the saved Ember answers (Nov 19 replay), each with a
wrong control that must stay refused. Passages are short excerpts of the real releases."""

from first_print.binding_text import fiscal_offset, metric_words
from first_print.source_document import SourceDocument, document_from_html, document_from_text
from first_print.summary_policy import SummaryFact
from first_print.summary_validation import validate_facts
from first_print.summary_verify import verify_facts


def shown(fact: SummaryFact, document: SourceDocument) -> SummaryFact | None:
    kept, _ = verify_facts([fact], document)
    return kept[0] if kept else None


def actual(metric: str, value: str, period: str = "Q3 2026", **fields: str) -> SummaryFact:
    return SummaryFact(metric=metric, value=value, source="", period=period, **fields)


PLTR = document_from_text(
    "Palantir reports third quarter 2026 results\n"
    "•Cash, cash equivalents, and U.S. Treasury securities of $10.4 billion"
)


def test_an_abbreviation_does_not_end_the_clause_that_names_the_measure() -> None:
    whole = actual("Cash, cash equivalents, and U.S. Treasury securities", "$10.4 billion")

    assert shown(whole, PLTR) == whole


def test_a_number_that_totals_a_list_is_not_its_first_item() -> None:
    assert shown(actual("Cash", "$10.4 billion"), PLTR) is None
    assert shown(actual("Cash equivalents", "$10.4 billion"), PLTR) is None


NFLX = document_from_text(
    "Netflix reports third quarter 2026 results\n"
    "Revenue in Q3 grew 12% year over year (+11% on a foreign exchange (F/X) neutral basis), "
    "and was in-line with our forecast."
)


def test_a_rate_named_as_growth_is_checked_as_growth() -> None:
    reported = actual("Revenue growth", "12%", change="+12%", comparison="YoY")
    neutral = actual("Revenue growth, F/X neutral", "11%", change="+11%", comparison="YoY")

    assert shown(reported, NFLX) == reported
    assert shown(neutral, NFLX) == neutral


def test_a_currency_neutral_label_needs_the_source_to_say_so_of_that_number() -> None:
    # The Nov 12 known-wrong fact, and the same mislabel given without its change
    assert shown(actual("Revenue growth (F/X neutral)", "12%", change="+11%"), NFLX) is None
    assert shown(actual("Revenue growth (F/X neutral)", "12%"), NFLX) is None
    assert shown(actual("Revenue growth", "11%"), NFLX) is None


def test_a_plus_signed_rate_beside_the_same_change_is_a_growth_rate() -> None:
    document = document_from_text(
        "Chipotle reports third quarter 2026 results\n"
        "Comparable restaurant sales increased 2.1%, including a 1.3% increase in average "
        "check and a 0.8% increase in transactions."
    )
    check = actual("Average check", "+1.3%", change="+1.3%", comparison="YoY")

    assert shown(check, document) == check
    assert shown(actual("Transactions", "+1.3%", change="+1.3%"), document) is None


def test_per_share_after_the_amount_is_part_of_the_measures_name() -> None:
    document = document_from_text(
        "Interactive Brokers reports third quarter 2026 results\n"
        "The Board of Directors declared an increase in the quarterly cash dividend from "
        "$0.0875 per share to $0.15 per share."
    )
    dividend = actual("Quarterly cash dividend per share", "$0.15")

    assert shown(dividend, document) == dividend
    assert shown(actual("Quarterly cash dividend per share", "$0.0875"), document) is None


def test_a_share_is_named_by_the_measure_it_is_a_share_of() -> None:
    document = document_from_text(
        "Chipotle reports third quarter 2026 results\n"
        "Digital sales represented 38.8% of total food and beverage revenue for the three "
        "months ended September 30, 2026."
    )
    share = actual("Digital sales share of food and beverage revenue", "38.8%")

    assert shown(share, document) == share
    assert shown(actual("Digital sales share of total revenue", "38.8%"), document) is None


def test_a_scale_given_as_the_unit_is_the_values_scale() -> None:
    document = document_from_text(
        "ASML reports third quarter 2026 results\n"
        "ASML expects Q4 2026 total net sales between €14.2 billion and €15.2 billion."
    )
    guide = SummaryFact(
        metric="Total net sales guidance",
        value="14.2-15.2",
        unit="billion",
        currency="EUR",
        source="",
        period="Q4 2026",
        kind="guide",
    )

    assert shown(guide, document) == guide
    assert shown(guide.__class__(**(vars(guide) | {"unit": "million"})), document) is None


WMT = (
    "<p>Walmart reports second quarter results</p><p>Q2 FY27 highlights</p>"
    "<table><tr><td>Total revenues</td><td>Q2 FY27</td></tr><tr><td>Revenue</td><td>177.4</td>"
    "</tr></table>"
    "<table><tr><td>Business Highlights Dollars in billions, except as noted.</td></tr></table>"
    "<table><tr><td>Walmart U.S.</td><td>Q2 FY27</td><td>Q2 FY26</td><td>Change</td></tr>"
    "<tr><td>Net sales</td><td>$125.2</td><td>$120.9</td><td>3.5%</td></tr></table>"
    "<p>•Membership fee revenue grew double-digits</p>"
    "<table><tr><td>Sam's Club U.S.</td><td>Q2 FY27</td><td>Q2 FY26</td><td>Change</td></tr>"
    "<tr><td>Net sales</td><td>$25.7</td><td>$23.6</td><td>8.8%</td></tr></table>"
)


def test_a_caption_laid_out_as_a_table_scales_the_tables_after_it() -> None:
    document = document_from_html(WMT)
    us = actual("Net sales", "$125.2 billion", "Q2 FY27", group="Walmart U.S.")
    club = actual("Net sales", "$25.7 billion", "Q2 FY27", group="Sam's Club U.S.")

    assert shown(us, document) == us
    assert shown(club, document) == club
    assert (
        shown(actual("Net sales", "$25.7 million", "Q2 FY27", group="Sam's Club U.S."), document)
        is None
    )


def test_a_cell_no_label_names_binds_no_measure() -> None:
    document = document_from_html(
        "<p>ServiceNow reports third quarter 2026 results</p>"
        "<table><tr><td></td><td>19</td></tr></table>"
    )

    assert shown(actual("Transactions over $5 million in net new ACV", "19"), document) is None


SNPS = (
    "<p>Synopsys reports third quarter fiscal year 2026 results</p>"
    "<p>Fourth Quarter and Full Fiscal Year 2026 Financial Targets</p>"
    "<p>(in millions, except per share amounts)</p>"
    "<table><tr><td></td><td colspan='2'>Range for Three Months Ending October 31, 2026</td>"
    "<td colspan='2'>Range for Fiscal Year Ending October 31, 2026</td></tr>"
    "<tr><td></td><td>Low</td><td>High</td><td>Low</td><td>High</td></tr>"
    "<tr><td>Revenue (1)</td><td>$ 2,530</td><td>$ 2,580</td><td>$ 9,690</td><td>$ 9,740</td></tr>"
    "</table>"
)


def guide(value: str, period: str) -> SummaryFact:
    return SummaryFact(
        metric="Revenue guidance", value=value, source="", period=period, kind="guide"
    )


def test_a_range_is_read_from_its_low_and_high_columns() -> None:
    document = document_from_html(SNPS)
    quarter = guide("$2.53B–$2.58B", "Q4 FY2026")
    year = guide("$9.69B–$9.74B", "FY2026")

    assert shown(quarter, document) == quarter
    assert shown(year, document) == year
    assert shown(guide("$2.53B–$9.74B", "Q4 FY2026"), document) is None
    assert shown(guide("$2.58B–$2.53B", "FY2026"), document) is None


def test_a_section_broken_down_by_segment_names_the_measure_of_its_rows() -> None:
    document = document_from_html(
        "<p>Synopsys reports third quarter fiscal year 2026 results</p>"
        "<table><tr><td></td><td>Three Months Ended July 31, 2026</td></tr>"
        "<tr><td>Adjusted operating margin by segment</td><td></td></tr>"
        "<tr><td>- Design IP</td><td>26.5 %</td></tr></table>"
    )
    margin = actual("Design IP adjusted operating margin", "26.5%", "Q3 FY2026")

    assert shown(margin, document) == margin
    assert shown(actual("Design IP gross margin", "26.5%", "Q3 FY2026"), document) is None


def test_a_tables_introduction_names_its_measure_from_the_measures_own_word() -> None:
    document = document_from_html(
        "<p>Duolingo reports third quarter 2026 results</p>"
        "<p>The table below provides revenues by product type:</p>"
        "<table><tr><td>(in thousands)</td><td>Q3 2025</td><td>Q3 2026</td></tr>"
        "<tr><td>Subscription</td><td>$229,491</td><td>$269,985</td></tr></table>"
    )
    revenue = actual("Subscription revenue", "$270 million")

    assert shown(revenue, document) == revenue
    assert shown(actual("Subscription bookings", "$270 million"), document) is None


NFLX_REGIONS = (
    "<p>Netflix reports third quarter 2026 results</p><p>Regional Breakdown</p>"
    "<table><tr><td colspan='3'></td><td>Q3'26</td></tr>"
    "<tr><td colspan='3'>APAC:</td><td></td></tr>"
    "<tr><td colspan='3'>Revenue</td><td>$ 1,537</td></tr></table>"
)


def test_a_colon_heading_as_wide_as_the_labels_is_the_rows_segment() -> None:
    document = document_from_html(
        NFLX_REGIONS.replace("Breakdown</p>", "Breakdown (in millions)</p>")
    )
    apac = actual("Revenue", "$1.54 billion", group="APAC", kind="segment")

    assert shown(apac, document) == apac
    assert shown(actual("Revenue", "$1.54 billion"), document) is None


def test_a_subtotal_that_names_the_measure_is_not_held_by_the_grouping_above_it() -> None:
    document = document_from_html(
        "<p>Visa reports fiscal fourth quarter and full-year 2026 results</p>"
        "<table><tr><td>(in millions)</td><td>Fiscal 2026</td><td>Fiscal 2025</td></tr>"
        "<tr><td>Changes in operating assets and liabilities:</td><td></td><td></td></tr>"
        "<tr><td>Accrued liabilities</td><td>412</td><td>380</td></tr>"
        "<tr><td>Net cash provided by (used in) operating activities</td><td>23,728</td>"
        "<td>23,059</td></tr></table>"
    )
    cash = actual("Net cash provided by operating activities", "$23.73 billion", "FY2026")

    assert shown(cash, document) == cash
    assert shown(actual("Accrued liabilities", "$23.73 billion", "FY2026"), document) is None


def test_a_label_note_is_read_part_by_part() -> None:
    document = document_from_html(
        "<p>ASML reports third quarter 2026 results</p>"
        "<table><tr><td></td><td>Q2 2026</td><td>Q3 2026</td></tr>"
        "<tr><td>EPS (basic; in euros)</td><td>7.59</td><td>12.95</td></tr></table>"
    )
    basic = actual("EPS (basic)", "€12.95")

    assert shown(basic, document) == basic
    assert shown(actual("EPS (diluted)", "€12.95"), document) is None


def test_names_keep_their_words_and_drop_abbreviations_and_lead_ins() -> None:
    assert metric_words("EPS (basic)") == ["eps", "basic"]
    assert metric_words('Net interest margin ("NIM")') == ["net", "interest", "margin"]
    assert metric_words("...of which Installed Base Management sales") == [
        "install",
        "bas",
        "management",
        "sal",
    ]


def test_a_fiscal_year_ahead_of_the_dates_its_periods_ended_sets_the_offset() -> None:
    walmart = (
        "Walmart reports Q2 FY27 results. "
        + "Strong growth across the business, led by eCommerce and advertising. " * 5
        + "Comp sales for the 13-week period ended July 31, 2026"
    )
    calendar = "Acme reports Q3 2026 results. Three months ended September 30, 2026"

    assert fiscal_offset(walmart) == 1
    assert fiscal_offset(calendar) == 0


GE = (
    "GE Aerospace Full-Year 2026 Guidance:\n"
    "Based on GE Aerospace's year-to-date performance and outlook for the fourth quarter, the "
    "company is raising total company guidance across the board and now expects:\n"
    "| 2025 | 2026 Guide Prior | 2026 Guide Current\n"
    "Operating Profit* | $9.1B | $10.55 - $10.75B | $10.9 - $11.0B\n"
    "In 2026, CES now expects revenue growth of low 20s, up from our prior guide of ~20%."
)


def test_a_range_scale_written_once_as_a_letter_covers_both_ends() -> None:
    raised = SummaryFact(
        metric="Operating profit guide",
        value="$10.9B–$11B",
        change="Raised versus prior guide of $10.55-$10.75B",
        source="Operating Profit* | $9.1B | $10.55 - $10.75B | $10.9 - $11.0B",
        period="FY2026",
        kind="guide",
    )

    accepted, rejected = validate_facts([raised], GE)

    assert accepted == [raised], rejected
    assert validate_facts([raised.__class__(**(vars(raised) | {"value": "$10.9B–$12B"}))], GE)[1]


def test_up_from_the_prior_guide_establishes_a_raise() -> None:
    raised = SummaryFact(
        metric="CES revenue growth guide",
        value="Low 20s%",
        change="Raised versus prior guide of ~20%",
        source=GE.splitlines()[-1].rstrip("."),
        period="FY2026",
        kind="guide",
    )

    release = GE.splitlines()[-1]
    assert validate_facts([raised], release)[0] == [raised]
    held = release.replace("up from our prior guide of", "in line with our prior guide of")
    moved = raised.__class__(**(vars(raised) | {"source": held}))
    assert not validate_facts([moved], held)[0]


def test_guide_names_the_guidance_slot_like_guidance() -> None:
    document = document_from_html(
        "<p>GE Aerospace announces third quarter 2026 results</p>"
        "<p>GE Aerospace Full-Year 2026 Guidance:</p>"
        "<table><tr><td></td><td>2025</td><td>2026 Guide Prior</td><td>2026 Guide Current</td></tr>"
        "<tr><td>Adjusted EPS*</td><td>$6.37</td><td>$7.65 - $7.85</td><td>$8.00 - $8.10</td></tr>"
        "</table>"
    )
    eps = SummaryFact(
        metric="Adjusted EPS guide", value="$8–$8.1", source="", period="FY2026", kind="guide"
    )

    assert shown(eps, document) == eps
    assert (
        shown(eps.__class__(**(vars(eps) | {"metric": "Adjusted EBITDA guide"})), document) is None
    )
