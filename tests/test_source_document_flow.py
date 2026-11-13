"""Evidence survives the real reader, summary handoff, replacements and store upgrades."""

import asyncio
import io
import sqlite3
from pathlib import Path

from earnings_bot.delivery import DeliveryStore
from earnings_bot.pipeline import Release
from earnings_bot.recorder import Journal
from earnings_bot.source_document import SourceDocument, document_from_text
from earnings_bot.sources import GuessedPage, RssFeed, Sighting
from earnings_bot.summary import Summary
from earnings_bot.text import pdf_text, release_document
from tests.fakes import RoutedFetcher
from tests.test_pipeline import (
    IR_PAGE,
    FakePoster,
    FakeSummarizer,
    pdf_sighting,
    uber,
)


async def test_html_cell_references_reach_the_writer_and_survive_a_store_restart(
    tmp_path: Path,
) -> None:
    html = (
        "<p>Uber Announces Results for Third Quarter 2026.</p>"
        "<p>Revenue and earnings per share increased. </p>"
        * 60
        + '<table><tr><th>Metric</th><th scope="col">Q3 2026</th></tr>'
        '<tr><th scope="row">Revenue</th><td>100</td></tr></table>'
    )
    source = RssFeed("rss:investor.uber.com", "https://investor.uber.com/rss")
    sighting = Sighting(IR_PAGE, "Results", IR_PAGE, earnings=True)
    fetcher = RoutedFetcher({IR_PAGE: (200, html.encode())})

    class Writer(FakeSummarizer):
        document: SourceDocument

        async def write_document(
            self, ticker: str, document: SourceDocument, seconds: float = 0.0
        ) -> Summary:
            self.document = document
            return await super().write_document(ticker, document, seconds)

    writer, log = Writer(), io.StringIO()
    path = tmp_path / "state.db"
    key = uber().release_key
    with DeliveryStore(path, 7) as store:
        release = Release(uber(), FakePoster(), fetcher, Journal(log), writer, jobs=store)
        release.add(source, sighting)
        await release.finish()
        first, second = await asyncio.gather(
            release._text.read_document(source, sighting),
            release._text.read_document(source, sighting),
        )
        assert first is second is writer.document
        assert len(fetcher.requests) == len(writer.calls) == 1

    with DeliveryStore(path, 7) as restarted:
        job = restarted.summary(key)
        assert job is not None and job.status == "delivered" and job.source_document
        restored = SourceDocument.from_json(job.source_document)
        assert restored == writer.document and restored.sha256 == job.document
        assert restored.resolve(job.document, "t1r1c1").text == "100"
        row = restored.tables[0].cell_at(1, 0)
        assert row and row.text == "Revenue" and row.header and row.scope == "row"
        assert job.document in log.getvalue()


async def test_pdf_reader_retains_plain_text_without_inventing_cells() -> None:
    source, sighting = pdf_sighting()
    document = await release_document(RoutedFetcher({}), source, sighting)
    assert document is not None and sighting.body is not None
    assert document.format == "text" and document.tables == ()
    assert document.text == pdf_text(sighting.body)


async def test_classified_exhibit_text_is_not_reinterpreted_as_html() -> None:
    text = "Revenue  < 100\n\nEPS   $1.00; <table> is literal here."
    sighting = Sighting(
        IR_PAGE, "Results", IR_PAGE, body=text.encode(), details={"verified_exhibit": "true"}
    )
    fetcher = RoutedFetcher({})
    document = await release_document(fetcher, GuessedPage(IR_PAGE), sighting)
    assert document is not None and document.text == text and document.tables == ()
    assert fetcher.requests == []


def test_evidence_versions_are_immutable_and_scoped_to_channel_and_release(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    document = document_from_text("Original PDF")
    with DeliveryStore(path, 7) as store:
        for evidence in (document.to_json(), "attempted overwrite"):
            store.summary_started(
                "release", document.sha256, "PDF", "2026-11-13", source_document=evidence
            )
        assert store.summary_document("release", document.sha256) == document.to_json()
        assert store.summary_document("other release", document.sha256) is None
    with DeliveryStore(path, 8) as other_channel:
        assert other_channel.summary_document("release", document.sha256) is None


def test_legacy_summary_schema_upgrades_without_discarding_a_written_result(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE summaries (
                channel_id INTEGER, release_key TEXT, document TEXT, source TEXT,
                status TEXT, attempts INTEGER, text TEXT, footer TEXT, held_back TEXT,
                input_tokens INTEGER, output_tokens INTEGER, updated TEXT,
                PRIMARY KEY (channel_id, release_key)
            )
        """)
        db.execute(
            "INSERT INTO summaries VALUES (7, 'release', 'legacy-text-hash', 'SEC', "
            "'written', 1, 'Saved brief', 'Saved footer', '[]', 10, 5, '2026-11-13')"
        )
    with DeliveryStore(path, 7) as store:
        job = store.summary("release")
        assert job is not None and job.status == "written" and job.text == "Saved brief"
        assert job.document == "legacy-text-hash" and job.source_document is None
        assert job.replaced is None and job.attempts == 1
