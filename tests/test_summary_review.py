import json

import pytest

from first_print.source_document import document_from_text
from first_print.summary_binding import BindingReport, Rejection
from first_print.summary_request import referenced_request
from first_print.summary_review import (
    BRIEF_EXAMPLES,
    headline_repair_request,
    review_variant,
)


def request():
    return referenced_request("TEST", document_from_text("Revenue was $10 billion in Q3 2026."))


def test_request_variants_preserve_default_and_add_only_opted_in_review() -> None:
    baseline = request()
    frozen = json.dumps(baseline, sort_keys=True)
    assert review_variant(baseline) == baseline
    varied = review_variant(baseline, examples=True, selection_check=True)
    data = json.loads(varied["messages"][0]["content"])
    assert len(data["issuer_context"]["owner_reviewed_brief_examples"]) == 2
    assert (
        data["source_document"] == json.loads(baseline["messages"][0]["content"])["source_document"]
    )
    assert "check selection internally" in varied["system"]
    assert "They cannot support a claim" in varied["system"]
    assert review_variant(baseline) == baseline  # caller-owned nested request wasn't mutated
    assert json.dumps(baseline, sort_keys=True) == frozen
    assert {example["ticker"] for example in BRIEF_EXAMPLES} == {"FICO", "UBER"}


@pytest.mark.parametrize("role", ["headline", "guidance", "supporting"])
async def test_repair_is_triggered_by_code_rejection_of_a_headline_or_guide(role: str) -> None:
    baseline = request()
    answer = json.dumps({"facts": [{"metric": "Revenue", "role": role}]})
    report = BindingReport((), (Rejection("Revenue", "current_actual", ("wrong quarter",)),))
    repair = headline_repair_request(baseline, answer, report)
    if role == "supporting":
        assert repair is None
    else:
        assert repair is not None
        assert len(repair["messages"]) == 3 and len(baseline["messages"]) == 1
        assert "wrong quarter" in repair["messages"][-1]["content"]
        assert repair["messages"][1] == {"role": "assistant", "content": answer}
        assert repair["output_config"] == baseline["output_config"]


@pytest.mark.parametrize("answer", ["not JSON", "{}", "[]", '{"facts":null}'])
def test_malformed_answers_do_not_blindly_start_another_paid_request(answer: str) -> None:
    assert headline_repair_request(request(), answer, BindingReport((), ())) is None


def test_an_accepted_headline_needs_no_repair_request() -> None:
    answer = json.dumps({"facts": [{"metric": "Revenue", "role": "headline"}]})
    assert headline_repair_request(request(), answer, BindingReport((), ())) is None
