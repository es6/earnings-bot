import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from earnings_bot.config import Settings
from earnings_bot.summary import (
    META_API,
    MODEL,
    Summarizer,
    SummaryError,
    summarizer_from,
)

RELEASE = """\
Uber Announces Results for Third Quarter 2026
Gross Bookings grew 20% year-over-year to $59.8 billion.
Approximately 700 fully driverless vehicles are expected on Uber by year-end.
(in millions)
| Three Months Ended September 30,
| 2025 | 2026
Revenue | $ | 13,467 | $ | 14,766"""


def fact(**fields: str) -> dict[str, str]:
    base = dict.fromkeys(
        [
            "metric",
            "value",
            "change",
            "prior",
            "source",
            "kind",
            "period",
            "basis",
            "unit",
            "currency",
            "group",
            "role",
            "relevance",
            "comparison",
        ],
        "",
    )
    return base | {"kind": "actual"} | fields


BOOKINGS = fact(
    metric="Gross Bookings",
    value="$59.8 billion",
    change="+20%",
    comparison="YoY",
    period="Q3 2026",
    source="Gross Bookings grew 20% year-over-year to $59.8 billion.",
)
AUTONOMY = fact(
    metric="Autonomous vehicles",
    value="About 700 fully driverless vehicles on Uber by year-end",
    kind="update",
    period="Q3 2026",
    source="Approximately 700 fully driverless vehicles are expected on Uber by year-end.",
)
INVENTED = fact(
    metric="Revenue", value="$15,800 million", period="Q3 2026", source="Revenue | $ | 13,467"
)


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
    """A Summarizer whose model answers with these text blocks, and the requests it gets."""
    asked: list[dict[str, Any]] = []
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="redacted_thinking")]
        + [SimpleNamespace(type="text", text=text) for text in texts],
        usage=SimpleNamespace(input_tokens=9000, output_tokens=700),
    )

    def stream(**request: Any) -> FakeStream:
        asked.append(request)
        return FakeStream(message)

    summarizer = Summarizer("not-a-real-key")
    summarizer.client = cast(Any, SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return summarizer, asked


def answer(*facts: dict[str, str]) -> str:
    return json.dumps({"lines": list(facts)})


async def test_confirmed_facts_are_laid_out_and_unconfirmed_ones_held_back() -> None:
    summarizer, asked = summarizer_answering("end_turn", answer(BOOKINGS, INVENTED, AUTONOMY))

    summary = await summarizer.write("UBER", RELEASE)

    assert summary.text == (
        "**Q3 2026 · Results**\n"
        "🔹 **Gross Bookings**: $59.8B; +20% YoY\n\n"
        "**Business updates**\n"
        "🔸 **Autonomous vehicles**: About 700 fully driverless vehicles on Uber by year-end"
    )
    assert len(summary.unverified) == 1
    assert summary.unverified[0].startswith("Revenue:")
    assert (summary.input_tokens, summary.output_tokens) == (9000, 700)
    [request] = asked
    assert request["model"] == MODEL == "muse-ember-1.0"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert RELEASE in request["messages"][0]["content"]


@pytest.mark.parametrize(
    ("stop_reason", "texts"),
    [
        ("refusal", ()),
        ("max_tokens", ('{"lines": [',)),
        ("end_turn", ("not json",)),
        ("end_turn", (answer(INVENTED),)),  # nothing the release confirms
    ],
)
async def test_a_refusal_a_cut_off_or_an_unconfirmed_answer_is_an_error(
    stop_reason: str, texts: tuple[str, ...]
) -> None:
    summarizer, _ = summarizer_answering(stop_reason, *texts)

    with pytest.raises(SummaryError):
        await summarizer.write("UBER", RELEASE)


def test_summaries_need_the_switch_and_the_key() -> None:
    def settings(**values: object) -> Settings:
        return Settings.model_validate({"discord_token": "x", **values})

    assert summarizer_from(settings()) is None
    assert summarizer_from(settings(meta_model_api_key="key")) is None  # a key alone: off
    assert summarizer_from(settings(live_summaries=True)) is None
    summarizer = summarizer_from(settings(live_summaries=True, meta_model_api_key="key"))
    assert summarizer is not None
    assert str(summarizer.client.base_url).rstrip("/") == META_API
    assert SecretStr("key").get_secret_value() == summarizer.client.api_key
