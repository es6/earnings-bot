"""Correct facts the verifier refused on Construction Partners' (ROAD) fiscal 2026 release (Nov 19),
each with a wrong control that must stay refused. Passages are short excerpts of the real
releases: ROAD's in tests/fixtures/releases, others inline."""

from pathlib import Path

from first_print.source_document import SourceDocument, document_from_html, document_from_text
from first_print.summary_format import format_fact
from first_print.summary_policy import SummaryFact
from first_print.summary_validation import validate_facts
from first_print.summary_verify import verify_facts

ROAD = document_from_html(
    (Path(__file__).parent / "fixtures" / "releases" / "road-fy2026-excerpt.html").read_text(
        encoding="utf-8"
    )
)
ROAD_TEXT = "\n".join(block.text for block in ROAD.blocks)


def shown(fact: SummaryFact, document: SourceDocument = ROAD) -> str | None:
    """The line the fact displays as once verified, or None when it is refused."""
    kept, _ = verify_facts([fact], document)
    return format_fact(kept[0]) if kept else None


def fact(metric: str, value: str, period: str = "FY2026", **fields: str) -> SummaryFact:
    return SummaryFact(metric=metric, value=value, source="", period=period, **fields)


# 1. The number before the measure


def test_a_rate_names_its_measure_right_after_it() -> None:
    organic = fact("Organic revenue growth", "7%")

    assert shown(organic) == "FY2026 · Organic revenue growth: 7%"
    # "7% organic revenue growth" is no growth of all revenue (that was 31%)
    assert shown(fact("Revenue growth", "7%")) is None
    # and it was "for the year", not the fourth quarter
    assert shown(fact("Organic revenue growth", "7%", "Q4 FY2026")) is None


def test_a_share_of_growth_is_no_growth_rate() -> None:
    # ROAD's sentence reworded with a share in it: no saved release says this, but a share of
    # a growth ("7% of total revenue growth") must not read as the growth itself
    road = document_from_text(
        "Construction Partners, Inc. Announces Fiscal 2026 Fourth Quarter and Full Year Results\n"
        "Strong infrastructure demand across the Sunbelt supplied 7% of total revenue growth for "
        "the year."
    )

    assert shown(fact("Revenue growth", "7%"), road) is None


def test_a_measure_named_after_its_number_keeps_to_its_own_clause() -> None:
    guide = fact("Revenue growth guidance", "15%", "FY2027", kind="guide")

    assert shown(guide) == "FY2027 · Revenue growth guidance: 15%"
    # "Following record fourth quarter revenue, ..." dates another measure
    assert shown(fact("Revenue growth guidance", "15%", "Q4 FY2027", kind="guide")) is None
    # A past rate is no forecast because the release has an "Outlook" heading
    assert shown(fact("Organic revenue growth guidance", "7%", "FY2027", kind="guide")) is None


def test_an_amount_of_a_measure_takes_the_rest_of_its_name_from_its_clause() -> None:
    added = fact("Backlog added by acquisitions", "$65.0M", "FY2027", kind="update")

    assert shown(added) == "FY2027 · Backlog added by acquisitions: $65M"
    # "added $65.0 million of backlog" is a change, never a backlog of $65.0 million
    assert shown(fact("Backlog", "$65.0M", "FY2027")) is None
    assert shown(fact("Revenue added by acquisitions", "$65.0M", "FY2027")) is None


def test_a_rate_named_after_its_number_must_be_a_measure_and_no_range_end() -> None:
    duol = document_from_text(
        "Duolingo reports third quarter 2026 results\n"
        "the 10% to 12% bookings growth and 15% to 18% revenue growth targets we set at the "
        "start of the year."
    )

    assert shown(fact("Bookings growth", "12%", "Q3 2026"), duol) is None
    assert shown(fact("Bookings growth", "10%", "Q3 2026"), duol) is None  # one end, of a target
    dash = document_from_text(
        "DoorDash reports third quarter 2026 results\n"
        "The value of $65.00 per Instacart share represents a 26% premium to Instacart's "
        "closing price on October 22, 2026."
    )
    assert shown(fact("Premium", "26%", "Q3 2026"), dash) is None


def test_a_rate_named_after_its_number_in_a_forecast_is_no_actual() -> None:
    duol = document_from_text(
        "Duolingo reports third quarter 2026 results\n"
        "We expect full-year Adjusted EBITDA of $323 million, or a 26.5% margin, up $3 million "
        "from our previous guidance."
    )

    assert shown(fact("Margin", "26.5%"), duol) is None


# 2. Numbers written as words


def test_a_count_written_as_a_word_counts_the_phrase_right_after_it() -> None:
    strategic = fact("Strategic acquisitions", "7", kind="update")

    assert shown(strategic) == "FY2026 · Strategic acquisitions: 7"
    # Not acquisitions in general, nor the "high-growth markets" further on
    assert shown(fact("Acquisitions", "7", kind="update")) is None
    assert shown(fact("Markets", "7", unit="markets")) is None
    # Made "during fiscal 2026", not in its fourth quarter
    assert shown(fact("Strategic acquisitions", "7", "Q4 FY2026", kind="update")) is None


def test_the_quote_check_reads_a_count_written_as_a_word() -> None:
    quote = (
        "During fiscal 2026, we advanced our ROAD 2030 goals through organic growth initiatives "
        "and seven strategic acquisitions that expanded our presence in high-growth markets"
    )
    seven = SummaryFact(
        metric="Strategic acquisitions", value="7", source=quote, period="FY2026", kind="update"
    )
    eight = SummaryFact(
        metric="Strategic acquisitions", value="8", source=quote, period="FY2026", kind="update"
    )

    assert validate_facts([seven], ROAD_TEXT) == ([seven], [])
    assert validate_facts([eight], ROAD_TEXT)[0] == []


def test_a_count_after_a_verb_needs_the_verb_in_its_name() -> None:
    completed = fact("Acquisitions completed", "3", "FY2027", kind="update")

    assert shown(completed) == "FY2027 · Acquisitions completed: 3"
    # "in the first few weeks of fiscal 2027" is not fiscal 2026
    assert shown(fact("Acquisitions completed", "3", kind="update")) is None
    # "we completed three acquisitions" counts completions, which the name must say
    assert shown(fact("Acquisitions", "3", "FY2027", kind="update")) is None


def test_one_of_a_list_of_counts_shares_the_lists_verb() -> None:
    cmg = document_from_text(
        "Chipotle reports third quarter 2026 results\n"
        "During the third quarter we opened 84 company-owned restaurants, of which 53 included "
        "a Chipotlane, and five international partner-operated restaurants.\n"
        "We're gaining traction with menu and digital innovation, and accelerating our global "
        "expansion with entry into three new markets this quarter."
    )
    markets = fact("Entry into new markets", "3", "Q3 2026", kind="update")

    assert shown(markets, cmg) == "Q3 2026 · Entry into new markets: 3"
    # Five restaurants opened, not five partner-operated restaurants in all
    partner = fact("International partner-operated restaurants", "5", "Q3 2026", kind="update")
    assert shown(partner, cmg) is None


def test_a_count_of_what_will_happen_is_no_result() -> None:
    now = document_from_text(
        "ServiceNow reports third quarter 2026 financial results\n"
        "ServiceNow today announced that it will attend and have executives present at four "
        "upcoming investor conferences.\n"
        "ServiceNow also unveiled Autonomous Security, the industry's most complete security "
        "offering, with a suite of six unified solutions to help enterprises prevent and "
        "remediate cyber risk."
    )

    assert shown(fact("Unified solutions", "6", "Q3 2026", kind="update"), now) == (
        "Q3 2026 · Unified solutions: 6"
    )
    upcoming = fact("Upcoming investor conferences", "4", "Q3 2026", kind="update")
    assert shown(upcoming, now) is None


def test_a_count_from_a_periods_boundary_is_not_that_periods() -> None:
    # Ten "since the start of fiscal 2026" include three made in fiscal 2027
    assert shown(fact("Acquisitions", "10", kind="update")) is None
    assert shown(fact("Acquisitions", "10", unit="acquisitions")) is None


def test_a_later_year_used_as_a_name_is_no_period() -> None:
    # "our ROAD 2030 goals" names a plan; "fiscal 2027" still dates its clause
    assert shown(fact("Strategic acquisitions", "7", kind="update")) is not None
    assert shown(fact("Acquisitions completed", "3", kind="update")) is None


# 3. Balances on a date


def test_a_balance_on_a_date_is_the_value_of_the_period_ending_then() -> None:
    backlog = fact("Project backlog", "$3.29B", currency="USD")

    # "approximately" is shown as the source words it
    assert shown(backlog) == "FY2026 · Project backlog: ~$3.29B"
    assert shown(fact("Project backlog", "$3.29B", "Q4 FY2026")) == (
        "Q4 FY2026 · Project backlog: ~$3.29B"
    )
    assert shown(fact("Project backlog", "$3.29B", "September 30, 2026")) == (
        "September 30, 2026 · Project backlog: ~$3.29B"
    )
    # The prior quarter's and the prior year's balances are no fiscal 2026 backlog
    assert shown(fact("Project backlog", "$3.36B")) is None
    assert shown(fact("Project backlog", "$3.03B")) is None


def test_a_balance_dated_before_the_periods_end_is_not_its_value() -> None:
    road = document_from_text(
        "Construction Partners, Inc. Announces Fiscal 2026 Fourth Quarter and Full Year "
        "Results for its fiscal fourth quarter and year ended September 30, 2026.\n"
        "Project backlog was approximately $3.29 billion at September 30, 2026.\n"
        "Project backlog was $3.36 billion at June 30, 2026."
    )

    assert shown(fact("Project backlog", "$3.29B"), road) == "FY2026 · Project backlog: ~$3.29B"
    assert shown(fact("Project backlog", "$3.36B"), road) is None
    assert shown(fact("Project backlog", "$3.36B", "Q4 FY2026"), road) is None


# 4. Cash flow outflows in parentheses


def test_a_cash_outflow_can_be_claimed_as_the_amount_spent() -> None:
    assert shown(fact("Business acquisitions, net of cash acquired", "$476.6M")) == (
        "FY2026 · Business acquisitions, net of cash acquired: $476.6M"
    )
    assert shown(fact("Business acquisitions, net of cash acquired", "-$476.6M")) == (
        "FY2026 · Business acquisitions, net of cash acquired: -$476.6M"
    )
    assert shown(fact("Purchases of property, plant and equipment", "$182.1M")) == (
        "FY2026 · Purchases of property, plant and equipment: $182.1M"
    )
    assert shown(fact("Net cash used in investing activities", "$613.5M")) == (
        "FY2026 · Net cash used in investing activities: $613.5M"
    )
    assert shown(fact("Purchase of treasury stock", "$34.3M")) == (
        "FY2026 · Purchase of treasury stock: $34.3M"
    )


def test_a_row_naming_an_inflow_in_full_is_no_outflow() -> None:
    snps = document_from_html(
        "<p>Synopsys reports third quarter fiscal year 2026 results</p>"
        "<table><tr><td>SYNOPSYS, INC. Condensed Consolidated Statements of Cash Flows "
        "(Unaudited, in thousands)</td><td colspan='2'>Nine Months Ended July 31,</td></tr>"
        "<tr><td></td><td>2026</td><td>2025</td></tr>"
        "<tr><td>CASH FLOWS FROM FINANCING ACTIVITIES:</td></tr>"
        "<tr><td>Repayment of debt</td><td>(3,463,726)</td><td>(2,579)</td></tr>"
        "<tr><td>Net cash provided by (used in) financing activities</td><td>(1,901,902)</td>"
        "<td>14,191,608</td></tr></table>"
    )

    assert shown(fact("Repayment of debt", "$3.46B", "9M FY2026"), snps) == (
        "9M FY2026 · Repayment of debt: $3.46B"
    )
    # "provided by (used in)" names an inflow, its note allowing for a negative one: a positive
    # claim would read as $1.9 billion provided
    provided = "Net cash provided by (used in) financing activities"
    assert shown(fact(provided, "$1.9B", "9M FY2026"), snps) is None
    assert shown(fact(provided, "-$1.9B", "9M FY2026"), snps) is not None


def test_a_sign_flip_on_a_row_that_names_no_outflow_is_refused() -> None:
    # An inflow claimed as an outflow, a net decrease claimed as an increase, and the prior
    # year's spend claimed for this year
    assert shown(fact("Proceeds from sale of property, plant and equipment", "-$31.8M")) is None
    assert shown(fact("Net change in cash, cash equivalents and restricted cash", "$25.6M")) is None
    assert shown(fact("Net cash provided by investing activities", "$613.5M")) is None
    assert shown(fact("Business acquisitions, net of cash acquired", "$1.16B")) is None


def test_an_annual_cash_flow_statement_states_its_duration_for_the_quote_check() -> None:
    row = "Net cash provided by operating activities, net of acquisitions | 343,951 | 291,303"
    year = SummaryFact(metric="Operating cash flow", value="$344.0M", source=row, period="FY2026")
    quarter = SummaryFact(
        metric="Operating cash flow", value="$344.0M", source=row, period="Q4 FY2026"
    )

    assert validate_facts([year], ROAD_TEXT) == ([year], [])
    assert validate_facts([quarter], ROAD_TEXT)[0] == []


def test_a_metric_naming_no_measure_is_refused_not_a_crash() -> None:
    assert shown(fact("Total", "$3.674B")) is None
