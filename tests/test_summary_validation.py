"""Fixed primary-source failures, with controls that keep valid facts available."""

from dataclasses import replace

import pytest

from earnings_bot.summary_policy import SummaryFact
from earnings_bot.summary_validation import validate_fact, validate_facts

# Source: ServiceNow Q3 2026 earnings release, SEC accession0001373715-26-000092.
NOW = """ServiceNow Q3 2026
(in millions)
| Three Months Ended | Nine Months Ended
| September 30, 2026 | September 30, 2025 | September 30, 2026 | September 30, 2025
Non-GAAP free cash flow | $ | 822 | $ | 592 | $ | 3,121 | $ | 2,604
Non-GAAP free cash flow margin | 20 | % | 17.5 | % | 26.5 | % | 27 | %"""
NOW_ROW = NOW.splitlines()[-2]
# Source: Synopsys Q3 FY2026 release furnished with accession0001193125-26-368620.
SNPS = """Synopsys Q3 FY2026
CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS
(in thousands)
| Nine Months Ended
| July 31, 2026 | July 31, 2025
Net cash provided by operating activities | 2,298,603 | 878,870"""


def cash() -> SummaryFact:
    return SummaryFact(
        "Non-GAAP free cash flow",
        "$822 million",
        NOW_ROW,
        period="Q3 2026",
        unit="USD million",
        currency="USD",
        basis="non-GAAP",
        prior="$592 million",
        change="+38.9%",
        comparison="YoY",
    )


def test_real_margin_is_not_growth_and_valid_arithmetic_survives() -> None:
    assert validate_fact(cash(), NOW) == []
    bad = replace(cash(), change="+20%")
    assert any("arithmetic" in issue for issue in validate_fact(bad, NOW))


def test_margin_as_growth_fails_without_prior_or_arithmetic() -> None:
    source = "Free cash flow | $ | 822 | 20 | %"
    release = "(in millions)\nQ3 2026\nAmount | Margin\n" + source
    bad = replace(cash(), source=source, prior="", change="+20%")
    assert any("growth" in issue for issue in validate_fact(bad, release))
    margin = replace(bad, metric="Free cash flow margin", value="20%", change="", unit="%")
    assert validate_fact(margin, release) == []


def test_table_percentages_ignore_the_money_scale_and_cash_burn_keeps_sign() -> None:
    source = "Operating margin | 22.5 | % | 20.3 | %"
    release = "(in millions)\n" + source
    margin = SummaryFact("Operating margin", "22.5%", source, period="Q3 2026", unit="%")
    assert validate_fact(margin, release) == []
    source = "Net cash used in operating activities | (10) | 20"
    release = "(in millions)\n| Three Months Ended\n" + source
    cash_burn = SummaryFact(
        "Operating cash flow", "-$10 million", source, period="Q3 2026", unit="USD million"
    )
    assert validate_fact(cash_burn, release) == []
    assert validate_fact(replace(cash_burn, value="$10 million"), release)


def test_real_ytd_cash_flow_cannot_be_full_year_or_quarter() -> None:
    fact = SummaryFact(
        "Operating cash flow",
        "$2.298603 billion",
        SNPS.splitlines()[-1],
        period="Nine months ended July 31, 2026",
        unit="USD billion",
        currency="USD",
    )
    assert validate_fact(fact, SNPS) == []
    for period in ["FY2026", "Q3 FY2026"]:
        assert any("duration" in x for x in validate_fact(replace(fact, period=period), SNPS))


def test_combined_table_ytd_column_is_not_quarter() -> None:
    fact = replace(cash(), value="$3,121 million", prior="", change="", comparison="")
    assert any("duration" in issue for issue in validate_fact(fact, NOW))
    assert validate_fact(replace(fact, period="Nine months ended September 30, 2026"), NOW) == []


def test_numbers_elsewhere_in_release_do_not_validate_a_quote() -> None:
    fact = replace(cash(), value="$839 million", prior="", change="", comparison="")
    assert any("source passage" in x for x in validate_fact(fact, NOW + "\nOperating cash flow839"))


def test_count_currency_and_empty_display_fields_are_rejected() -> None:
    source = "Adobe achieved 1 billion monthly active users."
    fact = SummaryFact(
        "Monthly active users",
        "1 billion",
        source,
        period="Q3 FY2026",
        unit="users",
        currency="USD",
    )
    assert any("count" in issue for issue in validate_fact(fact, source))
    assert validate_fact(replace(fact, currency=""), source) == []
    assert any(
        "empty" in issue for issue in validate_fact(replace(fact, value="", change=""), source)
    )


def test_unknown_quote_period_units_and_reference_are_not_repaired() -> None:
    valid = cash()
    mutations = [
        replace(valid, source="invented"),
        replace(valid, period=""),
        replace(valid, unit="%"),
        replace(valid, unit="$822 million"),
        replace(valid, period="Q3 2026 and FY2026"),
        replace(valid, comparison=""),
    ]
    for fact in mutations:
        assert validate_fact(fact, NOW)
    accepted, rejected = validate_facts([valid, *mutations], NOW)
    # Only the unlabeled change is optional: its sourced value stays, without the change
    assert accepted == [valid, replace(valid, prior="", change="", comparison="")]
    assert len(rejected) == len(mutations)


def test_model_quoting_styles_still_bind_to_the_release() -> None:
    release = (
        "Net income was $15.3 billion, up 6%.\n"
        "(in millions)\n"
        "Adjusted Revenue | $ | 13,285 | $ | 11,305 | 18 | %\n"
        "Total orders of $19.0B, +49%"
    )
    joined = SummaryFact(
        "Net income",
        "$15.3 billion",
        '"Net income was $15.3 billion, up 6%." "Adjusted Revenue"',
        period="Q3 2026",
    )
    assert validate_fact(joined, release) == []
    squeezed = SummaryFact(
        "Adjusted revenue",
        "$13,285 million",
        "Adjusted Revenue | $13,285 | $11,305 | 18 %",
        period="Q3 2026",
        unit="USD million",
    )
    assert validate_fact(squeezed, release) == []
    signed = SummaryFact(
        "Total orders",
        "$19.0B",
        "Total orders of $19.0B, +49%",
        period="Q3 2026",
        change="+49%",
        comparison="YoY",
    )
    assert validate_fact(signed, release) == []
    invented = replace(joined, source='"Net income was $15.3 billion, up 6%." "made up"')
    assert validate_fact(invented, release) == ["source passage is empty or not exact"]


def test_same_quarter_adjustment_reference_and_exclusion_scope() -> None:
    source = "Net interest income was $27.1 billion, up 13%."
    fact = SummaryFact(
        "Net interest income",
        "$27.1 billion",
        source,
        period="Q3 2026",
        unit="USD billion",
        basis="managed, excluding Markets",
    )
    assert any("exclusion" in x for x in validate_fact(fact, source))
    fact = replace(cash(), change="+$0.18 versus GAAP", comparison="YoY")
    assert any("adjustment" in x for x in validate_fact(fact, NOW))


def test_per_share_figures_keep_their_units_in_a_millions_table() -> None:
    release = "(in millions, except per-share amounts)\nAdjusted EPS | $ | 2.25 | $ | 1.66"
    eps = SummaryFact(
        "Adjusted EPS", "$2.25", "Adjusted EPS | $ | 2.25 | $ | 1.66", period="Q3 2026"
    )
    assert validate_fact(eps, release) == []


def test_an_exclusion_is_supported_by_its_word_in_the_quote() -> None:
    source = "Comp sales ex. fuel increased 4.6%"
    comps = SummaryFact(
        "Comparable sales", "4.6%", source, period="Q2 FY27", basis="excluding fuel", unit="%"
    )
    assert validate_fact(comps, source) == []
    assert any(
        "exclusion" in issue
        for issue in validate_fact(replace(comps, basis="excluding currency"), source)
    )


def test_a_range_scale_written_once_covers_both_ends() -> None:
    source = "Revenue of between $2.649 - $2.653 billion."
    guide = SummaryFact(
        "Revenue guidance",
        "$2.649 - $2.653 billion",
        source,
        kind="guide",
        period="Q4 2026",
        unit="USD billion",
    )
    assert validate_fact(guide, source) == []


def test_a_yoy_label_comes_only_from_the_quoted_wording() -> None:
    source = "Net interest income was $27.1 billion, up 13% year-over-year."
    fact = SummaryFact(
        "Net interest income", "$27.1 billion", source, period="Q3 2026", change="+13%"
    )
    [kept], _ = validate_facts([fact], source)
    assert (kept.change, kept.comparison) == ("+13%", "YoY")
    both = "Revenue grew 96% year-over-year and 20% quarter-over-quarter to $2.3 billion"
    unclear = SummaryFact("Revenue", "$2.3 billion", both, period="Q3 2026", change="+96%")
    [kept], _ = validate_facts([unclear], both)
    assert (kept.change, kept.comparison) == ("", "")  # left unlabeled, so the change is dropped


@pytest.mark.parametrize(
    "caption",
    [
        "In USD $ millions | Three months ended September 30",  # Nebius Q3 2026
        "(US$ millions, except per share amounts)",  # Brookfield Q3 2026
        "In USD millions | Three months ended September 30,",
        "(Millions of dollars, except per share amounts)",
        "(Dollars in millions)",
        "(in millions of U.S. dollars)",
    ],
)
def test_a_table_caption_sets_the_scale_however_it_names_the_currency(caption: str) -> None:
    source = "Revenues | 146.1 | 980.8 | 571 | %"
    release = f"Consolidated results\n{caption}\n| 2025 | 2026 | Change\n{source}"
    revenue = SummaryFact(
        "Revenue",
        "980.8",
        source,
        period="Q3 2026",
        unit="$ millions",
        currency="USD",
        prior="146.1",
        change="+571%",
        comparison="YoY",
    )
    assert validate_fact(revenue, release) == []
    # The scale comes from the caption, never from the fact's own claim
    assert validate_fact(replace(revenue, unit="$ billions"), release) != []


def test_prose_about_millions_is_not_a_table_caption() -> None:
    source = "Units shipped | 146.1 | 980.8"
    release = "We serve millions of customers in 40 markets.\n| 2025 | 2026\n" + source
    units = SummaryFact("Units shipped", "980.8", source, period="Q3 2026", unit="thousand units")
    assert validate_fact(replace(units, unit="$ millions", currency="USD"), release) != []
