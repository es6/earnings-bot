import pytest

from earnings_bot.summary_policy import DEFAULT_COMPARISON, EMBER
from earnings_bot.summary_request import structured_request

NOW = "ServiceNow Q3 2026 revenue was $4.11B."


def test_shared_request_is_ember_only_with_schema_and_no_word_cap() -> None:
    request = structured_request("NOW", NOW)
    assert DEFAULT_COMPARISON == (EMBER,)
    assert request["model"] == EMBER.model
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert request["max_tokens"] == 16000
    assert "no editorial word or line limit" in request["system"]
    assert NOW in request["messages"][0]["content"]


@pytest.mark.parametrize("release", ["", " " * 30, "x" * 200001])
def test_no_silent_input_truncation(release: str) -> None:
    with pytest.raises(ValueError):
        structured_request("NOW", release)
