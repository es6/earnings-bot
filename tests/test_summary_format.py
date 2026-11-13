# En dashes are the intended rendered range separators.
# ruff: noqa: RUF001

from dataclasses import replace

import pytest

from earnings_bot.summary_evaluation import display_lines
from earnings_bot.summary_format import (
    discord_brief,
    format_change,
    format_fact,
    format_label,
    format_period,
    format_value,
    render_facts,
)
from earnings_bot.summary_policy import SummaryFact


@pytest.mark.parametrize(
    ("value", "unit", "currency", "expected"),
    [
        ("$2,963.0 to $2,964.0 million", "millions", "USD", "$2.963B–$2.964B"),
        ("$2,963 to $2,964 million", "", "", "$2.963B–$2.964B"),
        ("$2,365-$2,395 million", "USD millions", "USD", "$2.365B–$2.395B"),
        ("845 to 846", "USD millions", "USD", "$845M–$846M"),
        ("€5.01–€5.02 billion", "EUR billion", "EUR", "€5.01B–€5.02B"),
        ("$900M to $1.1B", "USD", "USD", "$0.9B–$1.1B"),
        ("-3% to -1%", "percent", "EUR", "-3%–-1%"),
        ("10750", "USD millions", "USD", "$10.75B"),
        ("4.44", "EUR per share", "EUR", "€4.44"),
        ("0", "USD million", "USD", "$0"),
        ("7000", "", "", "7000"),
        ("1,2", "USD millions", "USD", "1,2"),
        ("around 57", "percent", "", "around 57%"),
        ("~12.0", "billion", "USD", "~$12B"),
        ("4,273 thousand", "thousand", "", "4.27M"),
        ("269985", "USD thousands", "USD", "$270M"),
        ("380000 to 410000", "USD thousands", "USD", "$380M–$410M"),
        ("5 trillion", "USD trillions", "USD", "$5T"),
        ("993 million", "million orders", "USD", "993M"),
        ("19", "count", "USD", "19"),
        ("680", "customers", "USD", "680"),
        ("1.5", "million pages", "EUR", "1.5M"),
        ("$1,338 million (32.5% margin)", "million", "USD", "$1.34B (32.5% margin)"),
        ("$385 million (9.5% margin)", "million", "USD", "$385M (9.5% margin)"),
        ("$900M to $1.1B (annual outlook)", "million", "USD", "$900M to $1.1B (annual outlook)"),
        ("$5M to €6M", "", "", "$5M to €6M"),
        ("€5M to €6M", "EUR million", "USD", "€5M to €6M"),
        ("164; 1.54", "EUR bn; ratio", "EUR", "164; 1.54"),
        ("Profit $5B; margin 15%", "USD", "USD", "Profit $5B; margin 15%"),
    ],
)
def test_units_and_ranges_keep_their_meaning(
    value: str, unit: str, currency: str, expected: str
) -> None:
    assert format_value(value, unit, currency) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full year fiscal 2026", "FY2026"),
        ("FYFY2026", "FY2026"),
        ("Q4 fiscal 2026 ended September 30, 2026", "Q4 FY2026"),
        ("Q3 2026", "Q3 2026"),
        ("3Q2026 (quarter ended 2026-09-30)", "Q3 2026"),
        ("Q4 2026 and FY 2026", "Q4 2026 and FY 2026"),
        ("FY2030", "FY2030"),
        ("Quarter ended September 30, 2026", "Quarter ended September 30, 2026"),
        ("Nine months ended Q3 FY2026", "Nine months ended Q3 FY2026"),
        ("nine-month period ended Q3 FY2026", "nine-month period ended Q3 FY2026"),
        ("6 months ended Q2 2026", "6 months ended Q2 2026"),
        ("Q3 FY2026 YTD", "Q3 FY2026 YTD"),
        ("Year-to-date Q3 FY2026", "Year-to-date Q3 FY2026"),
        ("9M FY2026", "9M FY2026"),
        ("H1 FY2026", "H1 FY2026"),
        ("First half FY2026", "First half FY2026"),
        ("Trailing twelve months ended Q3 2026", "Trailing twelve months ended Q3 2026"),
        ("TTM Q3 2026", "TTM Q3 2026"),
    ],
)
def test_fiscal_periods_are_explicit_without_duplicate_prefixes(value: str, expected: str) -> None:
    assert format_period(value) == expected


def test_duplicate_acronyms_are_removed_without_dropping_the_measure() -> None:
    assert format_label("Fee Related Earnings (FRE)") == "FRE"
    assert format_label("Total Operating Earnings (TOE)") == "TOE"
    assert format_label("EPS (GAAP)") == "EPS (GAAP)"


def test_compound_legacy_values_keep_their_unit_note_without_guessing() -> None:
    fact = SummaryFact("Backlog and ratio", "164; 1.54", "source", unit="EUR bn; ratio")
    assert "164; 1.54 [EUR bn; ratio]" in format_fact(fact)


def test_model_audit_reason_is_separate_from_the_display() -> None:
    fact = SummaryFact(
        "FY2027 Revenue",
        "$500M",
        "source",
        "guide",
        "FY2027",
        role="guidance",
        relevance="This drives next year's profit",
    )
    assert format_fact(fact) == "FY2027 · Revenue: $500M"
    assert "drives" not in format_fact(fact)


def test_cash_flow_duration_survives_the_live_and_replay_formatters() -> None:
    fact = SummaryFact(
        "Operating cash flow", "$2,298.6 million", "quote", period="Nine months ended Q3 FY2026"
    )
    expected = "Nine months ended Q3 FY2026 · Operating cash flow: $2.3B"
    assert format_fact(fact) == expected
    assert expected in display_lines("UNSEEN", [vars(fact)])


@pytest.mark.parametrize(
    ("metric", "basis", "expected"),
    [
        ("EPS", "non-GAAP", "EPS (non-GAAP)"),
        ("Adjusted EPS", "non-GAAP, diluted", "Adjusted EPS (diluted)"),
        ("Non-GAAP EPS", "GAAP", "Non-GAAP EPS (GAAP)"),
        ("GAAP EPS", "non-GAAP", "GAAP EPS (non-GAAP)"),
    ],
)
def test_basis_is_preserved_and_only_equivalent_labels_are_deduplicated(
    metric: str, basis: str, expected: str
) -> None:
    assert format_fact(SummaryFact(metric, "$1", "source", basis=basis)) == f"{expected}: $1"


def test_all_selected_kpis_and_guides_survive_a_long_brief() -> None:
    facts = [
        SummaryFact(f"Useful measure {i}", "a detailed verified value " * 8, "source")
        for i in range(20)
    ]
    facts += [
        SummaryFact("Orbital transfers", "300M", "source", role="driver"),
        SummaryFact("Revenue outlook", "$5M to $6M", "source", "guide", "FY2027"),
    ]
    text = render_facts("UNSEEN", facts)
    assert len(text.split()) > 90
    assert "Orbital transfers: 300M" in text
    assert "FY2027 · Revenue outlook: $5M–$6M" in text
    assert len(text.splitlines()) == len(facts) + 1


def test_the_replay_uses_safe_formatting_and_preserves_late_kpis() -> None:
    lines = [
        {"metric": "Long context", "value": "word " * 150},
        {
            "metric": "Forward revenue",
            "value": "$2,963 to $2,964 million",
            "kind": "guide",
            "period": "Full year fiscal 2026",
            "currency": "USD",
        },
        {"metric": "Leading volume", "value": "345M", "role": "driver"},
    ]
    text = display_lines("UNSEEN", lines)
    assert "$2.963B–$2.964B" in text
    assert "FYFY" not in text
    assert "Leading volume: 345M" in text


@pytest.mark.parametrize(
    ("change", "reference", "expected"),
    [
        ("13%", "YoY", "+13% YoY"),
        ("(2)%", "YoY", "-2% YoY"),
        ("(2%)", "QoQ", "-2% QoQ"),
        ("40 bps", "YoY", "+40 bps YoY"),
        ("0%", "YoY", "0% YoY"),
        ("13%", "", "+13%"),
        ("13% year-over-year", "", "+13% YoY"),
        ("-2% Y/Y; margin 25%", "", "-2% YoY; margin 25%"),
        ("(2%", "YoY", "(2%"),
        ("-0.02", "", "-0.02"),
    ],
)
def test_change_notation_preserves_direction_and_known_comparison(
    change: str, reference: str, expected: str
) -> None:
    assert format_change(change, reference) == expected


def test_explicit_basis_can_label_a_change_without_guessing_from_the_current_quarter() -> None:
    fact = SummaryFact("Refueling revenue", "$5B", "quote", period="Q3 2026", change="13%")
    assert format_fact(fact).endswith("+13%")
    assert format_fact(replace(fact, basis="YoY")).endswith("+13% YoY")
    assert "(YoY)" not in format_fact(replace(fact, basis="YoY"))
    assert format_fact(replace(fact, comparison="QoQ")).endswith("+13% QoQ")


def test_identical_metric_names_keep_their_distinct_segment_and_country_identity() -> None:
    fact = SummaryFact("Revenue", "$5B", "source", group="Northern region")
    other = replace(fact, value="$4B", group="Southern region")
    assert format_fact(fact) == "Northern region · Revenue: $5B"
    assert format_fact(other) == "Southern region · Revenue: $4B"
    assert format_fact(replace(fact, metric="View hours", value="24%", group="India")) == (
        "India · View hours: 24%"
    )


def test_existing_scope_labels_and_neutral_consolidation_are_not_repeated() -> None:
    fact = SummaryFact("Northern region revenue", "$5B", "source", group="Northern region")
    assert format_fact(fact) == "Northern region revenue: $5B"
    assert format_fact(replace(fact, metric="Revenue", group="Consolidated")) == "Revenue: $5B"


def test_a_growth_rate_without_a_level_reads_as_the_rate() -> None:
    fact = SummaryFact("Gross profit rate", "", "source", change="+96 bps", comparison="YoY")
    assert format_fact(fact) == "Gross profit rate: +96 bps YoY"


def test_long_or_repeated_basis_notes_stay_out_of_the_label() -> None:
    defined = SummaryFact(
        "Total contract value", "$2.9B", "s", basis="TCV as defined by the issuer"
    )
    assert format_fact(defined) == "Total contract value: $2.9B"
    repeated = SummaryFact("Adjusted revenue", "$13B", "s", basis="Adjusted (Non-GAAP)")
    assert format_fact(repeated) == "Adjusted revenue: $13B"
    assert format_fact(replace(repeated, metric="GAAP EPS", basis="non-GAAP")).startswith(
        "GAAP EPS (non-GAAP)"
    )


def test_the_card_groups_facts_by_period_and_kind() -> None:
    facts = [
        SummaryFact("Revenue", "$5B", "s", period="Q3 2026", change="+10%", comparison="YoY"),
        SummaryFact("Revenue outlook", "$21B", "s", kind="guide", period="FY2026"),
        SummaryFact(
            "Robotaxi program", "All Q3 milestones met", "s", kind="update", period="Q3 2026"
        ),
        SummaryFact("Operating income", "$1B", "s", period="Q3 2026"),
    ]
    assert discord_brief(facts) == (
        "**Q3 2026 · Results**\n"
        "🔹 **Revenue**: $5B; +10% YoY\n"
        "🔹 **Operating income**: $1B\n\n"
        "**FY2026 · Outlook**\n"
        "🔹 **Revenue outlook**: $21B\n\n"
        "**Business updates**\n"
        "🔸 **Robotaxi program**: All Q3 milestones met"
    )


@pytest.mark.parametrize(
    ("value", "unit", "currency", "expected"),
    [
        # Accounting negatives keep their currency and scale, with the sign in front
        ("(37.5)", "USD millions", "USD", "-$37.5M"),
        ("$(2,047) million", "$ million", "USD", "-$2.05B"),
        ("$-37.5", "USD millions", "USD", "-$37.5M"),
        ("-$7,300.4 million", "", "", "-$7.3B"),
        ("(18)% to (16)%", "percent", "", "-18%–-16%"),
        # A scaled amount shows at most four significant digits; ranges and per-share keep all
        ("$7,963.7 million", "USD millions", "USD", "$7.96B"),
        ("980.8", "USD millions", "USD", "$980.8M"),
        ("$1.234", "USD per share", "USD", "$1.234"),
        ("$2,365-$2,395 million", "USD millions", "USD", "$2.365B–$2.395B"),
        # Approximate amounts are formatted too
        ("Nearly $200 billion", "USD billions", "USD", "Nearly $200B"),
        ("more than 5,000 million", "USD millions", "USD", "more than $5B"),
    ],
)
def test_negative_rounded_and_approximate_amounts_read_cleanly(
    value: str, unit: str, currency: str, expected: str
) -> None:
    assert format_value(value, unit, currency) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3Q'26", "Q3 2026"),
        ("FQ1'27", "Q1 FY2027"),
        ("Q3 FY27", "Q3 FY2027"),
        ("FY'27", "FY2027"),
        ("third quarter 2026", "Q3 2026"),
        ("fourth quarter of fiscal 2026", "Q4 FY2026"),
        ("Q3 2026 (three months ended Sept 30, 2026)", "Q3 2026"),
        ("Q4 FY2026 (three months ending October 31, 2026)", "Q4 FY2026"),
    ],
)
def test_one_quarter_has_one_label(value: str, expected: str) -> None:
    assert format_period(value) == expected


def test_a_count_keeps_what_it_counts_and_a_margin_its_unusual_base() -> None:
    leasing = SummaryFact("Leasing completed", "5.3 million", "s", unit="million square feet")
    assert format_fact(leasing) == "Leasing completed: 5.3M square feet"
    margin = SummaryFact("Adjusted EBITDA margin", "3.6%", "s", unit="% of Gross Bookings")
    assert format_fact(margin) == "Adjusted EBITDA margin: 3.6% of Gross Bookings"
    assert format_fact(replace(margin, unit="% of revenue")) == "Adjusted EBITDA margin: 3.6%"


@pytest.mark.parametrize(
    ("basis", "group", "expected"),
    [
        ("U.S. GAAP, continuing operations", "", "Revenue (continuing operations): $5B"),
        ("GAAP balance sheet", "", "Revenue: $5B"),
        ("Reported", "", "Revenue: $5B"),
        ("Operating KPI", "", "Revenue: $5B"),
        ("constant currency", "", "Revenue (constant currency): $5B"),
        ("", "Consolidated (Nebius AI cloud, Avride and TripleTen)", "Revenue: $5B"),
    ],
)
def test_default_bases_and_consolidated_scopes_stay_off_the_label(
    basis: str, group: str, expected: str
) -> None:
    assert format_fact(SummaryFact("Revenue", "$5B", "s", basis=basis, group=group)) == expected


def test_a_level_gets_its_change_from_the_year_ago_figure() -> None:
    text = discord_brief(
        [
            SummaryFact("Revenue", "$755 million", "s", period="Q3 2026", prior="$687 million"),
            SummaryFact(
                "Adjusted EBITDA margin",
                "84%",
                "s",
                period="Q3 2026",
                prior="82%",
                comparison="YoY",
            ),
            SummaryFact(
                "Adjusted EBITDA",
                "(726.3)",
                "s",
                period="Q3 2026",
                prior="(717.7)",
                unit="USD millions",
                currency="USD",
            ),
            SummaryFact("Comparable sales growth", "5%", "s", period="Q3 2026", prior="3%"),
        ]
    )
    assert "🔹 **Revenue**: $755M; +9.9% YoY" in text
    assert "🔹 **Adjusted EBITDA margin**: 84%; +200 bps YoY" in text
    # No growth rate from a loss: the year-ago figure instead
    assert "🔹 **Adjusted EBITDA**: -$726.3M; vs -$717.7M a year ago" in text
    # A growth rate's own change isn't a margin change
    assert "🔹 **Comparable sales growth**: 5%\n" in f"{text}\n"


def test_headline_results_and_outlook_lead_the_brief() -> None:
    facts = [
        SummaryFact("Free cash flow", "$2B", "s", period="Q3 2026", role="supporting"),
        SummaryFact(
            "Revenue",
            "$15B",
            "s",
            period="Q3 2026",
            role="headline",
            change="+10%",
            comparison="YoY",
        ),
        SummaryFact("Cash", "$7B", "s", period="September 30, 2026", role="driver"),
        SummaryFact("Buybacks", "$889M", "s", period="Nine months ended September 30, 2026"),
        SummaryFact(
            "Robotaxi program", "All Q3 milestones met", "s", kind="update", period="Q3 2026"
        ),
        SummaryFact(
            "Revenue outlook", "$16B", "s", kind="guide", period="Q4 2026", role="guidance"
        ),
        SummaryFact(
            "Debt raise", "$600M notes due 2031", "s", kind="update", period="October 2026"
        ),
        SummaryFact("Adjusted EPS", "$0.88", "s", period="Q3 2026", role="headline"),
    ]
    assert discord_brief(facts) == (
        "**Q3 2026 · Results**\n"
        "🔹 **Revenue**: $15B; +10% YoY\n"
        "🔹 **Adjusted EPS**: $0.88\n\n"
        "**Q4 2026 · Outlook**\n"
        "🔹 **Revenue outlook**: $16B\n\n"
        "**Q3 2026 · More results**\n"
        "🔹 **Cash**: $7B\n"
        "🔹 **Free cash flow**: $2B\n\n"
        "**Nine months ended September 30, 2026 · Results**\n"
        "🔹 **Buybacks**: $889M\n\n"
        "**Business updates**\n"
        "🔸 **Robotaxi program**: All Q3 milestones met\n"
        "🔸 **October 2026 · Debt raise**: $600M notes due 2031"
    )


def test_a_quarter_written_out_joins_the_quarter() -> None:
    facts = [
        SummaryFact(
            "Revenue",
            "$980.8M",
            "s",
            period="Q3 2026 (three months ended Sep 30, 2026)",
            role="headline",
        ),
        SummaryFact(
            "Adjusted EBITDA", "$465.8M", "s", period="Three months ended September 30, 2026"
        ),
    ]
    assert discord_brief(facts) == (
        "**Q3 2026 · Results**\n🔹 **Revenue**: $980.8M\n🔹 **Adjusted EBITDA**: $465.8M"
    )


def test_segments_share_one_line_largest_first_with_their_growth() -> None:
    def segment(name: str, value: str, **extra: str) -> SummaryFact:
        return SummaryFact(
            f"{name} distributable earnings",
            value,
            "s",
            kind="segment",
            period="Q3 2026",
            group=name,
            unit="USD millions",
            currency="USD",
            role="driver",
            **extra,
        )

    facts = [
        SummaryFact(
            "Distributable earnings per share", "$0.64", "s", period="Q3 2026", role="headline"
        ),
        segment("Wealth Solutions", "511"),
        segment("Asset Management", "755", prior="687"),
        segment("Operating Businesses", "467"),
        SummaryFact(
            "Wealth Solutions distributable earnings growth",
            "",
            "s",
            period="Q3 2026",
            group="Wealth Solutions",
            change="+22%",
            comparison="YoY",
            role="driver",
        ),
    ]
    assert discord_brief(facts) == (
        "**Q3 2026 · Results**\n"
        "🔹 **Distributable earnings per share**: $0.64\n"
        "🔹 **Distributable earnings by segment**: Asset Management $755M, +9.9% YoY; "
        "Wealth Solutions $511M, +22% YoY; Operating Businesses $467M"
    )


def test_the_company_named_as_a_scope_on_most_lines_is_dropped() -> None:
    company = "Brookfield Corporation"
    facts = [
        SummaryFact("Revenue", "$20B", "s", period="Q3 2026", group=company, role="headline"),
        SummaryFact("Realized carried interest", "$222M", "s", period="Q3 2026", group=company),
        SummaryFact(
            "Fee-bearing capital", "$682B", "s", period="Q3 2026", group="Asset Management"
        ),
    ]
    text = discord_brief(facts)
    assert company not in text
    assert "🔹 **Asset Management · Fee-bearing capital**: $682B" in text
