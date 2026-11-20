"""Keep supported comparisons visible without guessing from the reporting quarter."""

import io
from dataclasses import replace

import pytest

from first_print.pipeline import Release
from first_print.recorder import Journal
from first_print.source_document import document_from_text
from first_print.summary_format import format_fact
from first_print.summary_policy import SummaryFact
from first_print.summary_validation import validate_facts
from first_print.summary_verify import verify_facts
from tests.fakes import FakePoster, FakeSummarizer
from tests.test_pipeline import IR_PAGE, fetcher, ir_sighting, uber
from tests.test_pipeline import release_texts as release_texts


def test_qualitative_growth_keeps_its_only_explicit_source_comparison() -> None:
    source = "AI Infrastructure revenue nearly doubled year over year."
    fact = SummaryFact("AI Infrastructure revenue", "nearly doubled", source, period="Q3 2026")
    [accepted], _ = validate_facts([fact], source)

    assert format_fact(accepted).endswith("nearly doubled YoY")
    sequential = source.replace("year over year", "quarter over quarter")
    [accepted], _ = validate_facts([replace(fact, source=sequential)], sequential)
    assert format_fact(accepted).endswith("nearly doubled QoQ")
    ambiguous = source + " Total revenue rose quarter over quarter."
    [accepted], _ = validate_facts([replace(fact, source=ambiguous)], ambiguous)
    assert not accepted.comparison


@pytest.mark.parametrize("change", ["+10.1 percentage points", "+10.1 pp", "+1010 bps"])
def test_margin_changes_bind_as_points_and_retain_yoy(change: str) -> None:
    source = "Gross margin reached 43.0%, up 10.1 percentage points year over year."
    doc = document_from_text("Example reports third quarter 2026 results.\n" + source)
    fact = SummaryFact("Gross margin", "43.0%", source, period="Q3 2026", change=change)
    accepted, _ = validate_facts([fact], doc.text, unquoted=True)
    [shown], dropped = verify_facts(accepted, doc)

    assert shown.change == change and shown.comparison == "YoY", dropped
    assert format_fact(shown).endswith("YoY")
    bad = replace(fact, change="+10.1%", comparison="YoY")
    [trimmed], _ = verify_facts([bad], doc)
    assert not trimmed.change


def test_a_fleet_group_that_already_contains_the_label_is_shown_once() -> None:
    fact = SummaryFact(
        "Middle East fleet",
        "approximately 600",
        "source",
        group="Middle East Robotaxi fleet",
        unit="vehicles",
    )
    assert format_fact(fact) == "Middle East Robotaxi fleet: approximately 600 vehicles"
    separate = replace(fact, group="Europe", metric="Middle East fleet")
    assert "Europe · Middle East fleet" in format_fact(separate)


def test_a_growth_rate_in_value_keeps_its_reference_without_a_duplicate_change() -> None:
    fact = SummaryFact("Overseas revenue growth", "+171.5%", "s", comparison="YoY")
    assert format_fact(fact) == "Overseas revenue growth: +171.5% YoY"
    assert "YoY" not in format_fact(replace(fact, comparison=""))
    assert format_fact(replace(fact, metric="Gross margin")) == "Gross margin: 171.5%"


def test_unaudited_is_implicit_but_adjusted_and_preliminary_labels_remain() -> None:
    fact = SummaryFact("Total revenue (unaudited)", "$321M", "s", basis="Unaudited")
    assert format_fact(fact) == "Total revenue: $321M"
    assert "Non-IFRS" in format_fact(replace(fact, metric="Non-IFRS adjusted loss"))
    assert "Preliminary" in format_fact(replace(fact, basis="Preliminary"))


async def test_footer_describes_checker_findings_without_claiming_missing_source_facts(
    release_texts: dict[str, str],
) -> None:
    release_texts[IR_PAGE] = "Example announces third quarter 2026 results."
    poster = FakePoster()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), FakeSummarizer())
    release.add(*ir_sighting())
    await release.finish()

    summary = next(text for kind, text in poster.calls if kind == "summary")
    assert "verification check flagged omitted facts or details" in summary
    assert "release didn't confirm" not in summary and "figure held back" not in summary
