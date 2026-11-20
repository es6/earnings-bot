"""Every model answer remains replayable after validation, failure and restart."""

import hashlib
import io
import json
from pathlib import Path

import pytest

from first_print.delivery import FAILED, DeliveryStore
from first_print.pipeline import Release
from first_print.recorder import Journal
from first_print.source_document import document_from_html
from first_print.summary import SummaryError
from tests.fakes import FakePoster, summarizer_answering
from tests.test_pipeline import IR_PAGE, fetcher, ir_sighting, uber
from tests.test_pipeline import release_texts as release_texts
from tests.test_summary import BOOKINGS, INVENTED, RELEASE, answer


async def test_default_fact_writer_keeps_the_exact_answer_and_document_without_credentials() -> (
    None
):
    raw = answer(BOOKINGS, INVENTED)
    writer, asked = summarizer_answering("end_turn", raw)
    doc = document_from_html("<p>" + RELEASE.replace("\n", "</p><p>") + "</p>")

    result = await writer.write_document("UBER", doc)

    assert result.evidence is not None
    evidence = json.loads(result.evidence)
    assert evidence["version"] == "fact-summary/1"
    assert evidence["raw_answer"] == raw and evidence["document_sha256"] == doc.sha256
    request = json.dumps(asked[0], ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert evidence["request_sha256"] == hashlib.sha256(request).hexdigest()
    assert evidence["accepted"] and evidence["rejected"]
    assert "not-a-real-key" not in result.evidence and "api_key" not in evidence
    assert result.validation == "quote-check/1"


@pytest.mark.parametrize(
    ("stop", "raw"),
    [("end_turn", "not JSON"), ("max_tokens", answer(BOOKINGS)), ("end_turn", answer(INVENTED))],
)
async def test_failed_answers_remain_addressable_after_reopening_the_store(
    tmp_path: Path, stop: str, raw: str
) -> None:
    writer, _ = summarizer_answering(stop, raw)
    doc = document_from_html("<p>" + RELEASE.replace("\n", "</p><p>") + "</p>")
    with pytest.raises(SummaryError) as failed:
        await writer.write_document("UBER", doc)
    evidence = failed.value.evidence
    assert evidence is not None and json.loads(evidence)["raw_answer"] == raw
    assert json.loads(evidence)["document_sha256"] == doc.sha256
    path = tmp_path / "capture.db"
    with DeliveryStore(path, 1) as store:
        store.summary_started("release", doc.sha256, "IR", "2026-11-20")
        store.summary_status("release", FAILED, evidence=evidence)
    with DeliveryStore(path, 1) as store:
        job = store.summary("release")
        assert job is not None and job.status == FAILED and job.evidence == evidence
        # A later attempt does not overwrite the previous document's answer.
        store.summary_started("release", "new-document", "wire", "2026-11-20")
        assert store.bound_summary_evidence("release", doc.sha256) == evidence


async def test_pipeline_persists_a_failed_candidate_before_a_later_source_can_retry(
    tmp_path: Path, release_texts: dict[str, str]
) -> None:
    release_texts[IR_PAGE] = RELEASE
    writer, _ = summarizer_answering("end_turn", "not JSON")
    with DeliveryStore(tmp_path / "pipeline.db", 1) as store:
        release = Release(
            uber(), FakePoster(), fetcher(), Journal(io.StringIO()), writer, jobs=store
        )
        release.add(*ir_sighting())
        await release.finish()
        job = store.summary(uber().release_key)
        assert job is not None and job.status == FAILED and job.evidence is not None
        captured = json.loads(job.evidence)
        assert captured["raw_answer"] == "not JSON"
        assert captured["document_sha256"] == job.document
