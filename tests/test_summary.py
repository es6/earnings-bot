from types import SimpleNamespace
from typing import Any, cast

import pytest

from earnings_bot.estimates import Estimates
from earnings_bot.summary import MODEL, Summarizer, SummaryError, unverified_numbers

RELEASE = """\
Uber Announces Results for Second Quarter 2026
Gross Bookings grew 24% year-over-year to $58.0 billion
Revenue | $ 12,651 | $ 14,211
Diluted EPS | $ 0.63 | $ 1.17
Trips | 3,350 | 3,900
Adjusted EBITDA margin was 4.8% of Gross Bookings"""


def test_numbers_from_the_release_check_out() -> None:
    post = """\
UBER $UBER Q2'26 EARNINGS HIGHLIGHTS

🔹 Revenue: $14.21B (Est. $14.05B) 🟢; +12% YoY
🔹 GAAP EPS: $1.17
🔹 Gross Bookings: $58.0B; +24% YoY
🔹 Trips: 3.9B
🔹 Adj. EBITDA margin: 4.8% of Gross Bookings"""

    # +12% isn't stated in the release, so it's the one number to check
    assert unverified_numbers(post, RELEASE, Estimates(eps=0.83, revenue=14.05e9)) == ["12%"]


def test_made_up_numbers_are_flagged() -> None:
    post = "UBER $UBER Q2'26 EARNINGS HIGHLIGHTS\n\n🔹 Revenue: $15.80B\n🔹 FY26 Guide: $61.2B"

    assert unverified_numbers(post, RELEASE, None) == ["15.80B", "61.2B"]


def test_labels_and_small_counts_are_ignored() -> None:
    post = "HEADER\n\nQ3 Guide:\n🔹 FY26 revenue growth in 2 segments"

    assert unverified_numbers(post, RELEASE, None) == []


class FakeStream:
    def __init__(self, message: SimpleNamespace) -> None:
        self.message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def get_final_message(self) -> SimpleNamespace:
        return self.message


def summarizer_answering(stop_reason: str, *texts: str) -> tuple[Summarizer, list[dict[str, Any]]]:
    """A Summarizer whose Claude answers with these text blocks, and the requests it gets."""
    asked: list[dict[str, Any]] = []
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking="")]
        + [SimpleNamespace(type="text", text=text) for text in texts],
        usage=SimpleNamespace(input_tokens=9000, output_tokens=700),
    )

    def stream(**request: Any) -> FakeStream:
        asked.append(request)
        return FakeStream(message)

    summarizer = Summarizer("not-a-real-key")
    summarizer.client = cast(
        Any, SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    )
    return summarizer, asked


async def test_the_summary_is_claudes_text_with_its_numbers_checked() -> None:
    post = "UBER $UBER Q2'26 EARNINGS HIGHLIGHTS\n\n🔹 Revenue: $14.21B; +12% YoY"
    summarizer, asked = summarizer_answering("end_turn", post)

    summary = await summarizer.write("UBER", RELEASE, Estimates(eps=0.83, revenue=14.05e9))

    assert summary.text == post
    assert summary.unverified == ["12%"]
    assert (summary.input_tokens, summary.output_tokens) == (9000, 700)
    [request] = asked
    assert request["model"] == MODEL
    assert request["fallbacks"] == "default"
    assert "Revenue: $14.05B" in request["messages"][0]["content"]
    assert RELEASE in request["messages"][0]["content"]


@pytest.mark.parametrize(("stop_reason", "texts"), [("refusal", ()), ("max_tokens", ("  ",))])
async def test_a_refusal_or_an_empty_answer_is_an_error(
    stop_reason: str, texts: tuple[str, ...]
) -> None:
    summarizer, _ = summarizer_answering(stop_reason, *texts)

    with pytest.raises(SummaryError):
        await summarizer.write("UBER", RELEASE, None)
