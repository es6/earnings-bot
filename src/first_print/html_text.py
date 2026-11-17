"""Shared HTML text/table parsing, independent of retrieval and evidence documents."""

from __future__ import annotations

import re
from bisect import bisect_left
from html.parser import HTMLParser
from typing import NamedTuple

# Tags that start a new line
_BLOCK = re.compile(
    r"p|div|br|li|ul|ol|table|tr|section|article|header|footer|h[1-6]|blockquote|pre|hr"
)
_SKIPPED = {"script", "style", "noscript", "template", "svg", "head"}


# A cell that only dresses up the number beside it, which EDGAR tables give a column of its
# own: a currency or percent sign, a negative number's parenthesis, or a footnote mark
_DECORATION = re.compile(r"[$€£¥₹%()]+|[A-Z]{1,3}\$|\([a-z]{1,2}\)|[*†‡]+")
_MAX_SPAN = 1000  # browsers cap colspan and rowspan at about this too


def clean_text(text: str) -> str:
    return " ".join(text.split())  # the same whitespace as re's \s, and faster


def _span(value: str | None) -> int:
    if not value or value == "1":
        return 1
    digits = re.match(r"\s*([0-9]{1,6})", value)
    return min(max(int(digits[1]), 1), _MAX_SPAN) if digits else 1


class _Cell(NamedTuple):
    text: str
    span: int  # colspan
    rows: int  # rowspan
    header: bool = False
    scope: str = ""


class _Placed(NamedTuple):
    column: int
    span: int
    text: str


class HTMLTable:
    """One <table>'s rows, kept until it ends so its columns can be lined up."""

    def __init__(self) -> None:
        # Rows of cells, and lines of any text outside the cells, in page order
        self.items: list[list[_Cell] | str] = []
        self.loose: list[str] = []
        self.cell: list[str] | None = None
        self._row: list[_Cell] | None = None
        self._spans = (1, 1)
        self._header = False
        self._scope = ""

    def start_row(self) -> None:
        self.end_row()
        self._flush_loose()
        self._row = []

    def end_row(self) -> None:
        self.end_cell()
        if self._row is not None:
            self.items.append(self._row)
            self._row = None

    def start_cell(
        self, colspan: int, rowspan: int, *, header: bool = False, scope: str = ""
    ) -> None:
        self.end_cell()
        if self._row is None:
            self.start_row()
        self.cell = []
        self._spans = (colspan, rowspan)
        self._header, self._scope = header, scope

    def end_cell(self) -> None:
        if self.cell is not None and self._row is not None:
            self._row.append(
                _Cell(clean_text("".join(self.cell)), *self._spans, self._header, self._scope)
            )
        self.cell = None

    def close(self) -> list[str]:
        self.end_row()
        self._flush_loose()
        return table_lines(self.items)

    def _flush_loose(self) -> None:
        text = "".join(self.loose)
        self.loose.clear()
        self.items.extend(line for raw in text.split("\n") if (line := clean_text(raw)))


def _place(items: list[list[_Cell] | str]) -> list[list[_Placed] | str]:
    """Each cell with the column it starts in. A cell from a row above that reaches down with
    rowspan takes up its columns again as an empty cell."""
    placed: list[list[_Placed] | str] = []
    above: list[tuple[int, int, int]] = []  # (column, span, rows still to cover)
    for item in items:
        if isinstance(item, str):
            placed.append(item)
            continue
        taken = sorted((column, span) for column, span, _ in above)
        row: list[_Placed] = []
        reaching: list[tuple[int, int, int]] = []
        column = k = 0
        for cell in item:
            while k < len(taken) and taken[k][0] <= column:
                if column < sum(taken[k]):
                    row.append(_Placed(*taken[k], ""))
                    column = sum(taken[k])
                k += 1
            row.append(_Placed(column, cell.span, cell.text))
            if cell.rows > 1:
                reaching.append((column, cell.span, cell.rows - 1))
            column += cell.span
        above = [(c, s, left - 1) for c, s, left in above if left > 1] + reaching
        placed.append(row)
    return placed


def table_lines(items: list[list[_Cell] | str]) -> list[str]:
    """One line per row, cells joined with " | ". Spacer columns, empty in every row, are left
    out. An empty cell elsewhere stays as a blank between the bars, so a header like
    "Adjusted" stays over the column it names."""
    rows = [
        row
        for row in _place(items)
        if isinstance(row, str) or any(cell.text for cell in row)  # skip all-blank rows
    ]
    cells = [cell for row in rows if not isinstance(row, str) for cell in row]
    # A cell is a column's own when no narrower cell overlaps it; wider ones are group
    # headings like "Three Months Ended". EDGAR's empty spacer cells count, so they keep a
    # heading that spans "$ | 1,000" from claiming the "$" column.
    narrowest: dict[int, int] = {}
    for cell in cells:
        for column in range(cell.column, cell.column + cell.span):
            narrowest[column] = min(narrowest.get(column, cell.span), cell.span)
    columns = sorted(
        {
            cell.column
            for cell in cells
            if cell.text
            and not _DECORATION.fullmatch(cell.text)
            and all(narrowest[c] == cell.span for c in range(cell.column, cell.column + cell.span))
        }
    )
    lines = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
            continue
        fields: list[str] = []
        for cell in row:
            if cell.text:
                fields.append(cell.text)
            else:
                # A blank for each column this empty cell covers. A "$" column's blanks
                # aren't needed, so EDGAR's "Net sales | $ | 1,000" and "Membership | 50"
                # read as before.
                first = bisect_left(columns, cell.column)
                last = bisect_left(columns, cell.column + cell.span)
                fields.extend("" for _ in columns[first:last])
        while fields and not fields[-1]:
            fields.pop()
        lines.append(" | ".join(fields))
    return lines


class HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0
        self._tables: list[HTMLTable] = []  # open tables, innermost last

    def _sink(self) -> list[str]:
        if not self._tables:
            return self.parts
        table = self._tables[-1]
        return table.cell if table.cell is not None else table.loose

    def _in_cell(self) -> bool:
        return bool(self._tables) and self._tables[-1].cell is not None

    def _block(self) -> None:
        # Business Wire wraps every table cell's contents in a <p>, which mustn't split the row
        self._sink().append(" " if self._in_cell() else "\n")

    def _table(self) -> HTMLTable:
        if not self._tables:  # a row or cell without its <table>
            self._tables.append(HTMLTable())
        return self._tables[-1]

    def _end_table(self) -> None:
        lines = self._tables.pop().close()
        if self._in_cell():  # a table inside another table's cell stays in that cell
            self._sink().append(" " + " ".join(lines) + " ")
        else:
            self._sink().append("\n" + "\n".join(lines) + "\n")

    def finish(self) -> None:
        while self._tables:
            self._end_table()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skipping += 1
        elif self._skipping:
            return
        elif tag == "table":
            self._tables.append(HTMLTable())
        elif tag == "tr":
            self._table().start_row()
        elif tag in ("td", "th"):
            spans = dict(attrs)
            self._table().start_cell(
                _span(spans.get("colspan")),
                _span(spans.get("rowspan")),
                header=tag == "th",
                scope=spans.get("scope") or "",
            )
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif self._skipping:
            return
        elif tag == "table" and self._tables:
            self._end_table()
        elif tag == "tr" and self._tables:
            self._tables[-1].end_row()
        elif tag in ("td", "th") and self._tables:
            self._tables[-1].end_cell()
        elif _BLOCK.fullmatch(tag):
            self._block()

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            # Line breaks in the HTML source are just spaces; only tags start new lines
            self._sink().append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """Plain text with one line per paragraph and table row. Cells are joined with " | " so
    financial tables keep their columns."""
    parser = HTMLTextParser()
    parser.feed(html)
    parser.finish()
    return "\n".join(
        line for raw in "".join(parser.parts).splitlines() if (line := clean_text(raw))
    )
