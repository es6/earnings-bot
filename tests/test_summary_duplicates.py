"""Duplicate removal preserves each verified measure, basis, period and comparison."""

from dataclasses import replace

import pytest

from first_print.summary_format import discord_brief
from first_print.summary_policy import SummaryFact


@pytest.mark.parametrize("metric", ["System sales", "Quarterly dividend", "Paid subscribers"])
def test_identical_levels_keep_the_richer_comparison_once(metric: str) -> None:
    fact = SummaryFact(metric, "600", "exact source", period="Q3 2026", unit="units")
    richer = replace(fact, change="+10%", comparison="QoQ")
    card = discord_brief([fact, richer])
    assert card.count(f"**{metric}**") == 1 and "+10% QoQ" in card
    # No coalescing across a period, taxonomy, currency or conflicting comparison.
    for distinct in (
        replace(richer, period="Q2 2026"),
        replace(richer, group="Overseas"),
        replace(richer, currency="EUR"),
        replace(richer, basis="Constant currency"),
    ):
        assert discord_brief([fact, distinct]).count("🔹") == 2
    conflicting = replace(richer, change="+11%")
    assert discord_brief([richer, conflicting]).count(f"**{metric}**") == 2


def test_separate_level_and_growth_combine_without_treating_distinct_bases_as_duplicates() -> None:
    fact = SummaryFact("Revenue", "$600M", "source", period="Q3 2026", currency="USD")
    growth = SummaryFact("Revenue growth", "+10%", "source", period="Q3 2026", comparison="YoY")
    card = discord_brief([fact, growth])
    assert card.count("🔹") == 1 and "$600M; +10% YoY" in card
    for distinct in (
        replace(growth, basis="Constant currency"),
        replace(growth, metric="Organic revenue growth"),
        replace(growth, group="Digital"),
        replace(growth, period="Q2 2026"),
    ):
        assert discord_brief([fact, distinct]).count("🔹") == 2


def test_duplicate_reported_labels_do_not_erase_a_separate_adjusted_measure() -> None:
    eps = SummaryFact("Diluted EPS", "$0.69", "source", period="Q3 2026", basis="GAAP")
    card = discord_brief(
        [
            eps,
            replace(eps, basis="reported"),
            replace(eps, metric="Adjusted EPS", value="$0.73", basis="Non-GAAP"),
        ]
    )
    assert card.count("🔹") == 2 and "$0.69" in card and "$0.73" in card
    # Two adjusted definitions with different exclusions remain separate.
    adjusted = replace(eps, metric="Adjusted EPS", basis="Non-GAAP; excluding fuel")
    assert (
        discord_brief([adjusted, replace(adjusted, basis="Non-GAAP; excluding FX")]).count("🔹")
        == 2
    )


def test_a_narrative_duplicate_cannot_drop_a_different_constraint_or_timing() -> None:
    update = SummaryFact(
        "Plant expansion", "Capacity doubles in 2027", "source", kind="update", period="2027"
    )
    card = discord_brief(
        [update, update, replace(update, value="Capacity doubles after permits arrive")]
    )
    assert card.count("🔸") == 2 and "after permits arrive" in card


def test_segment_combination_keeps_distinct_bases_and_conflicting_values_visible() -> None:
    first = SummaryFact("Revenue", "$12M", "source", kind="segment", period="Q3 2026", group="West")
    second = replace(first, group="East", value="$22M")
    adjusted = replace(first, value="$14M", basis="Non-GAAP")
    card = discord_brief([first, second, adjusted])
    assert all(value in card for value in ("$12M", "$22M", "$14M")) and "Non-GAAP" in card
    conflict = replace(first, value="$13M")
    card = discord_brief([first, second, conflict])
    assert all(value in card for value in ("$12M", "$22M", "$13M"))


def test_combined_segment_line_keeps_its_common_comparison_basis() -> None:
    fact = SummaryFact(
        "Revenue",
        "$12M",
        "source",
        kind="segment",
        period="Q3 2026",
        group="West",
        basis="Constant currency",
    )
    card = discord_brief([fact, replace(fact, group="East", value="$22M")])
    assert "Constant currency" in card and "$12M" in card and "$22M" in card
