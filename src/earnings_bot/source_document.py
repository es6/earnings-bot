"""Version-scoped text/table references for the next summary evidence schema.

Coordinates and header tags are observed structure, not inferred metric/period labels.
Plain text never gains invented table geometry. The current live writer is unchanged.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import cache
from typing import Literal

from pydantic import TypeAdapter

from earnings_bot.text import _clean, _Table, _table_lines, _TextParser

DOCUMENT_VERSION = 1


@dataclass(frozen=True)
class SourceCell:
    id: str
    row: int  # zero-based source row, including blank rows
    column: int  # zero-based logical HTML column, including blank/decorative cells
    text: str
    rowspan: int
    colspan: int
    header: bool
    scope: str


@dataclass(frozen=True)
class SourceTable:
    id: str
    text: str
    cells: tuple[SourceCell, ...]
    rows: int
    nested: bool  # outer layout tables cannot establish independent cell bindings

    def cell_at(self, row: int, column: int) -> SourceCell | None:
        """The original cell covering this coordinate, including a spanning header."""
        if row < 0 or row >= self.rows or column < 0:
            return None
        return next(
            (
                cell
                for cell in self.cells
                if cell.row <= row < cell.row + cell.rowspan
                and cell.column <= column < cell.column + cell.colspan
            ),
            None,
        )


@dataclass(frozen=True)
class SourceBlock:
    id: str
    kind: Literal["paragraph", "table"]
    text: str


@dataclass(frozen=True)
class SourceDocument:
    sha256: str
    format: Literal["html", "text"]
    blocks: tuple[SourceBlock, ...]
    tables: tuple[SourceTable, ...] = ()
    raw_text: str | None = None

    @property
    def text(self) -> str:
        return (
            self.raw_text if self.raw_text is not None else "\n".join(b.text for b in self.blocks)
        )

    def to_json(self) -> str:
        return json.dumps(
            {"version": DOCUMENT_VERSION, "document": asdict(self)}, ensure_ascii=False
        )

    @classmethod
    def from_json(cls, text: str) -> "SourceDocument":
        data = json.loads(text)
        if (
            not isinstance(data, dict)
            or data.get("version") != DOCUMENT_VERSION
            or "document" not in data
        ):
            raise ValueError("Unsupported source document representation")
        return _document_adapter().validate_python(data["document"])

    def resolve(self, sha256: str, reference: str) -> SourceBlock | SourceTable | SourceCell:
        """Reject a reference to another document version or a nonexistent source location."""
        if sha256 != self.sha256:
            raise ValueError("Evidence belongs to a different document version")
        for table in self.tables:
            if table.id == reference:
                return table
            for cell in table.cells:
                if cell.id == reference:
                    return cell
        for block in self.blocks:
            if block.id == reference:
                return block
        raise ValueError(f"Unknown source reference: {reference}")


@cache
def _document_adapter() -> TypeAdapter[SourceDocument]:
    return TypeAdapter(SourceDocument)


def _digest(source: str, kind: str) -> str:
    return hashlib.sha256(
        f"source-document/{DOCUMENT_VERSION}\0{kind}\0{source}".encode()
    ).hexdigest()


def document_from_text(text: str) -> SourceDocument:
    """For an already extracted PDF/text: retain lines, without pretending they are cells."""
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    if not lines:
        raise ValueError("No source text")
    return SourceDocument(
        _digest(text, "text"),
        "text",
        tuple(SourceBlock(f"p{i + 1}", "paragraph", line) for i, line in enumerate(lines)),
        raw_text=text,
    )


def _snapshot(table: _Table, name: str, nested: bool) -> SourceTable:
    table.end_row()
    table._flush_loose()
    cells: list[SourceCell] = []
    active: list[SourceCell] = []
    row = 0
    for item in table.items:
        if isinstance(item, str):
            continue
        active = [cell for cell in active if cell.row + cell.rowspan > row]
        column = 0
        for raw in item:
            # Find a contiguous free span; do not allocate rowspan*colspan grid squares.
            while overlaps := [
                cell
                for cell in active
                if cell.column < column + raw.span and column < cell.column + cell.colspan
            ]:
                column = max(cell.column + cell.colspan for cell in overlaps)
            cell = SourceCell(
                f"{name}r{row}c{column}",
                row,
                column,
                raw.text,
                raw.rows,
                raw.span,
                raw.header,
                raw.scope,
            )
            cells.append(cell)
            active.append(cell)
            column += raw.span
        row += 1
    text = "\n".join(_clean(line) for line in _table_lines(table.items) if _clean(line))
    return SourceTable(name, text, tuple(cells), row, nested)


class _DocumentParser(_TextParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: dict[_Table, str] = {}
        self.nested: set[_Table] = set()
        self.captured: list[SourceTable] = []
        self.positions: dict[int, SourceTable] = {}

    def _identify(self, table: _Table) -> str:
        if table not in self.names:
            self.names[table] = f"t{len(self.names) + 1}"
        return self.names[table]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if self._tables:
            self._identify(self._tables[-1])
            self.nested.update(self._tables[:-1])

    def _end_table(self) -> None:
        table = self._tables[-1]
        snapshot = _snapshot(table, self._identify(table), table in self.nested)
        self.captured.append(snapshot)
        super()._end_table()
        if not self._tables:
            self.positions[len(self.parts) - 1] = snapshot


def document_from_html(html: str) -> SourceDocument:
    """Use the same HTML extraction as the live reader, retaining raw table coordinates.

    Table IDs follow opening order, even for nested layout tables. Only top-level
    tables appear in the reading-order block list; inner tables remain addressable.
    """
    parser = _DocumentParser()
    parser.feed(html)
    parser.finish()
    blocks: list[SourceBlock] = []
    pending: list[str] = []
    paragraphs = 0

    def flush() -> None:
        nonlocal paragraphs
        for line in "".join(pending).splitlines():
            if text := _clean(line):
                paragraphs += 1
                blocks.append(SourceBlock(f"p{paragraphs}", "paragraph", text))
        pending.clear()

    for index, part in enumerate(parser.parts):
        table = parser.positions.get(index)
        if table is None:
            pending.append(part)
            continue
        flush()
        if table.text:
            blocks.append(SourceBlock(table.id, "table", table.text))
    flush()
    if not blocks:
        raise ValueError("No source text")
    return SourceDocument(
        _digest(html, "html"),
        "html",
        tuple(blocks),
        tuple(sorted(parser.captured, key=lambda table: int(table.id[1:]))),
    )
