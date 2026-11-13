import io
import json
from pathlib import Path

import pytest

from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.pipeline import Release
from earnings_bot.recorder import Journal
from earnings_bot.source_document import document_from_text
from earnings_bot.summary import SummaryError, summarizer_from
from tests.test_pipeline import FakePoster, fetcher, ir_sighting, uber
from tests.test_summary import summarizer_answering
from tests.test_summary_evidence import fact, value

DOCUMENT = document_from_text("Uber Reports Third Quarter 2026 Results\nRevenue was $10 billion.")


def candidate(*, wrong_only: bool = False) -> str:
    block = DOCUMENT.blocks[-1]
    ref = {
        "document_sha256": DOCUMENT.sha256,
        "block_id": block.id,
        "cell_ids": [],
        "quote": block.text,
    }
    level = value(amount="10", scale="billion", basis="", evidence=[ref])
    good = fact(current_actual=level)
    wrong_metric = fact(metric="Operating income", current_actual=level)
    wrong_period = fact(current_actual=level | {"period": "Q4 2026"})
    return json.dumps(
        {
            "schema_version": "source-values/1",
            "facts": [wrong_metric, wrong_period]
            if wrong_only
            else [good, wrong_metric, wrong_period],
        }
    )


async def test_reference_mode_binds_each_value_and_preserves_auditable_rejections() -> None:
    writer, asked = summarizer_answering("end_turn", candidate())
    writer.references = True
    context = {"advisory_prior_release_profile": {"usual_kpi": "Revenue"}}
    result = await writer.write_document("UBER", DOCUMENT, context=context)
    assert "$10B" in result.text and "Operating income" not in result.text
    assert "Q4" not in result.text and len(result.unverified) == 2
    request = json.loads(asked[0]["messages"][0]["content"])
    assert request["source_document"]["sha256"] == DOCUMENT.sha256
    assert request["issuer_context"] == context
    evidence = json.loads(result.evidence or "{}")
    assert evidence["candidate"] == candidate() and len(evidence["accepted"]) == 1
    assert len(evidence["rejected"]) == 2 and len(evidence["request_sha256"]) == 64
    assert evidence["document_sha256"] == DOCUMENT.sha256


@pytest.mark.parametrize(
    "stop,answer",
    [
        ("end_turn", candidate(wrong_only=True)),
        ("end_turn", "not JSON"),
        ("max_tokens", candidate()),
        ("refusal", candidate()),
    ],
)
async def test_reference_failures_do_not_fall_back_to_the_legacy_checker(
    stop: str, answer: str
) -> None:
    writer, asked = summarizer_answering(stop, answer)
    writer.references = True
    with pytest.raises(SummaryError):
        await writer.write_document("UBER", DOCUMENT)
    assert len(asked) == 1


def test_reference_mode_requires_explicit_opt_in_and_disables_sdk_retries() -> None:
    base = {"discord_token": "x", "meta_model_api_key": "key", "live_summaries": True}
    default = summarizer_from(Settings.model_validate(base))
    opted = summarizer_from(Settings.model_validate(base | {"source_referenced_summaries": True}))
    assert default is not None and not default.references and default.client.max_retries == 1
    assert opted is not None and opted.references and opted.client.max_retries == 0
    assert (
        summarizer_from(
            Settings.model_validate(
                base | {"live_summaries": False, "source_referenced_summaries": True}
            )
        )
        is None
    )


async def test_reference_result_survives_pipeline_persistence_and_restart_without_a_new_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sighting = ir_sighting()

    async def released(*args: object, **kwargs: object):
        return DOCUMENT

    monkeypatch.setattr("earnings_bot.pipeline.release_document", released)
    writer, asked = summarizer_answering("end_turn", candidate())
    writer.references = True
    db = tmp_path / "state.db"
    for _ in range(2):
        with DeliveryStore(db, 7) as store:
            release = Release(
                uber(), FakePoster(), fetcher(), Journal(io.StringIO()), writer, jobs=store
            )
            release.add(source, sighting)
            await release.finish()
            job = store.summary(uber().release_key)
            assert job is not None and job.evidence is not None
            evidence = json.loads(job.evidence)
            assert evidence["document_sha256"] == DOCUMENT.sha256
            assert store.bound_summary_evidence(job.key, job.document) == job.evidence
            assert job.attempts == 1
    assert len(asked) == 1


def test_evidence_versions_and_attempts_never_attach_old_binding_to_a_new_result(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        store.summary_started("release", "pdf", "PDF", "2026-11-13")
        store.summary_written("release", "first", "footer", [], 100, 20, evidence="pdf-evidence")
        store.summary_started("release", "pdf", "PDF", "2026-11-13")
        store.summary_written("release", "legacy result", "footer", [], 100, 20)
        job = store.summary("release")
        assert job is not None and job.evidence is None
        assert store.bound_summary_evidence("release", "pdf") == "pdf-evidence"
        store.summary_started("release", "html", "HTML", "2026-11-13", replaces="PDF")
        store.summary_written(
            "release", "replacement", "footer", [], 100, 20, evidence="html-evidence"
        )
    with DeliveryStore(db, 7) as store:
        assert store.bound_summary_evidence("release", "pdf") == "pdf-evidence"
        assert store.bound_summary_evidence("release", "html") == "html-evidence"
        job = store.summary("release")
        assert job is not None and job.evidence == "html-evidence"
    with DeliveryStore(db, 99) as store:
        assert store.summary("release") is None
        assert store.bound_summary_evidence("release", "html") is None
