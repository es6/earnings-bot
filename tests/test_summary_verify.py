"""Verified live summaries: each fact the live answer keeps, checked against the release."""

import json

from earnings_bot.source_document import document_from_text
from earnings_bot.summary_policy import SummaryFact
from earnings_bot.summary_verify import verify_facts
from tests.fakes import summarizer_answering

RELEASE = (
    "Acme reports third quarter 2026 results\n"
    "Revenue was $6.76 billion, up 13% year over year.\n"
    "Free cash flow was $1.2 billion.\n"
    "We are expanding our marketplace into Europe."
)


def fact(metric: str, value: str, source: str, **fields: str) -> SummaryFact:
    return SummaryFact(metric=metric, value=value, source=source, period="Q3 2026", **fields)


REVENUE = fact("Revenue", "$6.76 billion", "Revenue was $6.76 billion, up 13% year over year.")


def test_a_fact_the_release_confirms_is_shown_and_a_mislabeled_one_is_not() -> None:
    document = document_from_text(RELEASE)
    mislabeled = fact("Operating income", "$6.76 billion", REVENUE.source)

    shown, dropped = verify_facts([REVENUE, mislabeled], document)

    assert shown == [REVENUE]
    assert dropped and dropped[0].startswith("Operating income:")


def test_a_growth_rate_the_release_doesnt_state_is_dropped_from_its_fact() -> None:
    document = document_from_text(RELEASE)
    stated = fact("Revenue", "$6.76 billion", REVENUE.source, change="+13%", comparison="YoY")
    computed = fact(
        "Free cash flow", "$1.2 billion", "Free cash flow was $1.2 billion.", change="+9%"
    )

    shown, dropped = verify_facts([stated, computed], document)

    assert shown[0].change == "+13%"
    assert shown[1].metric == "Free cash flow" and shown[1].change == ""
    assert any(d.startswith("Free cash flow growth:") for d in dropped)


def test_a_fact_with_no_number_keeps_its_quoted_words() -> None:
    document = document_from_text(RELEASE)
    update = SummaryFact(
        metric="Expansion",
        value="Expanding the marketplace into Europe",
        source="We are expanding our marketplace into Europe.",
        kind="update",
    )

    shown, _ = verify_facts([update], document)

    assert shown == [update]


async def test_the_live_writer_checks_its_facts_only_when_verified_summaries_are_on() -> None:
    lines = [
        {
            "metric": "Revenue",
            "value": "$6.76 billion",
            "period": "Q3 2026",
            "source": "Revenue was $6.76 billion, up 13% year over year.",
            "kind": "actual",
        },
        {
            "metric": "Operating income",
            "value": "$6.76 billion",
            "period": "Q3 2026",
            "source": "Revenue was $6.76 billion, up 13% year over year.",
            "kind": "actual",
        },
    ]
    answer = json.dumps({"lines": lines})

    plain, _ = summarizer_answering("end_turn", answer)
    checked, _ = summarizer_answering("end_turn", answer)
    checked.verified = True
    unverified = await plain.write("ACME", RELEASE)
    verified = await checked.write("ACME", RELEASE)

    assert "Operating income" in unverified.text
    assert "Operating income" not in verified.text and "Revenue" in verified.text
    assert any(reason.startswith("Operating income:") for reason in verified.unverified)


def test_a_live_value_is_read_as_one_number_or_a_range_as_written() -> None:
    from earnings_bot.summary_verify import parse

    assert parse("+5.9% reported, +5.1% cc")[:2] == ("5.9", None)  # type: ignore[index]
    assert parse("$4.8 - $5.0 billion")[:3] == ("4.8", "5.0", "billion")  # type: ignore[index]
    assert parse("-3.0 to -1.0")[:2] == ("-3.0", "-1.0")  # type: ignore[index]
    assert parse("(3.0) to (1.0)%")[:2] == ("-3.0", "-1.0")  # type: ignore[index]
    assert parse("~$12.0B vs $12.5B previously") == ("12.0", None, "billion", "approximately")


def test_a_fact_with_several_values_is_dropped_as_only_one_could_be_checked() -> None:
    document = document_from_text(RELEASE)
    combined = fact(
        "Revenue and free cash flow",
        "Revenue $6.76 billion; free cash flow $1.9 billion",
        REVENUE.source,
    )

    shown, dropped = verify_facts([combined], document)

    assert shown == [] and dropped[0].endswith("several values in one fact")


GUIDANCE = (
    "Acme reports third quarter fiscal 2027 results\n"
    "Acme's outlook for the fourth quarter of fiscal 2027 is as follows:\n"
    "Revenue is expected to be $108.0 billion, plus or minus 2%.\n"
    "Gross margins are expected to be 74.0%, plus or minus 50 basis points."
)


def test_a_guided_value_with_a_tolerance_is_one_value() -> None:
    document = document_from_text(GUIDANCE)
    passage = "Revenue is expected to be $108.0 billion, plus or minus 2%."
    revenue = SummaryFact(
        metric="Revenue outlook",
        value="$108.0 billion, plus or minus 2%",
        source=passage,
        period="Q4 FY2027",
        kind="guide",
        role="guidance",
    )
    margin = SummaryFact(
        metric="Gross margin outlook",
        value="74.0% ± 50 bps",
        source="Gross margins are expected to be 74.0%, plus or minus 50 basis points.",
        period="Q4 FY2027",
        kind="guide",
        role="guidance",
    )

    shown, dropped = verify_facts([revenue, margin], document)

    assert shown == [revenue, margin], dropped


def _table_release(caption: str) -> str:
    return (
        "<p>Acme reports third quarter fiscal 2027 results</p>"
        f"<table><tr><td>{caption}</td><td>Q3 FY27</td><td>Q3 FY26</td><td>Y/Y</td></tr>"
        "<tr><td>Revenue</td><td>$96,221</td><td>$46,743</td><td>106 %</td></tr>"
        "<tr><td>Operating income</td><td>$63,734</td><td>$28,440</td><td>124 %</td></tr>"
        "</table>"
    )


def _unscaled_revenue() -> SummaryFact:
    return SummaryFact(
        metric="Revenue",
        value="$96,221",
        source="Revenue $96,221 $46,743 106 %",
        period="Q3 FY2027",
        kind="actual",
        role="headline",
        change="+106%",
        comparison="YoY",
    )


def test_money_copied_from_a_table_without_its_scale_takes_only_a_scale_the_table_states() -> None:
    from earnings_bot.source_document import document_from_html

    scaled = document_from_html(_table_release("(In millions)"))
    thousands = document_from_html(_table_release("(In thousands)"))
    unstated = document_from_html(_table_release(""))

    shown, dropped = verify_facts([_unscaled_revenue()], scaled)
    smaller, _ = verify_facts([_unscaled_revenue()], thousands)
    as_written, _ = verify_facts([_unscaled_revenue()], unstated)

    assert [f.value for f in shown] == ["$96,221 million"], dropped
    assert shown[0].change == "+106%"
    assert [f.value for f in smaller] == ["$96,221 thousand"]
    assert [f.value for f in as_written] == ["$96,221"]
