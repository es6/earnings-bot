"""References must preserve document version and source table geometry before verification."""

import json
from dataclasses import FrozenInstanceError

import pytest

from earnings_bot import source_document
from earnings_bot.source_document import (
    SourceCell,
    SourceDocument,
    SourceTable,
    document_from_html,
    document_from_text,
)
from earnings_bot.text import html_to_text


def at(table: SourceTable, row: int, column: int) -> SourceCell:
    cell = table.cell_at(row, column)
    assert cell is not None
    return cell


def test_blocks_and_references_are_stable_and_scoped_to_the_document_version() -> None:
    html = "<h1>Q3 2026</h1><p>Revenue was $10B.</p><p>Outlook unchanged.</p>"
    first = document_from_html(html)
    repeated = document_from_html(html)
    assert first == repeated
    assert [(block.id, block.text) for block in first.blocks] == [
        ("p1", "Q3 2026"),
        ("p2", "Revenue was $10B."),
        ("p3", "Outlook unchanged."),
    ]
    assert first.resolve(first.sha256, "p2").text == "Revenue was $10B."
    revised = document_from_html(html.replace("$10B", "$11B"))
    assert revised.sha256 != first.sha256
    with pytest.raises(ValueError, match="different document"):
        revised.resolve(first.sha256, "p2")
    with pytest.raises(ValueError, match="Unknown source reference"):
        first.resolve(first.sha256, "p99")
    with pytest.raises(FrozenInstanceError):
        first.blocks[0].text = "Changed by the model"  # type: ignore[misc]


def test_cells_keep_source_rows_columns_headers_blanks_and_decorations() -> None:
    html = """<p>USD millions</p><table>
    <tr><th scope="col">Metric</th><th colspan="4">Q3 2026</th><th>Q3 2025</th></tr>
    <tr><th scope="row">Operating income</th>
    <td>$</td><td>(</td><td>37.5</td><td>)</td><td>9</td></tr>
    <tr><th scope="row">Revenue</th><td></td><td></td><td>100</td><td></td><td>80</td></tr>
    </table><p>Outlook for Q4 2026</p>"""
    document = document_from_html(html)
    [table] = document.tables
    assert [(block.id, block.kind) for block in document.blocks] == [
        ("p1", "paragraph"),
        ("t1", "table"),
        ("p2", "paragraph"),
    ]
    assert document.text == html_to_text(html)
    header = table.cell_at(0, 3)
    assert header and header.text == "Q3 2026" and header.colspan == 4
    assert at(table, 0, 5).text == "Q3 2025"
    negative = document.resolve(document.sha256, "t1r1c3")
    assert isinstance(negative, SourceCell)
    assert (negative.row, negative.column, negative.text) == (1, 3, "37.5")
    assert at(table, 1, 1).text == "$" and at(table, 1, 2).text == "("
    assert at(table, 1, 4).text == ")"
    row = table.cell_at(1, 0)
    assert row and row.header and row.scope == "row" and row.text == "Operating income"
    assert at(table, 2, 1).text == ""  # blank is not the next numeric column
    assert at(table, 2, 3).text == "100"
    assert table.cell_at(3, 0) is None and table.cell_at(-1, 0) is None


def test_rowspan_keeps_the_header_over_the_original_column() -> None:
    html = """<table><tr><th rowspan="2" scope="row">Revenue</th><td>10</td><td>8</td></tr>
    <tr><td>12</td><td>9</td></tr></table>"""
    document = document_from_html(html)
    [table] = document.tables
    assert table.cell_at(1, 0) == table.cell_at(0, 0)
    assert at(table, 1, 1).id == "t1r1c1"
    assert at(table, 1, 1).text == "12"
    assert at(table, 1, 2).text == "9"
    assert document.text == html_to_text(html)


def test_nested_layout_tables_remain_distinct_and_mark_the_outer_binding_uncertain() -> None:
    html = """<p>Before</p><table><tr><td>Layout
    <table><tr><th>Revenue</th><td>10</td></tr></table>
    </td></tr></table><p>After</p>"""
    document = document_from_html(html)
    outer, inner = document.tables
    assert (outer.id, inner.id) == ("t1", "t2")
    assert outer.nested and not inner.nested
    assert document.resolve(document.sha256, "t2r0c1").text == "10"
    assert [block.id for block in document.blocks] == ["p1", "t1", "p2"]
    assert document.text == html_to_text(html)


@pytest.mark.parametrize(
    "html",
    [
        "<table><tr><th>Revenue<th>Prior<tr><td>10<td>9",  # omitted closing tags
        "<p>A</p><table><tr></tr><tr><td>10</td></tr></table><p>B</p>",
        "<head><title>999</title></head><p>Revenue &amp; profit</p><script>999</script>",
        "<table><tr><td><p>A</p><p>B</p></td><td>10</td></tr></table>",
    ],
)
def test_evidence_extraction_preserves_the_live_readers_text(html: str) -> None:
    assert document_from_html(html).text == html_to_text(html)


def test_plain_pdf_text_does_not_gain_invented_table_cells() -> None:
    text = "Three Months Ended\nRevenue   100   80\nOperating income   10   8"
    document = document_from_text(text)
    assert document.format == "text" and document.tables == ()
    assert document.text == text  # spacing in the existing writer's input stays intact
    assert document.resolve(document.sha256, "p2").text == "Revenue 100 80"
    with pytest.raises(ValueError, match="Unknown source reference"):
        document.resolve(document.sha256, "t1r1c1")


@pytest.mark.parametrize(
    "document",
    [
        document_from_text("Revenue   100   80\n\nSecond line"),
        document_from_html('<table><tr><th scope="row">Revenue</th><td>100</td></tr></table>'),
    ],
)
def test_persisted_document_restores_frozen_blocks_and_cell_references(
    document: SourceDocument,
) -> None:
    restored = SourceDocument.from_json(document.to_json())
    assert restored == document and restored.text == document.text
    reference = "t1r0c1" if document.tables else "p1"
    assert restored.resolve(document.sha256, reference) == document.resolve(
        document.sha256, reference
    )
    with pytest.raises(FrozenInstanceError):
        restored.blocks[0].text = "overwritten"  # type: ignore[misc]


def test_unknown_persisted_document_version_is_rejected() -> None:
    data = json.loads(document_from_text("Valid report").to_json())
    data["version"] = 999
    with pytest.raises(ValueError, match="Unsupported source document"):
        SourceDocument.from_json(json.dumps(data))


def test_an_extraction_version_change_invalidates_old_reference_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = document_from_text("Same report, different extraction rules")
    monkeypatch.setattr(source_document, "DOCUMENT_VERSION", 2)
    new = document_from_text("Same report, different extraction rules")
    with pytest.raises(ValueError, match="different document"):
        new.resolve(old.sha256, "p1")


def test_huge_declared_spans_are_bounded_without_allocating_a_square_grid() -> None:
    html = '<table><tr><td colspan="999999" rowspan="999999">10</td></tr></table>'
    [table] = document_from_html(html).tables
    [cell] = table.cells
    assert (cell.colspan, cell.rowspan) == (1000, 1000)
    assert table.cell_at(0, 999) == cell and table.cell_at(999, 999) is None


@pytest.mark.parametrize("value", ["", " \n "])
def test_empty_sources_have_no_bindable_document(value: str) -> None:
    with pytest.raises(ValueError, match="No source text"):
        document_from_text(value)
    with pytest.raises(ValueError, match="No source text"):
        document_from_html(value)
