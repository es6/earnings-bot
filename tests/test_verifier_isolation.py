"""One fact the verifier can't read must cost only that fact, not the whole summary."""

from first_print import summary_verify
from first_print.source_document import document_from_text
from first_print.summary_policy import SummaryFact
from first_print.summary_verify import verify_facts

RELEASE = (
    "Revenue was $3.67 billion for fiscal 2026. Net income was $142.1 million for fiscal 2026."
)


def test_a_fact_that_breaks_the_verifier_is_refused_alone(monkeypatch):
    document = document_from_text(RELEASE)
    good = SummaryFact(
        metric="Net income",
        value="$142.1M",
        source="Net income was $142.1 million",
        period="FY2026",
        currency="USD",
    )
    bad = SummaryFact(
        metric="Revenue",
        value="$3.67B",
        source="Revenue was $3.67 billion",
        period="FY2026",
        currency="USD",
    )
    real = summary_verify.check

    def check(fact, doc):
        if fact.metric == "Revenue":
            raise IndexError("list index out of range")  # as a metric named "Total" did
        return real(fact, doc)

    monkeypatch.setattr(summary_verify, "check", check)
    shown, dropped = verify_facts([bad, good], document)
    assert [fact.metric for fact in shown] == ["Net income"]
    assert dropped == ["Revenue: the verifier failed on it (IndexError)"]
