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
