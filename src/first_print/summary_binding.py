"""Fail-closed source binding; a number elsewhere in a quote is never enough.

Each value the model selects must be stated by the source it cites: the measure named by
the cited row (or the clause before the number), the number itself with its sign and scale,
the period of the column (or of the release), the segment, and an adjusted or reported
basis. A conflict rejects the value. Descriptive metadata the source doesn't state is not a
claim: it is dropped (a model's own label in `basis`, a currency on a count), never shown
as if the source said it.
"""

# En dashes and minus signs are real separators and signs in release text.
# ruff: noqa: RUF001

import json
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from first_print.binding_text import (
    DATE,
    FUSED,
    MEASURE_STEMS,
    WORD,
    basis_of,
    claimed_amounts,
    count_words,
    currencies,
    date_of,
    duration,
    durations_in,
    fiscal_offset,
    matches,
    metric_words,
    normalized,
    number_tokens,
    period_end,
    period_issues,
    qualifier_issues,
    quarter,
    reporting_year,
    source_qualifiers,
    source_scale,
    stem,
    stems,
    unit_kind,
    words,
    years,
)
from first_print.sec_results import reported_period
from first_print.source_document import SourceCell, SourceDocument, SourceTable
from first_print.summary_evidence import (
    VALUE_FIELDS,
    EvidenceRef,
    ReferencedFact,
    SourcedValue,
    UnverifiedBrief,
    _reference_issues,
)

_DECORATION = {"", "$", "€", "£", "¥", "(", ")", "%", "bps", "-", "—", "–"}
# A group that names the whole company is no segment
_NEUTRAL_GROUPS = {
    "",
    "total",
    "consolidated",
    "company",
    "overall",
    "group",
    "firmwide",
    "total company",
    "corporate total",
    "all",
}
# Words before a measure's name that make it a different measure: subscription revenue is
# not revenue, free cash flow is not cash flow
_MODIFIERS = {
    "subscription",
    "gross",
    "operating",
    "net",
    "free",
    "recurring",
    "product",
    "products",
    "service",
    "services",
    "organic",
    "comparable",
    "core",
    "segment",
    "deferred",
    "unearned",
    "digital",
    "advertising",
    "cloud",
    "hardware",
    "software",
    "license",
    "licence",
    "interest",
    "noninterest",
    "fee",
    "fees",
    "underlying",
    "pro",
    "forma",
    "attributable",
    "comprehensive",
    "other",
    "unallocated",
    "contribution",
    "incremental",
    "annualized",
}
_GROWTH = re.compile(
    r"growth|change|yoy|qoq|increas|decreas|grew|\bup\b|\bdown\b|\brose\b|\bfell\b|declin|"
    r"\bvs\.?|versus|compared|year[- ]on[- ]year|year[- ]over[- ]year|y/y|% chg|\bchg\b|"
    r"o/\(u\)|over/\(under\)|\bvariance\b",
    re.IGNORECASE,
)
# The words of a period label and nothing else: "Q4 2026", "FY27", "Full year 2026"
_PERIOD_ONLY = re.compile(
    r"\b(?:q[1-4]|[1-4]q|fy|h[12]|fiscal|full|year|quarter|first|second|third|fourth|"
    r"20\d{2}|'?\d{2})\b|’\d{2}",
    re.IGNORECASE,
)
_OUTLOOK_HEADING = re.compile(
    r"\b(?:outlook|guidance|forecast|expectations|forward-looking)\b", re.IGNORECASE
)
_GUIDANCE = re.compile(
    r"guidance|outlook|forecast|\bexpects?\b|\bexpected\b|\bexpectations?\b|\btargets?\b|"
    # "We project a Q4 operating margin of 26.0%", not a construction company's "project
    # backlog"
    r"\banticipat|\b(?:we|company|management|now|also)\s+projects?\b|\bprojected\b|"
    r"\bprojections?\b|\bwill be\b|\bestimat|\bplans? (?:to|for)\b|forward-looking",
    re.IGNORECASE,
)
_DECREASE = re.compile(r"\b(?:decrease|decreased|decline|declined|down|lower|fell|drop|dropped)\b")
# Earlier guidance, as a column or clause names it: Walmart's "Original from 2.19.2026"
_PREVIOUS = re.compile(
    r"previous|prior (?:guidance|outlook)|old guidance|initial|\boriginal\b|"
    r"\bprior\b(?![- ](?:year|period|quarter))",  # Coca-Cola's "Current | Prior" columns
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BoundFact:
    fact: ReferencedFact
    locations: tuple[str, ...]


@dataclass(frozen=True)
class Rejection:
    metric: str
    field: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BindingReport:
    facts: tuple[BoundFact, ...]
    rejected: tuple[Rejection, ...]


_METADATA = re.compile(
    r"quarter|\bQ[1-4]\b|\bFY\s*\d|fiscal year|full.year|months ended|"
    r"\bin (?:USD |US\$ )?(?:millions|thousands|billions)|targets?|outlook|guidance|"
    # Chipotle's "For 2027, management is anticipating the following:"
    r"anticipat|\bfor (?:fiscal )?20\d{2}\b"
    r"|(?:USD|EUR|GBP|CAD|RMB)\s+(?:millions|thousands|billions)?",
    re.IGNORECASE,
)


# "Moody's Analytics ("MA")": a name and the abbreviation the release defines for it
_DEFINED = re.compile(
    r"((?:(?:[A-Z][\w'’&.-]*|of|and|on|for|the|&)\s+){0,5}[A-Z][\w'’&.-]*)"
    r"\s*\(\s*[\"“]?([A-Z]{2,5})[\"”]?\s*\)"
)
_DEFINED_MEASURE = re.compile(
    r"((?:[A-Za-z][\w'’&-]*\s+){0,5}[A-Za-z][\w'’&-]*)\s*\(\s*[\"“]?([A-Z]{2,5})[\"”]?\s*\)"
)
# Abbreviations of an accounting basis, period or market, never expanded
_NOT_NAMES = {"GAAP", "IFRS", "SEC", "YTD", "LTM", "TTM", "YOY", "QOQ", "NYSE", "NASDAQ", "USD"}
# A word that can be left out of a measure's name without changing the measure: "net sales"
# and "sales", "net operating revenues" and "revenues". Unless the release lists both.
_OPTIONAL = frozenset(
    (stem(word), stem(measure))
    for word, measure in (("net", "sales"), ("net", "revenue"), ("operating", "revenue"))
)
# "Moody's Corporation (NYSE: MCO)": the issuer's name and ticker, from its exchange tag
_LISTED = re.compile(
    r"((?:[\w'’&.,-]+\s+){0,5}[\w'’&.,-]+)\s*\((?:NYSE|NASDAQ|Nasdaq|NYSE American)\s*:\s*"
    r"([A-Z][A-Z.]{0,5})\)"
)
_SYMBOL = re.compile(r"\b(?:NYSE|NASDAQ|Nasdaq) symbols?\s*:\s*([A-Z][A-Z.]{0,5})\b")
_LEGAL = {"plc", "inc", "inc.", "co", "co.", "ltd", "ltd.", "corp", "corp.", "n.v.", "s.a.", "ag"}
# Group words that name no segment: the whole company, or the role a fact plays
_NOT_SEGMENT = {
    "the",
    "consolidated",
    "total",
    "company",
    "corporation",
    "inc",
    "plc",
    "group",
    "overall",
    "firmwide",
    "guidance",
    "outlook",
    "headline",
    "headlines",
    "highlights",
    "results",
    "financial",
    "financials",
    "key",
    "metrics",
    "kpis",
    "performance",
    "summary",
    "profitability",
    "profit",
    "growth",
    "net",
    "sales",
    "revenue",
    "revenues",
    "earnings",
    # Categories a summary sorts facts into: "Operating Metrics", "Key business drivers",
    # "Balance sheet and liquidity"
    "operating",
    "data",
    "business",
    "drivers",
    "balance",
    "sheet",
    "liquidity",
    "statistics",
    "indicators",
}
# Words that name the slot a value fills, not its measure: "EPS guidance", "Revenue growth"
_GUIDANCE_STEMS = frozenset(stem(w) for w in ("guidance", "guide", "outlook", "forecast", "target"))
_GROWTH_STEMS = frozenset(stem(w) for w in ("growth", "change", "increase", "decrease", "yoy"))


@dataclass(frozen=True)
class _Terms:
    """What a release itself establishes about the names it uses."""

    acronyms: tuple[tuple[str, str], ...] = ()  # ("MA", "Moody's Analytics"), as it defines
    # (word, measure) pairs the release lists separately: "net sales" and "sales" rows
    distinct: frozenset[tuple[str, str]] = frozenset()
    issuer: frozenset[str] = frozenset()  # the words of the issuer's name and its ticker
    vocabulary: str = ""  # every word of the release, in order, padded with spaces
    end: date | None = None  # the last day of the period the release reports

    def names(self, phrase: str) -> bool:
        """Whether the release says this phrase anywhere, as written or as defined."""
        return any(
            words(normalized(form)) and f" {words(normalized(form))} " in self.vocabulary
            for form in (phrase, self.expand(phrase))
        )

    def expand(self, text: str) -> str:
        for short, name in self.acronyms:
            # Not where it is defined: "Remaining performance obligations (RPO)"
            text = re.sub(rf"(?<![(\"“])\b{short}\b(?![)\"”])", lambda _, name=name: name, text)
        return text

    def optional(self, measure: set[str]) -> set[str]:
        """The words a measure's name can go without here."""
        found = {w for w, m in _OPTIONAL if m in measure and (w, m) not in self.distinct}
        if "eps" in measure:
            found.add(stem("diluted"))
        return found


_NO_TERMS = _Terms()


_TERMS: dict[str, _Terms] = {}


def _terms(document: SourceDocument) -> _Terms:
    """Read once per release version: a live summary binds each fact against the same one."""
    # Summaries run on worker threads: read the cache once, never after another may clear it
    terms = _TERMS.get(document.sha256)
    if terms is None:
        terms = _read_terms(document)
        if len(_TERMS) > 16:
            _TERMS.clear()
        _TERMS[document.sha256] = terms
    return terms


def _read_terms(document: SourceDocument) -> _Terms:
    acronyms: dict[str, str] = {}
    for match in _DEFINED.finditer(document.text):
        names = match[1].split()
        for start in range(len(names)):
            tail = names[start:]
            if not tail[0][0].isupper():
                continue
            if "".join(w[0] for w in tail if w[0].isupper()) == match[2]:
                name = " ".join(tail)
                # A measure's own abbreviation ("Free Cash Flow (FCF)") is an alias, not a name
                if match[2] not in _NOT_NAMES and not set(metric_words(name)) & MEASURE_STEMS:
                    acronyms.setdefault(match[2], name)
                break
    # A measure's own abbreviation, in any case: "Free cash flow (FCF)" makes "FCF forecast"
    # a forecast of free cash flow
    for match in _DEFINED_MEASURE.finditer(document.text):
        names = match[1].split()
        for start in range(len(names)):
            tail = [w for w in names[start:] if w.casefold() not in ("of", "and", "the", "&")]
            if not tail or "".join(w[0].upper() for w in tail) != match[2]:
                continue
            name = " ".join(names[start:])
            if match[2] not in _NOT_NAMES and set(metric_words(name)) & MEASURE_STEMS:
                acronyms.setdefault(match[2], name)
            break
    # Row labels only: a column header ("Reported Net Revenues") describes other rows
    labels = set()
    for table in document.tables:
        for row in range(table.rows):
            cells = [c for c in _row(table, row) if c.text.strip() not in _DECORATION]
            if cells and len(cells[0].text) <= 80 and not _numeric(cells[0]):
                labels.add(frozenset(metric_words(cells[0].text)))
    distinct = frozenset(
        (word, measure)
        for word, measure in _OPTIONAL
        if any(word in label and measure in label and label - {word} in labels for label in labels)
    )
    issuer: set[str] = set()
    for match in _LISTED.finditer(document.text[:10_000]):
        name = []
        for word in reversed(match[1].split()):
            if not (word[0].isupper() or word.casefold() in _LEGAL):
                break
            name.append(word)
        issuer |= set(words(" ".join(name)).split()) | {match[2].casefold()}
    # JPMorgan's letterhead: "NYSE symbol: JPM"
    issuer |= {ticker.casefold() for ticker in _SYMBOL.findall(document.text[:10_000])}
    return _Terms(
        tuple(acronyms.items()),
        distinct,
        frozenset(issuer),
        f" {words(normalized(document.text))} ",
        period_end(document.text),
    )


def _slot_stems(field: str) -> frozenset[str]:
    if field in ("previous_guidance", "new_guidance"):
        return _GUIDANCE_STEMS
    if field in ("reported_growth", "comparable_growth"):
        return _GROWTH_STEMS
    return frozenset()


@dataclass(frozen=True)
class _Context:
    near: str  # the text just before the cited block, since the previous table
    far: str  # the release's opening, which names its reporting period
    offset: int = 0  # how far the release's fiscal year label runs ahead of the calendar
    reported: tuple[str, bool] | None = None  # the year the release reports, and if fiscal
    lead: str = ""  # the paragraph right before a table: "Net sales by business and region:"
    # The scale a caption above states for the tables that follow it until another states
    # its own: Walmart's "Dollars in billions, except as noted" over three segment tables
    scale: str | None = None
    terms: _Terms = _NO_TERMS
    end: date | None = None  # the last day of the period the release reports
    section: str = ""  # an explicitly named business section above the cited block

    def __str__(self) -> str:
        return f"{self.near}\n{self.far}"


def _context(document: SourceDocument, block_id: str) -> _Context:
    """The text that frames a block: what comes just before it, and the release's opening.
    Never other tables' rows."""
    opening: list[str] = []
    local: list[str] = []
    passed_table = False
    lead = ""
    scale = None
    section = ""
    for block in document.blocks:
        if block.id == block_id:
            break
        text = block.text.strip()
        # Some IR pages lay a business heading out as a one-cell table. Accept a name
        # with its acronym, or the explicit "Results for ..." corner of a results table.
        named = re.fullmatch(r"[A-Z][A-Za-z &'’,.-]{2,85}\s+\([A-Z]{2,6}\)", text)
        results = re.match(r"Results for ([^|\n]{2,80})", text, re.IGNORECASE)
        if results and _generic_heading(results[1]):
            results = None
        if named or results:
            heading = results[1].strip() if results else text
            if not section or not _names_group(section, heading):
                section = heading
            local.clear()
        elif (
            block.kind == "paragraph"
            and len(text) <= 100
            and not re.search(r"[.!?;]|\d", text)
            and text.rstrip(":").casefold() != "discussion of results"
        ):
            # A new standalone heading ends the section, even when it names no business.
            section = ""
        lead = block.text if block.kind == "paragraph" and len(block.text) <= 300 else ""
        # A caption's scale holds for the tables after it until another states its own:
        # Walmart's "Dollars in billions, except as noted" over three segment tables
        stated = source_scale(block.text[:400] if block.kind == "table" else block.text[:300])
        if stated and stated != "ambiguous":
            scale = stated
        if block.kind == "table":
            passed_table = True
            local.clear()
        elif passed_table:
            local.append(block.text)
        else:
            opening.append(block.text)
            local.append(block.text)
    # A bullet's period can be in a heading several paragraphs up
    lines = [line for line in local[-10:] if _METADATA.search(line)]
    near = lines[-2:]
    # And an outlook's heading stays in view below the rows of its table: Spotify's "Outlook
    # for Q4'26" above "Total MAUs ... in the quarter", "Total Premium Subscribers ..."
    outlook = next(
        (
            line
            for line in reversed(lines[:-2])
            if _OUTLOOK_HEADING.search(line) and len(line) <= 100  # a heading, not prose
        ),
        None,
    )
    if outlook:
        near = [outlook, *near]
    far = [line for line in opening[:12] if _METADATA.search(line)]
    return _Context("\n".join(near), "\n".join(far), lead=lead, scale=scale, section=section)


# Words after a growth rate that say what it compares, not what it measures
_COMPARED = r"(?:year[- ]over[- ]year|yoy|y/y|growth|increase|decrease|sequentially|qoq|q/q)"


# A cell's value, then its breakdown: Coca-Cola's "At least $13.0 billion, consisting of: At
# least $15.2 billion of cash from operations ..."
_BREAKDOWN = re.compile(
    r",?\s*(?:which includes|which consists of|consisting of|including)\b.*$",
    re.IGNORECASE | re.DOTALL,
)


def _value_part(text: str) -> str:
    return _BREAKDOWN.sub("", text)


def _numeric_text(text: str) -> bool:
    text = _value_part(text)
    text = re.sub(
        # An increase reads as the number it states ("Increase 3.0% to 3.75%"); a decrease
        # would need its sign, so it is never read as a plain number
        r"^(?:nearly|almost|about|approximately|approx\.?|roughly|around|more than|at least|"
        r"less than|up to|at most|over|under|increase(?: of)?|up|growth of|>=|<=|>|<|~)\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    text = re.sub(rf"\s*{_COMPARED}(?:\s+{_COMPARED})*\s*$", "", text, flags=re.IGNORECASE)
    # A label with digits in it ("Q2 FY27", "3Q26", "FY2026") is not a value
    if re.search(r"[A-Za-z]\d|\d[A-Za-z]{2,}", text):
        return False
    tokens = number_tokens(text)
    if not tokens or tokens[0][0] > 3:
        return False
    rest = text[tokens[-1][1] :].strip()
    return not re.search(r"[A-Za-z]{3,}", rest) or bool(
        re.fullmatch(r"(?:to|-|–)?\s*", text[tokens[0][1] : tokens[-1][0]].strip())
        and not re.search(r"[A-Za-z]{4,}", rest)
    )


def _prefixed(cell: SourceCell) -> tuple[str, str]:
    """A cell like "Non-GAAP: $6.30 to $6.35" carries its own label."""
    match = re.fullmatch(r"\s*([A-Za-z][A-Za-z -]{0,30}):\s*(.+)", cell.text)
    if match and _numeric_text(match[2]):
        return match[1], match[2]
    return "", cell.text


def _numeric(cell: SourceCell) -> bool:
    return bool(cell.text.strip()) and _numeric_text(_prefixed(cell)[1])


def _row(table: SourceTable, index: int) -> list[SourceCell]:
    return sorted((c for c in table.cells if c.row == index), key=lambda c: c.column)


def _headers(table: SourceTable, value: SourceCell) -> str:
    return " ".join(_column_headers(table, value))


def _column_headers(table: SourceTable, value: SourceCell) -> list[str]:
    """The header cells above a value that cover its column, top first."""
    headers: list[str] = []
    for row in range(value.row):
        cells = _row(table, row)
        label = next((c for c in cells if c.column == 0), None)
        data_row = bool(
            label
            and label.text
            and words(label.text) != "metric"
            and any(_numeric(c) and not c.header for c in cells if c.column > 0)
        )
        headers.extend(
            cell.text
            for cell in cells
            if (
                cell.scope != "row"
                and (cell.header or not data_row)
                and (cell.column <= value.column < cell.column + cell.colspan)
                # A paragraph laid out across the table heads no column: Walmart's "guidance
                # reflects the Company's expectations as of August 20, 2026". A period's name
                # does: Synopsys's "Range for Three Months Ending October 31, 2026"
                and (not _prose(cell) or _period_header(cell.text))
            )
        )
    return headers


def _period_header(text: str) -> bool:
    """A column header that names a period, however many words it takes, and nothing more."""
    return (
        len(text) <= 80
        and not re.search(r"[.:;]\s", text)
        and bool(
            re.search(r"\bmonths? end|\bquarter\b|\b(?:fiscal|full)[- ]year\b", text, re.IGNORECASE)
        )
    )


# A range's ends in two columns: Synopsys's "Low | High" under "Range for Fiscal Year ..."
_LOW = re.compile(r"\b(?:low|minimum|min)\b", re.IGNORECASE)
_HIGH = re.compile(r"\b(?:high|maximum|max)\b", re.IGNORECASE)


def _range_end(table: SourceTable, row: list[SourceCell], index: int) -> str:
    """The high end of a range whose low end is this cell, from the next value cell in its row,
    when their column headers are the same but for "Low" and "High"; else nothing."""
    own = _headers(table, row[index])
    if not _LOW.search(own):
        return ""
    for position in range(index + 1, len(row)):
        cell = row[position]
        if cell.header or not _numeric(cell):
            continue
        other = _headers(table, cell)
        if not _HIGH.search(other) or _LOW.sub("", own).split() != _HIGH.sub("", other).split():
            return ""
        start = position
        while start and row[start - 1].text.strip() in _DECORATION and start - 1 > index:
            start -= 1
        return " ".join(c.text for c in row[start : position + 1])
    return ""


def _lead_measure(text: str) -> str:
    """The measure a table's introduction names before what it's broken down by: "Net sales
    for the third quarter by business and region:" names net sales."""
    head = re.split(
        r"\b(?:for|by|in|during|from|across|of the)\b|[:,;(.]", text.strip(), maxsplit=1
    )[0]
    if len(head.split()) > 5:
        return ""
    # From the measure's own words: Duolingo's "The table below provides revenues by product
    # type:" names revenues
    names = head.split()
    first = next(
        (
            i
            for i, word in enumerate(names)
            if {stem(w) for w in words(normalized(word)).split()}
            & (MEASURE_STEMS | _MODIFIER_STEMS)
        ),
        len(names),
    )
    return " ".join(names[first:])


# What a table is broken down by follows its measure: "Adjusted operating margin by segment"
_BREAKDOWN_BY = re.compile(
    r"\s+by\s+(?:reportable\s+)?(?:segments?|regions?|geograph\w*|business(?:es)?|products?|"
    r"types?|channels?|markets?|categor(?:y|ies)|divisions?|countr(?:y|ies)|brands?)\b.*$",
    re.IGNORECASE | re.DOTALL,
)


def _row_measure(table: SourceTable, value: SourceCell, label: str, section: str, lead: str) -> str:
    """What a row measures when its label names only a segment or region ("North America"):
    the value's own column header ("Reported Net Revenues"), else the section above the row,
    else the sentence that introduces the table. A label that names a measure keeps it."""
    if set(metric_words(label)) & MEASURE_STEMS:
        return ""
    for text in [*reversed(_column_headers(table, value)), section, _heading(table, value)]:
        if set(metric_words(text)) & MEASURE_STEMS:
            return _BREAKDOWN_BY.sub("", text)
    found = _lead_measure(lead)
    return found if set(metric_words(found)) & MEASURE_STEMS else ""


def _heading(table: SourceTable, value: SourceCell) -> str:
    """The nearest heading row above a value that names a measure: a row with only a first
    cell, however wide ("Revenue:" over "Product" and "Service"). "Cost of revenue:" is the
    nearer heading for the rows under it."""
    for row in range(value.row - 1, -1, -1):
        cells = [c for c in _row(table, row) if c.text.strip() not in _DECORATION]
        if (
            len(cells) == 1
            and cells[0].column == 0
            and not _numeric(cells[0])
            and set(metric_words(cells[0].text)) & MEASURE_STEMS
        ):
            return cells[0].text
    return ""


def _generic_heading(text: str) -> bool:
    """A table's corner cell that names no segment: "Metric", "Consolidated metric", "($ in
    millions)"."""
    plain = re.sub(r"^(?:(?:consolidated|total|company|group|firmwide)\s+)+", "", words(text))
    return (
        not plain
        or plain
        in {
            "metric",
            "metrics",
            "item",
            "items",
            "description",
            "measure",
            "measures",
            "line item",
            "financial highlights",
            "highlights",
            "summary",
            "key metrics",
        }
        or _unit_note(f"({text.strip('() ')})", "")
        or bool(re.search(r"\bin (?:us |usd )?(?:thousands|millions|billions)\b", plain))
        or bool(re.search(r"\bper share (?:amounts|data)\b|\bunaudited\b", plain))
        or bool(re.search(r"\b(?:months|quarter|ended|20\d{2})\b", plain))
    )


def _segment_heading(cell: SourceCell) -> bool:
    """A heading that introduces the rows under it with a colon and names no measure: "APAC:",
    not a statement's "Current assets:" or "Costs and expenses:"."""
    return bool(re.search(r":\s*$", cell.text)) and not set(metric_words(cell.text)) & MEASURE_STEMS


def _label_and_group(table: SourceTable, value: SourceCell) -> tuple[str, str]:
    """The row's own name (the first text cell left of the value) and the section heading
    above it (a row with a single text cell)."""
    label = next(
        (
            c.text
            for c in _row(table, value.row)
            if c.column < value.column
            and c.text.strip() not in _DECORATION
            and not _numeric(c)
            and not _prefixed(c)[0]
        ),
        "",
    )
    section = ""
    # A heading sits in the label column. One as wide as the labels heads the rows under it
    # when it says so with a colon: Netflix's "APAC:" over its "Revenue" row
    width = next((c.colspan for c in _row(table, value.row) if c.column == 0), 1)
    for row in range(value.row):
        cells = [c for c in _row(table, row) if c.text.strip()]
        if not cells or cells[0].column != 0 or _numeric(cells[0]):
            continue
        if (
            len(cells) == 1
            and (
                cells[0].colspan == 1 or (cells[0].colspan <= width and _segment_heading(cells[0]))
            )
            and not _prose(cells[0])
        ):
            # A heading, not a paragraph laid out in the table ("...guidance is based on the
            # following Q3 FY26 figures")
            section = cells[0].text
        elif (
            not _generic_heading(cells[0].text)
            and not any(_numeric(c) and not c.header for c in cells[1:])
            and any(
                re.search(
                    r"\bQ[1-4]\b|\b[1-4]Q\d{2}\b|\bFY|20\d{2}|months|quarter",
                    c.text,
                    re.IGNORECASE,
                )
                for c in cells[1:]
            )
        ):
            # A header row's first cell titles the table: "Walmart U.S. | Q2 FY27 | Q2 FY26"
            section = cells[0].text
    return label, section


def _group(fact: ReferencedFact, terms: _Terms) -> str:
    """The fact's segment, or none when its group names the whole company (its name or
    ticker: "MCO") or only the role the fact plays ("guidance", "headline")."""
    named = set(words(fact.group).split()) - {"and", "of", "the"}
    if words(fact.group) in _NEUTRAL_GROUPS or named <= _NOT_SEGMENT | terms.issuer:
        return ""
    if fact.group.split() and fact.group.split()[-1].casefold() in _LEGAL:
        return ""  # a company's legal name, "JPMorgan Chase & Co.", is the whole company
    grouped = set(metric_words(terms.expand(fact.group)))
    if grouped & MEASURE_STEMS and grouped <= set(metric_words(terms.expand(fact.metric))):
        # It repeats the measure ("Total net interest income" for net interest income), where
        # a segment in the metric's name ("Walmart U.S." comp sales) stays a segment
        return ""
    if (
        fact.kind != "segment"
        and terms.vocabulary
        and not named & terms.issuer
        and not terms.names(fact.group)
    ):
        # A category of the model's own ("Backlog", "Operating data") on a fact it doesn't
        # call a segment. The release never says it, so it is no segment of this release.
        # A group with the issuer's name in it ("Acme Analytics") claims a business unit,
        # and stays to be established
        return ""
    return fact.group


def _group_forms(group: str) -> list[str]:
    """A group as written, and by its name or its abbreviation alone when it gives both:
    "Commercial & Investment Bank (CIB)" is named by "Results for CIB"."""
    found = re.fullmatch(r"\s*(.+?)\s*\(\s*([A-Za-z&]{2,6})\s*\)\s*", group)
    return [group, found[1], found[2]] if found else [group]


def _names_group(group: str, text: str) -> bool:
    return any(words(form) and words(form) in words(text) for form in _group_forms(group))


def _clause_names(group: str, text: str) -> bool:
    """Whether a clause names a segment: its words in order, in any form ("Commissions" in
    "Commission revenue"), without the words that name no segment ("U.S. total" is U.S.)."""
    said = f" {' '.join(stems(words(normalized(text))))} "
    for form in _group_forms(group):
        named = [stem(w) for w in words(normalized(form)).split() if w not in _NOT_SEGMENT]
        if not named or f" {' '.join(named)} " in said:
            return True
    return False


def _unit_note(note: str, unit: str) -> bool:
    """A label's parenthetical that only states units: "(in millions)", "(square feet)"."""
    text = words(note)
    counted = set(count_words(unit)) if unit_kind(unit) == "count" else set()
    return bool(
        re.fullmatch(
            r"(?:in )?(?:usd |us |eur )?(?:thousands?|millions?|billions?|percent|dollars|"
            r"euros?|unaudited)(?: of (?:us )?dollars)?(?: except per share(?: data| amounts)?)?",
            text,
        )
        or (counted and set(text.split()) <= counted | {"in", "of", "thousands", "millions"})
    )


def _identity_issues(
    claim: str,
    label: str,
    group: str,
    unit: str = "",
    *,
    slot: frozenset[str] = frozenset(),
    terms: _Terms = _NO_TERMS,
) -> list[str]:
    """A row names the claimed measure when they differ by at most generic words; a row that
    is a narrower or different measure (a margin, subscription revenue) does not bind. A
    label's own unit note is not part of its name, and neither is the slot a value fills
    ("guidance", "growth")."""
    # Each part of a note on its own: ASML's "EPS (basic; in euros)" is basic EPS
    label = re.sub(
        r"\(([^()]*)\)",
        lambda m: " ".join(
            f"({part.strip()})"
            for part in re.split(r"[;,]", m[1])
            if part.strip() and not _unit_note(f"({part.strip()})", unit)
        ),
        label,
    )
    # The issuer's own name or ticker is no part of a measure's name: "MCO Revenue"
    unnamed = set(metric_words(group)) | slot | {stem(w) for w in terms.issuer}
    claimed = set(metric_words(claim)) - unnamed
    named = set(metric_words(label)) - unnamed
    if not claimed:
        return ["metric names no measure"]
    generic = {stem("consolidated"), "group"}
    if "eps" in claimed:
        generic |= {stem("diluted"), stem("attributable")}
    optional = terms.optional(claimed | named)
    if claimed - named - {stem("diluted")} - optional or named - claimed - generic - optional:
        return ["metric does not match the cited source row"]
    return []


def _basis_issues(fact: ReferencedFact, value: SourcedValue, source: str) -> tuple[list[str], str]:
    """An adjusted measure's name ("Adjusted EPS") must match an adjusted source, and an
    adjusted source value must be claimed as adjusted. A non-GAAP tag only in the model's
    `basis` field, which the source doesn't state, is dropped rather than shown. Returns
    the basis tag the source supports."""
    named = basis_of(fact.metric)
    claimed = named | basis_of(value.basis)
    stated = basis_of(source)
    if "adjusted" in named and "adjusted" not in stated:
        return ["adjusted (non-GAAP) basis is not established by the source"], ""
    if "adjusted" not in claimed and stated == {"adjusted"}:
        return ["the source value is adjusted (non-GAAP), the claim is not"], ""
    return [], "non-GAAP" if "adjusted" in claimed and "adjusted" in stated else ""


def _currency_issues(value: SourcedValue, local: str, context: str) -> tuple[list[str], str]:
    found = currencies(local) or currencies(context)
    claimed = value.currency.strip().upper()
    if not found:
        return [], claimed
    if claimed and claimed not in found:
        return ["currency conflicts with the source"], claimed
    if not claimed:
        return (
            ([], next(iter(found)))
            if len(found) == 1
            else (["currency is ambiguous in the source"], "")
        )
    return [], claimed


_COMPARABLE_METRIC = re.compile(
    r"comparable|same[- ]store|organic|like[- ]for[- ]like|constant currency|comp\b", re.IGNORECASE
)


def _slot_issues(
    field: str, value: SourcedValue, local: str, metric: str = "", before: str | None = None
) -> list[str]:
    """`before` is the text ahead of a number in running text: whether new guidance is old is
    read there, as what follows belongs to its comparator ("...$12.0B (vs. our prior
    projection of $12.5B)")."""
    local = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", local)  # a footnote mark: "1Targets assume"
    guidance = bool(_GUIDANCE.search(local))
    old = bool(
        _PREVIOUS.search(before if before is not None and field == "new_guidance" else local)
    )
    growth = bool(_GROWTH.search(local))
    issues = []
    if field in ("previous_guidance", "new_guidance"):
        if not guidance or (field == "previous_guidance") != old:
            issues.append("guidance status is not established by the source")
    elif field in ("reported_growth", "comparable_growth"):
        if not growth:
            issues.append("growth is not established by the source")
        if (
            field == "comparable_growth"
            and not value.comparison_qualifier
            and not _COMPARABLE_METRIC.search(metric)
        ):
            issues.append("comparable growth needs its source exclusions")
    elif guidance or (
        growth
        and unit_kind(value.unit) in ("%", "bps")
        and "margin" not in local.lower()
        and not _growth_measure(metric)
    ):
        issues.append("actual is attached to a guidance/growth column")
    return issues


def _growth_measure(metric: str) -> bool:
    """A measure that is itself a rate of growth: "Comparable restaurant sales increased 2.1%"
    states its value with a growth word."""
    return bool(re.search(r"comparable|same[- ]store|\bcomps?\b", metric, re.IGNORECASE))


def _guidance_heading(text: str) -> bool:
    return any(
        re.fullmatch(
            r"(?:(?:(?:full|fiscal)[- ]year|q[1-4])\s+20\d{2}\s+)?"
            r"(?:guidance|outlook|forecast)\s*:?",
            line.strip(),
            re.IGNORECASE,
        )
        for line in text.splitlines()
    )


_QUALIFIER_FILLER = frozenset(stem(w) for w in ("basis", "the", "and", "growth", "yoy", "rate"))


def _comparison(field: str, value: SourcedValue, local: str) -> tuple[list[str], SourcedValue]:
    """A comparison label the source doesn't state is dropped, not shown; a comparable
    growth rate whose exclusions it doesn't state is rejected."""
    text = normalized(local)
    claimed = years(value.period)
    earlier = str(int(max(claimed)) - 1) if claimed else "none"
    patterns = {
        "YoY": rf"\byoy\b|prior[- ]year|last year|year[- ]ago|same (?:quarter|period)|"
        rf"(?:compared (?:to|with)|versus|\bvs\b|\bfrom\b|\bover\b)[^.;]*\b{earlier}\b",
        "QoQ": r"\bqoq\b|sequential|prior quarter|previous quarter|last quarter",
        "versus prior guidance": r"previous|prior|initial|raised|lowered",
    }
    if value.comparison and not re.search(patterns[value.comparison], text):
        value = value.model_copy(update={"comparison": ""})
    # Each word the qualifier names, in any order: "organic, constant currency" is stated by
    # "on an organic constant currency basis"
    stated = set(stems(words(text)))
    named = {
        w
        for w in stems(words(value.comparison_qualifier))
        if len(w) > 2 and w not in _QUALIFIER_FILLER
    }
    if value.comparison_qualifier and not all(
        any(said.startswith(word) for said in stated)
        for word in named  # "organically"
    ):
        if field == "comparable_growth":
            return ["comparison qualifier is not established"], value
        value = value.model_copy(update={"comparison_qualifier": ""})
    return [], value


# A growth rate with currency moves taken out: "9% on a constant-dollar basis", "up 5.1% in
# constant currency", "+11% on a foreign exchange (F/X) neutral basis"
_NEUTRAL = re.compile(
    r"constant[- ](?:currency|dollars?)|(?:fx|f/x|foreign exchange)(?:\s*\(f/x\))?[- ]neutral|"
    r"currency[- ]neutral|excluding (?:the impact of )?(?:foreign )?(?:currency|exchange)|"
    r"\bex[- ]?fx\b|\(cc\)",
    re.IGNORECASE,
)


_MONEY_MARK = re.compile(r"US\$|C\$|A\$|[$€£¥]")


def _neutral_name(metric: str) -> bool:
    """Whether a rate's name claims it is currency-neutral: "Revenue growth (F/X neutral)",
    "Constant currency revenue growth". A note beside the name ("(13% in constant currency)",
    "(constant currency also disclosed)") describes another number, not this one."""
    bare = re.sub(
        r"\(([^()]*)\)",
        lambda m: (
            m[0]
            if _NEUTRAL.fullmatch(f"({m[1].strip()})") or _NEUTRAL.fullmatch(m[1].strip())
            else " "
        ),
        metric,
    )
    return bool(_NEUTRAL.search(bare))


def _normal(value: SourcedValue) -> SourcedValue:
    """Model metadata reduced to what binding checks: a basis tag only when it is a basis, a
    comparison qualifier the model put in `basis`."""
    qualifier = value.comparison_qualifier
    if not qualifier and (
        re.search(r"organic|ex-?\s?acquisitions?|excluding|comparable", value.basis, re.IGNORECASE)
        or _NEUTRAL.search(value.basis)
    ):
        qualifier = value.basis
    basis = "non-GAAP" if "adjusted" in basis_of(value.basis) else ""
    return value.model_copy(update={"basis": basis, "comparison_qualifier": qualifier})


def _unit_issues(value: SourcedValue, scope: str) -> tuple[list[str], str]:
    kind = unit_kind(value.unit)
    if kind != "count":
        return [], value.unit
    counted = count_words(value.unit)
    if counted and all(word[:5] in words(scope) for word in counted):
        return [], " ".join(counted)
    return [], "count"  # unconfirmed: shown without a unit, never as the model's label


def _accepted(value: SourcedValue, currency: str, unit: str) -> SourcedValue:
    kind = unit_kind(value.unit)
    return value.model_copy(
        update={
            "currency": currency if kind in ("money", "per share") else "",
            "unit": "money" if kind == "money" else "per share" if kind == "per share" else unit,
        }
    )


# A row label that names cash going out: what was bought, repaid, returned or used
_OUTFLOW = re.compile(
    r"^\s*(?:business\s+|cash\s+paid\s+for\s+)?acquisitions?\b|\bpurchases?\s+of\b|"
    r"^\s*capital\s+expenditures?\b|\brepurchases?\s+of\b|\bdividends?\s+paid\b|"
    r"^\s*(?:principal\s+)?(?:re)?payments?\s+(?:of|on|for)\b|\bused\s+in\b",
    re.IGNORECASE,
)
_CASH_FLOW = re.compile(
    r"\bcash\s+flows?\b|\b(?:investing|financing)\s+activities\b", re.IGNORECASE
)


def _outflow(label: str, around: str) -> bool:
    """Whether a cash-flow statement row's label names an outflow, outside any note in
    parentheses: "Net cash provided by (used in) investing activities" names an inflow."""
    plain = re.sub(r"\([^()]*\)", " ", label)
    return bool(_OUTFLOW.search(plain)) and bool(_CASH_FLOW.search(around))


def _table_binding(
    fact: ReferencedFact,
    field: str,
    value: SourcedValue,
    ref: EvidenceRef,
    table: SourceTable,
    frame: _Context,
) -> tuple[list[str], str, SourcedValue | None]:
    claims, dimension = claimed_amounts(value)
    kind = unit_kind(value.unit)
    terms = frame.terms
    group = terms.expand(_group(fact, terms))
    context = str(frame)
    candidates: list[tuple[str, SourcedValue]] = []
    failures: list[str] = []
    for cell in table.cells:
        if cell.id not in ref.cell_ids or cell.header or not _numeric(cell):
            continue
        prefix, body = _prefixed(cell)
        body = _value_part(body)
        row = _row(table, cell.row)
        index = row.index(cell)
        left, right = index, index + 1
        while left and row[left - 1].text.strip() in _DECORATION:
            left -= 1
        while right < len(row) and row[right].text.strip() in _DECORATION:
            right += 1
        raw = " ".join(
            [*(c.text for c in row[left:index]), body, *(c.text for c in row[index + 1 : right])]
        )
        if len(claims) == 2 and (high := _range_end(table, row, index)):
            raw = f"{raw} to {high}"
        label, section = (terms.expand(t) for t in _label_and_group(table, cell))
        section = section or terms.expand(frame.section)
        headers = f"{_headers(table, cell)} {prefix}".strip()
        named = _PERIOD_ONLY.sub("", headers).strip(" -:|")
        if label.strip() and not _PERIOD_ONLY.sub("", label).strip(" -:|") and named:
            # A table laid out the other way: DoorDash's outlook "Period | Marketplace GOV |
            # Adjusted EBITDA" over "Q4 2026 | $35.4 billion - ..." names its measure in the
            # column and its period in the row
            label, headers = named, f"{label} {prefix}".strip()
        # A label that names the claim binds on its own ("Gain on business divestitures"); a
        # segment's label ("North America") binds with the measure its table states
        measure = _row_measure(table, cell, label, section, frame.lead)
        slot = _slot_stems(field)
        column = _headers(table, cell)
        if kind == "%" and re.search(
            r"\bincrease|\bgrowth\b|\bgrow\b", f"{raw} {column}", re.IGNORECASE
        ):
            # The cell or its column says the rate is a growth rate: "Net sales (cc) |
            # Increase 3.0% to 3.75%", "cRPO" under "Year/Year Growth (%)"
            slot |= _CHANGE_STEMS
        # A column of margins: "Income from operations" under "Margin (%)" is the operating
        # margin
        margin = (
            f"{label} margin"
            if kind == "%" and re.search(r"\bmargin", column, re.IGNORECASE)
            else ""
        )
        # A cell nothing names (a page number laid out as a table) names no measure
        issues: list[str] = ["metric does not match the cited source row"]
        # A section the row sits in is part of its name: "Net revenue" under "Results for
        # CIB" is CIB net revenue
        titled = re.sub(
            r"\b(?:results?|highlights?|summary|selected)\b(?:\s+for)?",
            "",
            section,
            flags=re.IGNORECASE,
        ).strip()
        named_by = ""  # the name that binds the claim
        for name in dict.fromkeys(
            (label, f"{label} {measure}".strip(), f"{titled} {label}".strip(), margin)
        ):
            if not name:
                continue
            named_by = name
            issues = _identity_issues(
                terms.expand(fact.metric),
                name,
                group,
                value.unit,
                slot=slot,
                terms=terms,
            )
            if not issues:
                break
        # The row's own label names the claimed measure in full, without help from above
        self_named = not issues and named_by == label
        if field not in ("previous_guidance", "new_guidance", "prior_year_actual") and (
            _guidance_heading(frame.near)
        ):
            issues.append("actual is inside an explicit guidance section")
        sectioned = bool(words(section)) and words(section) not in _NEUTRAL_GROUPS
        if (
            group
            and not _names_group(group, f"{section} | {label}")
            and (sectioned or not _names_itself(fact, group, label))
        ):
            issues.append("segment/group is not established by the source row")
        elif (
            not group
            and words(section)
            and words(section) not in _NEUTRAL_GROUPS
            # "Results for CIB" is the CIB section; "Results for JPM" is the whole company
            # and "Adjusted operating margin by segment" titles the margins of its rows
            and not set(metric_words(_BREAKDOWN_BY.sub("", section)))
            - _SECTION_WORDS
            - terms.issuer
            <= set(metric_words(fact.metric))
            # A statement's grouping ("Changes in operating assets and liabilities:") names
            # line items, no segment: a subtotal under it that names the claimed measure in
            # full ("Net cash provided by operating activities") is that measure
            and not (
                self_named
                and set(metric_words(section)) & MEASURE_STEMS
                # a grouping says so with a colon, or groups the label's own family:
                # "Net interest margin" under "Net interest income", never a business's
                # "Capital deployed" under "Asset management"
                and (
                    re.search(r":\s*$", section)
                    or set(metric_words(section)) & set(metric_words(label))
                )
            )
            # A reconciliation's heading ("Non-GAAP adjustments") names a basis, no segment
            and not re.fullmatch(
                r"(?:non gaap |gaap |ifrs )*(?:adjustments?|reconciliations?|measures?)",
                words(section),
            )
        ):
            issues.append("the row belongs to a section the claim doesn't name")
        per_share = kind == "per share" or bool(re.search(r"per share|\beps\b", normalized(label)))
        # The header nearest above the value states its scale; a table can restate its
        # headers further down for another section ("Amount ($ billions)")
        scaled = next(
            (h for h in reversed(_column_headers(table, cell)) if source_scale(h)), headers
        )
        caption = source_scale(f"{label} {scaled} {prefix}", per_share)
        if caption is None and kind == "money":
            caption = source_scale(context) or source_scale(table.text[:400]) or frame.scale
        if caption == "ambiguous":
            issues.append("source scale is ambiguous")
            caption = None
        percent_column = bool(
            re.search(r"%|percent|bps|basis points", f"{label} {headers}", re.IGNORECASE)
        )
        tokens = number_tokens(raw, "" if kind in ("%", "bps") else (caption or ""))
        if dimension in ("%", "bps") and percent_column:
            tokens = [
                (a, b, amount, step, dimension if kind_ == "number" else kind_)
                for a, b, amount, step, kind_ in tokens
            ]
        found = [t for t in tokens if t[4] == dimension]
        if (
            kind == "money"
            and found
            and all(t[2] < 0 for t in found)
            and all(amount > 0 for amount, _ in claims)
            and _outflow(label, f"{section}\n{table.text[:300]}\n{frame.near}")
        ):
            # A cash-flow statement puts an outflow in parentheses; the row's own label says it
            # is one: "Business acquisitions, net of cash acquired | (476,585)" is $476.6 million
            # spent on acquisitions
            found = [(a, b, -amount, step, d) for a, b, amount, step, d in found]
        if len(found) != len(claims) or not all(
            matches(claim, token[2], token[3]) for claim, token in zip(claims, found, strict=False)
        ):
            issues.append("value/sign/scale/unit does not match the cited cell")
        # A count unit of the model's own ("gross bookings") on an amount the cell writes as
        # money is shown as the money it is, never without its currency
        money = kind == "count" and bool(_MONEY_MARK.search(raw))
        if re.search(r"per share|\beps\b", normalized(label)) and kind not in (
            "per share",
            "%",  # EPS growth: Coca-Cola's "Comparable EPS | At least 11% growth"
            "bps",
        ):
            issues.append("per-share unit is missing")
        # Other values in the row are other periods unless their own labels set them apart
        # ("GAAP: $4.65 to $4.70 | Non-GAAP: $6.30 to $6.35")
        others = [
            c
            for c in row
            if c is not cell
            and _numeric(c)
            and not c.header
            and not (prefix and _prefixed(c)[0] and _prefixed(c)[0] != prefix)
        ]
        # A change column compares the periods beside it; it needs no year of its own
        change_column = field in ("reported_growth", "comparable_growth") and bool(
            _GROWTH.search(headers)
        )
        # Guidance columns named as versions ("Current | Prior", "Original | As of ...") are
        # not periods
        versions = field in ("previous_guidance", "new_guidance") and bool(
            re.search(
                r"\bcurrent\b|\bprior\b|\bprevious\b|\boriginal\b|\bupdated\b|\brevised\b",
                headers,
                re.IGNORECASE,
            )
        )
        comparative = bool(others) and not years(headers) and not change_column and not versions
        issues.extend(
            period_issues(
                value,
                headers,
                frame.near,
                frame.far,
                comparative=comparative,
                prior=field == "prior_year_actual",
                offset=frame.offset,
                reported=frame.reported,
                change=change_column,
                guide=field in ("previous_guidance", "new_guidance"),
            )
            if not comparative
            else ["comparative values need a period on the source column"]
        )
        # A table's caption can state its basis for every row: "GE Aerospace (Non-GAAP)"
        caption_basis = frame.near if basis_of(frame.near) == {"adjusted"} else ""
        basis_issues, basis = _basis_issues(
            fact, value, f"{label} {headers} {raw} {section} {caption_basis}"
        )
        issues.extend(basis_issues)
        issues.extend(qualifier_issues(value, f"{raw}"))
        if (
            kind == "%"
            # The nearest header decides, as for scale: ServiceNow's "Margin (%)" rows sit
            # below its "Constant Currency Year/Year Growth" rows in one column
            and _NEUTRAL.search(
                f"{label} {([h for h in _column_headers(table, cell) if h.strip()] or [''])[-1]}"
                f" {raw}"
            )
            and not _NEUTRAL.search(f"{value.comparison_qualifier} {fact.metric}")
        ):
            # "Net sales (cc) | Increase 3.0% to 3.75%" is constant-currency growth
            issues.append("the source rate excludes currency moves, the claim doesn't")
        status = headers
        if field in ("previous_guidance", "new_guidance") and not _GUIDANCE.search(headers):
            # A heading above the table, or a paragraph laid out in it above the value ("The
            # Company's third quarter fiscal 2027 guidance is based on...")
            above = " ".join(c.text for c in table.cells if c.row < cell.row and _prose(c))
            status = f"{headers} {frame.near} {above}"
        issues.extend(_slot_issues(field, value, status, fact.metric))
        shown = value
        if field in ("reported_growth", "comparable_growth"):
            compared, shown = _comparison(field, value, f"{headers} {label} {raw} {context}")
            issues.extend(compared)
        currency_issues, currency = (
            _currency_issues(value, f"{raw} {label} {headers}", f"{context} {table.text[:400]}")
            if kind in ("money", "per share") or money
            else ([], "")
        )
        issues.extend(currency_issues)
        _, unit = _unit_issues(value, f"{fact.metric} {label} {headers} {context}")
        if money:
            shown = shown.model_copy(update={"unit": "money"})
        if not issues:
            candidates.append(
                (cell.id, _accepted(shown.model_copy(update={"basis": basis}), currency, unit))
            )
        failures.extend(issues)
    if len(candidates) != 1:
        return (
            list(dict.fromkeys(failures)) or ["value-cell binding is missing or ambiguous"],
            "",
            None,
        )
    return [], candidates[0][0], candidates[0][1]


# A bullet starts a clause, spaced or not: "(cc)1 •Operating income growth of 28.8%"
_CLAUSES = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(\"“•])|;\s*|\n+|\s*[•◦▪]\s*")
# A full stop that ends an abbreviation, not a sentence: "U.S. Treasury securities", "Inc."
_ABBREVIATION = re.compile(
    r"(?:\b[A-Za-z]\.[A-Za-z]\.|\b(?:Inc|Corp|Co|Ltd|No|Nos|vs|approx|Mr|Ms|Dr|Jr|Sr|St)\.)$"
)


def _clauses(text: str) -> list[str]:
    """A passage's clauses. A sentence break after an abbreviation is none: "Cash, cash
    equivalents, and U.S. Treasury securities of $10.4 billion" is one clause."""
    clauses: list[str] = []
    for brk, piece in zip([None, *_CLAUSES.finditer(text)], _CLAUSES.split(text), strict=True):
        if (
            brk is not None
            and clauses
            and not brk[0].strip()  # a sentence break, not a bullet or a semicolon
            and _ABBREVIATION.search(text[: brk.start()])
        ):
            clauses[-1] = f"{clauses[-1]} {piece}"
        else:
            clauses.append(piece)
    return clauses


def _metric_window(claim: list[str], prefix: str) -> tuple[int, int] | None:
    """The last place before a number where the claimed measure is named: all its words,
    in order, within a few words of each other. A name with no measure in it ("Total") is
    named nowhere."""
    if not claim:
        return None
    tokens = [(m.start(), m.end(), m[0]) for m in re.finditer(rf"{FUSED}{WORD}", prefix)]
    names = [stem(t[2].replace("-", "")) for t in tokens]
    for end in range(len(tokens) - 1, -1, -1):
        if names[end] != claim[-1]:
            continue
        position = end
        for word in reversed(claim[:-1]):
            found = next(
                (i for i in range(position - 1, max(position - 4, -1), -1) if names[i] == word),
                None,
            )
            if found is None:
                break
            position = found
        else:
            return tokens[position][0], tokens[end][1]
    return None


# A basis can sit inside the measure's name after a number: "19% growth in organic sales".
# The basis check, not the name, decides whether it is the claimed one.
_BASIS_STEMS = frozenset(stem(w) for w in ("organic", "comparable", "reported", "adjusted"))
_SECTION_WORDS = frozenset(stem(w) for w in ("results", "highlights", "summary", "selected"))
_MANAGED = stem("managed")  # JPMorgan's managed basis: "managed revenue of $50.9 billion"


def _named_after(claim: list[str], following: str, kind: str, growth: bool, prefix: str) -> bool:
    """An amount or a growth rate can name its measure right after it: "delivered $1.1 billion
    in revenue", "closed $1.463 billion of U.S. commercial TCV", "up 28%, with 19% growth in
    organic sales". The phrase after "in" or "of" must be the claimed measure, no more: "$5
    million in revenue synergies" is not revenue. A claimed word the phrase leaves out must be
    the verb just before the number: "repurchased $110.0 million of stock" is stock
    repurchased."""
    lead = (
        r"\s*(?:in|of)\s+"
        if kind == "money"
        else r"\s*(?:growth|increase|decrease|decline)\s+in\s+"
    )
    if kind != "money" and not growth:
        return False
    if re.search(
        r"\b(?:accounted for|contribut\w*|represent\w*|comprised|of which)[\s$€£¥(]*$", prefix
    ):
        return False  # a part of the measure: "Cloud accounted for $4.5 billion of revenue"
    found = re.match(lead + r"([a-z&\-.' ]+)", following)
    if found is None:
        return False
    phrase = re.split(r"\b(?:and|or|compared|from|versus|vs|to|on|over|for|at)\b", found[1])[0]
    named = stems(phrase.rstrip(". "))
    if not named or named[-1] not in claim or set(named) - set(claim) > _BASIS_STEMS:
        return False
    if kind == "money" and _changed_by(prefix, claim):
        return False
    # The rest of the claim's name is said before the number in the same part of the clause:
    # "we completed three acquisitions that added $65.0 million of backlog" is backlog added by
    # acquisitions; "organic growth added $65.0 million of backlog" is not
    return set(claim) - set(named) - _LINK_STEMS <= set(stems(_segment(prefix)))


# Words that link a measure's name to its source and name no measure: "backlog added by
# acquisitions"
_LINK_STEMS = frozenset(stem(w) for w in ("by", "from", "with", "through", "via"))
# A verb right before an amount that makes the amount a change, not a level: "acquisitions
# added $65.0 million of backlog" is no backlog of $65.0 million
_CHANGE_VERBS = frozenset(
    stem(w)
    for w in (
        "added",
        "adding",
        "adds",
        "increased",
        "decreased",
        "grew",
        "rose",
        "fell",
        "declined",
        "reduced",
        "lowered",
        "gained",
        "lost",
        "cut",
        "expanded",
        "improved",
        "up",
        "down",
    )
)


def _segment(prefix: str) -> str:
    """The part of a clause right before a number, from its last comma, semicolon or
    conjunction: "in the first few weeks of fiscal 2027, we completed three acquisitions that
    added" gives "we completed three acquisitions that added"."""
    return re.split(r"[,;]|\b(?:and|but|while|whereas)\b", prefix)[-1]


def _changed_by(prefix: str, claim: list[str]) -> bool:
    """Whether the verb right before a number makes it a change the claim doesn't name."""
    before = [w for w in stems(prefix) if w not in ("by", "a", "an", "approximately", "about")]
    return bool(before) and before[-1] in _CHANGE_VERBS and before[-1] not in claim


# Where the name that follows a number ends: "seven strategic acquisitions that expanded ..."
_PHRASE_END = re.compile(
    r"\b(?:that|which|who|whose|and|or|but|to|in|of|for|with|from|at|on|by|during|since|"
    r"across|as|were|was|are|is|have|has|had|we|our|it|its|they|their|compared|versus|vs|this|"
    r"these|those|last|next|each|per|after|before|ago|than|over|within|into|driven|led)\b|"
    r"[,;:.()]"
)
# The upper end of a range is no value of its own: "the 10% to 12% bookings growth ... targets"
_RANGE_END = re.compile(r"\d\s*%?\s*(?:to|-|–|—)\s*[$€£¥]?\s*$")


def _rate_named_after(claim: list[str], following: str, growth: bool) -> bool:
    """A rate or an amount can name its measure right after it: "supported 7% organic revenue
    growth", "a 25.1% adjusted EBITDA margin", "our over $170 billion commercial services
    backlog". The words between the number and the growth word (or the phrase's end) must be
    the claimed measure exactly: "7% organic revenue growth" is no revenue growth, and "7%
    organic volume growth" no organic revenue growth."""
    if growth:
        found = re.match(r"\s*([a-z&\-.' ]+?)\s+(?:growth|increase|decrease|decline)\b", following)
        phrase = found[1] if found and not _PHRASE_END.search(found[1]) else ""
    else:
        phrase = _PHRASE_END.split(following, maxsplit=1)[0]
    named = metric_words(phrase)
    # A measure, not "a 26% premium" or "$1.7 billion now"
    return bool(set(named) & MEASURE_STEMS) and set(named) == set(claim)


def _counted_after(needed: list[str], following: str, prefix: str) -> bool:
    """A count written as a word counts the noun phrase right after it, no more and no less:
    "seven strategic acquisitions" are strategic acquisitions (not acquisitions in general, nor
    the "high-growth markets" further on). A claimed word the phrase leaves out, and any verb
    just before the count ("we completed three acquisitions"), must be named in the same part
    of the clause."""
    named = metric_words(_PHRASE_END.split(following, maxsplit=1)[0])
    if not named or named[-1] not in needed or set(named) - set(needed):
        return False
    segment = _segment(prefix)
    # One of a list of counts shares the list's verb: "we opened 84 company-owned restaurants,
    # of which 53 ..., and five international partner-operated restaurants" are five opened
    listed = number_tokens(prefix, worded=True)
    verbs = {
        w
        for w in re.findall(r"[a-z]{3,}ed\b", prefix if listed else segment)
        if stem(w) not in needed
    }
    return not verbs and set(needed) - set(named) - _LINK_STEMS <= set(stems(segment))


# A value counted from or after a period's boundary is not that period's: "Subsequent to the
# end of fiscal 2026, we completed three additional acquisitions", "the ten acquisitions we
# have completed since the start of fiscal 2026"
_FROM_BOUNDARY = re.compile(
    r"\b(?:subsequent to|since|after|following)\s+(?:the\s+)?(?:end|start|beginning|close)\s+of\b|"
    r"\bsince\s+(?:fiscal\s+|the\s+)?(?:20\d{2}|fy\s?\d{2}|q[1-4]|year[- ]end|quarter[- ]end)\b|"
    r"\b(?:subsequent to|after)\s+(?:the\s+)?(?:fiscal\s+)?(?:year|quarter|period)[- ]end\b"
)
_AT_DATE = re.compile(r"\b(?:at|as of|as at)\s+(?=[a-z]{3})")


# A year that is part of a name, after a capitalised word that names no period: "our ROAD
# 2030 goals", "Vision 2030"
_NAMED_YEAR = re.compile(r"\b([A-Z][A-Za-z&]*)\s+(20\d{2})\b")
_NOT_NAME = re.compile(
    r"(?:fiscal|fy|calendar|year|quarter|half|in|of|for|during|through|to|by|until|from|since|"
    r"and|the|q[1-4]|h[12]|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|"
    r"nov\w*|dec\w*|full|first|second|third|fourth|end|ended|ending)",
    re.IGNORECASE,
)


def _named_years_dropped(clause: str, text: str, reported: int) -> str:
    """The clause's text without the later years it uses as a name ("our ROAD 2030 goals"):
    an actual's clause can name a plan, not a later period."""
    named = {
        found[2]
        for found in _NAMED_YEAR.finditer(clause)
        if int(found[2]) > reported and not _NOT_NAME.fullmatch(found[1])
    }
    for year in named:
        text = re.sub(rf"(?<![\d.,$]){year}\b", " ", text)
    return text


def _year_before(day: date) -> date:
    return (
        day.replace(year=day.year - 1, day=28)
        if (day.month, day.day) == (2, 29)
        else (day.replace(year=day.year - 1))
    )


def _balance_date(text: str, start: int, end: int, floor: int) -> date | None:
    """The date a balance in running text is stated at: right after it ("$3.29 billion at
    September 30, 2026"), else the last "as of <date>" before it in its clause, after the
    previous amount ("As of September 30, 2026, cash was $132.9 million")."""
    after = re.match(r"\s*,?\s*(?:at|as of|as at)\s+", text[end:])
    if after and (dated := DATE.match(text, end + after.end())):
        return date_of(dated)
    found = None
    for match in _AT_DATE.finditer(text, floor, start):
        if dated := DATE.match(text, match.end()):
            found = date_of(dated)
    return found


def _per_share_window(claim: list[str], prefix: str, following: str) -> tuple[int, int] | None:
    """Earnings per share often follows the net income it divides: "net income of $582
    million, or $0.39 per share". The window is that net income's name."""
    share = re.match(r"\s*per (?:(diluted|basic) )?(?:common |ordinary )?share\b", following)
    if share is None or share[1] == "basic" or set(claim) - {"eps"}:
        return None
    income = list(re.finditer(r"\b(?:net (?:income|earnings|loss)|earnings)\b", prefix))
    return (income[-1].start(), income[-1].end()) if income else None


def _names_itself(fact: ReferencedFact, group: str, label: str) -> bool:
    """A metric that names its own segment ("Business Professionals & Consumers subscription
    revenue") needs no separate group: the row or clause that names it establishes it."""
    specific = set(metric_words(fact.metric)) - set(metric_words(group)) - MEASURE_STEMS
    specific -= {stem(w) for w in ("net", "total", "diluted", "basic", "subscription", "operating")}
    return bool(specific) and specific <= set(metric_words(label))


_MODIFIER_STEMS = frozenset(stem(w) for w in _MODIFIERS)
_SHARE_STEMS = frozenset(stem(w) for w in ("share", "percentage", "percent", "portion", "mix"))
_CHANGE_STEMS = frozenset(
    stem(w) for w in ("growth", "change", "increase", "decrease", "yoy", "qoq", "rate")
)


def _modifier_issues(prefix: str, start: int, claim: list[str]) -> list[str]:
    before = stems(prefix[:start])
    for word in reversed(before[-3:]):
        if word in claim or word in ("total", "record", "the", "its", "our", "a", "an"):
            continue
        if word in _MODIFIER_STEMS:
            return ["the cited clause names a narrower measure"]
        break
    return []


def _positional_issues(
    text: str,
    tokens: list[tuple[int, int, Decimal, Decimal, str]],
    index: int,
    width: int,
    dimension: str,
) -> list[str]:
    """Only words can tie a number in running text to its period. A table row flattened into
    text ("Net income $ 1,331 $ 1,438 $ 448 $ 421") or a "respectively" list gives only
    positions, which say nothing about which column or item a number is."""
    if re.search(r"\brespectively\b", text) and (
        sum(1 for t in tokens if t[4] == dimension) > width
    ):
        return ["a 'respectively' list cannot bind a value by its position"]
    run = tokens[index : index + width]
    for neighbour in (index - 1, index + width):
        if 0 <= neighbour < len(tokens) and tokens[neighbour][4] == dimension:
            # The text between this value and the next one before or after it
            gap = text[min(run[-1][1], tokens[neighbour][1]) : max(run[0][0], tokens[neighbour][0])]
            if not re.search(r"[a-z]{2,}", gap):
                return ["a row of bare values cannot bind a value by its position"]
    return []


def _directed(
    tokens: list[tuple[int, int, Decimal, Decimal, str]], text: str
) -> list[tuple[int, int, Decimal, Decimal, str]]:
    """A growth rate the text calls a decrease is negative: "a 6.9% decrease", "declined 3%"."""
    result = []
    for start, end, amount, step, dimension in tokens:
        around = f"{text[max(0, start - 25) : start]} {text[end : end + 15]}"
        decrease = (
            dimension in ("%", "bps")
            and amount > 0
            and _DECREASE.search(around)
            and not re.search(r"\bincreas|\bgrew\b|\brose\b", around)
        )
        result.append((start, end, -amount if decrease else amount, step, dimension))
    return result


def _paragraph_binding(
    fact: ReferencedFact,
    field: str,
    value: SourcedValue,
    ref: EvidenceRef,
    frame: _Context,
) -> tuple[list[str], str, SourcedValue | None]:
    claims, dimension = claimed_amounts(value)
    kind = unit_kind(value.unit)
    terms = frame.terms
    group = terms.expand(_group(fact, terms))
    metric = terms.expand(fact.metric)
    context = str(frame)
    # A currency-neutral basis in the name ("Revenue growth (F/X neutral)", "Adjusted operating
    # income (cc)") is checked as a basis below, where the source states it, often after the
    # number: "+11% on a foreign exchange (F/X) neutral basis"
    measure = metric_words(_NEUTRAL.sub(" ", metric))
    growth_slot = field in ("reported_growth", "comparable_growth")
    if growth_slot:
        # The slot says it is a growth rate: "ARR growth" is named by "ARR grew"
        measure = [w for w in measure if w not in _CHANGE_STEMS] or measure
    elif field in ("previous_guidance", "new_guidance"):
        # And that it is guidance: "EPS guidance" is named by "expects adjusted EPS between"
        measure = [w for w in measure if w not in _GUIDANCE_STEMS] or measure
    # The group's words are checked as the group; when they are all the measure has
    # ("Adobe AI-first ARR" growth), the measure is named in full
    group_words = set(metric_words(group))
    if group and _names_group(group, terms.expand(frame.section)):
        # "CIB net revenue" below "Commercial & Investment Bank (CIB)": the heading
        # establishes the abbreviation as part of the group, not the measure's name.
        group_words |= {w for form in _group_forms(frame.section) for w in metric_words(form)}
    claim = [w for w in measure if w not in group_words] or measure
    # "Net sales" is named by "Sales in the quarter were", unless the release tells them apart
    required = [w for w in claim if w not in terms.optional(set(claim))] or claim
    named_claim, named_required = claim, required
    failures: list[str] = []
    for clause in _clauses(ref.quote):
        text = normalized(terms.expand(clause))
        caption = source_scale(clause) or (
            source_scale(context)
            if kind == "money" and not re.search(r"billion|million", text)
            else None
        )
        tokens = number_tokens(
            text, "" if kind in ("%", "bps") else (caption or ""), worded=kind == "count"
        )
        if field in ("reported_growth", "comparable_growth"):
            tokens = _directed(tokens, text)
        for index, token in enumerate(tokens):
            run = tokens[index : index + len(claims)]
            if len(run) != len(claims) or not all(
                t[4] == dimension and matches(c, t[2], t[3])
                for c, t in zip(claims, run, strict=True)
            ):
                continue
            if len(run) == 2 and not re.fullmatch(
                # "(3.0) to (1.0) percent": a negative end's parenthesis sits between them
                r"\s*(?:to|-|–|—|and)\s*[$€£¥(]*\s*",
                text[run[0][1] : run[1][0]],
            ):
                continue
            issues: list[str] = []
            prefix = text[: token[0]]
            following = text[run[-1][1] :]
            claim, required = named_claim, named_required
            if _MANAGED in claim and re.search(r"\bmanaged\b", prefix[-60:]):
                # A basis the clause names next to the measure ("managed revenue of $50.9
                # billion") is checked there, not as part of the measure's name
                claim = [w for w in claim if w != _MANAGED] or claim
                required = [w for w in required if w != _MANAGED] or required
            if (
                field in ("previous_guidance", "new_guidance")
                and kind in ("%", "bps")
                and re.search(r"\b(?:declin|decreas|increas|grow|growth)", prefix)
            ):
                # A forecast rate the clause states as a change: "net sales to decline ... by
                # (3.0) to (1.0) percent" is net sales growth guidance
                claim = [w for w in claim if w not in _CHANGE_STEMS] or claim
                required = [w for w in required if w not in _CHANGE_STEMS] or required
                if (
                    re.search(r"\b(?:declin|decreas)", prefix[-80:])
                    and not re.search(r"\b(?:increas|grow)", prefix[-80:])
                    and any(amount > 0 for amount, _ in claims)
                ):
                    # "to decline ... by 3 to 1 percent" is never a rise of 3% to 1%
                    failures.append("the source states a decline, the claim a rise")
                    continue
            worded = text[token[0]].isalpha()  # "seven strategic acquisitions"
            named_next = False  # the measure is named only after the number, by a rule below
            window = None if worded else _metric_window(claim, prefix)
            if window is None and required != claim and not worded:
                window = _metric_window(required, prefix)
            if worded:
                # A count written as a word counts only the noun phrase right after it
                if _counted_after(required, following, prefix):
                    window, named_next = (token[0], token[0]), True
            elif window is None and (
                kind == "count" or (kind == "%" and re.match(r"\s*of\b", following))
            ):
                # A count can name what it counts after its number ("repurchased 9.5 million
                # shares", "1 billion monthly active users"), and a share what it is a share of
                # ("represented 38.8% of total food and beverage revenue")
                # To the clause's end: "340 to 360 new company-owned restaurant openings in the
                # U.S. and Canada"
                following_words = stems(following)[:14]
                preceding_words = stems(prefix)[-6:]
                # A share's name says it is one ("Digital sales share of ... revenue"); the
                # source says so with "of" after the rate
                needed = [w for w in required if kind != "%" or w not in _SHARE_STEMS]
                # and what it is a share of is the claim's: "of total food and beverage
                # revenue" is no share of total revenue
                of = re.match(r"\s*of\s+(.*)", following) if kind == "%" else None
                whole = re.split(
                    r"\b(?:for|in|during|compared|versus|vs|from|as|at)\b|[,.;(]",
                    of[1] if of else "",
                    maxsplit=1,
                )[0]
                if (
                    set(needed) & set(following_words)
                    and set(needed) <= set(following_words) | set(preceding_words)
                    and set(metric_words(whole)) <= set(needed)
                ):
                    window = (token[0], token[0])
            if (
                window is None
                and not worded
                and _named_after(required, following, kind, growth_slot, prefix)
            ):
                window = (token[0], token[0])
            if (
                window is None
                and not worded
                and (kind == "%" or (kind == "money" and not _changed_by(prefix, required)))
                and field in ("current_actual", "reported_growth", "new_guidance")
                and _rate_named_after(required, following, growth_slot)
            ):
                window, named_next = (token[0], token[0]), True
            if named_next and _RANGE_END.search(prefix):
                failures.append("the number ends a range, it is no value of its own")
                continue
            if (
                window is None
                and not worded
                and len(claim) > 1
                and re.search(r"[a-z]ed$", fact.metric.casefold())
            ):
                # A name ending in its verb reads verb first in a release: "Total contract value
                # closed" is "Closed total contract value of $2.918 billion"
                window = _metric_window(claim[-1:] + claim[:-1], prefix)
            if window is None and kind == "per share":
                window = _per_share_window(required, prefix, following)
            if window is None and kind == "per share":
                # "Per share" after the amount is part of the measure's name: "increased Visa's
                # quarterly cash dividend 16% to $0.780 per share"
                share = re.match(
                    r"\s*per (?:(?:diluted|basic|common|ordinary|class [a-z]) )*share\b", following
                )
                rest = [w for w in claim if w not in set(stems(share[0]))] if share else claim
                if rest and rest != claim:
                    window = _metric_window(rest, prefix)
            if window is None:
                failures.append("metric is absent from the cited claim clause")
                continue
            between = stems(
                re.sub(
                    r"[$€£¥]?\(?-?\d[\d,.]*\)?\s*(?:%|bps|billion|million|thousand|[kmbt]\b)?",
                    " ",
                    prefix[window[1] :],
                )
            )
            if any(w in MEASURE_STEMS and w not in claim for w in between):
                failures.append("a different measure lies between the metric and the number")
                continue
            # The name the clause gives runs on to the verb or preposition before the number
            named_on = re.split(
                r"\b(?:of|was|were|is|are|totaled|totalled|totaling|totalling|reached|grew|rose|"
                r"increased|decreased|declined|fell|to|at|came|amounted|stood|up|down|for|in)\b|"
                r"[:\d$€£¥(]",
                prefix[window[1] :],
                maxsplit=1,
            )[0]
            if window[1] > window[0] and re.search(r"\b(?:and|or|plus)\b|&", named_on):
                # The number totals more than the claim names: "Cash, cash equivalents, and
                # U.S. Treasury securities of $10.4 billion" is not the cash alone
                failures.append("the cited clause names a broader measure")
                continue
            issues.extend(_modifier_issues(prefix, window[0], claim))
            issues.extend(_positional_issues(text, tokens, index, len(claims), dimension))
            after_end = run[-1][1]
            following = (
                tokens[index + len(claims)][0] if index + len(claims) < len(tokens) else len(text)
            )
            after = re.split(r"\band\b|,|;", text[after_end:following], maxsplit=1)[0]
            previous = [t for t in tokens[:index] if t[4] == dimension]
            before_start = max(window[0] - 40, previous[-1][1] if previous else 0)
            boundaries = list(
                re.finditer(r"[,;]|\b(?:and|but|while|whereas)\b", text[before_start : window[0]])
            )
            if previous and boundaries:
                before_start += boundaries[-1].end()
            before = text[before_start : token[0]]
            roles = list(
                re.finditer(
                    r"\b(?:compared (?:with|to)|versus|vs\.?|from|to)\b", prefix[window[1] :]
                )
            )
            if roles and roles[-1][0] == "to":
                before = prefix[window[1] + roles[-1].end() :]
            temporal = f"{before} {text[after_end:following]}"
            prior_word = bool(re.search(r"\b(?:last year|prior[- ]year|year[- ]ago)\b", temporal))
            comparator = bool(roles) and (
                roles[-1][0] == "from" or (roles[-1][0] != "to" and bool(previous))
            )
            if field == "current_actual" and (prior_word or comparator):
                issues.append("current actual is attached to the comparison/prior-year amount")
            if field not in ("previous_guidance", "new_guidance") and _FROM_BOUNDARY.search(text):
                issues.append("the clause counts the value from a period's boundary")
            dated = _balance_date(text, token[0], after_end, previous[-1][1] if previous else 0)
            if dated is not None and field in ("current_actual", "prior_year_actual"):
                # A balance on a date is the value of the period that ends on it: backlog "at
                # September 30, 2026" is fiscal 2026's, never "at June 30, 2026"
                end = frame.end
                if field == "prior_year_actual" and end is not None:
                    end = _year_before(end)
                if end is None or dated != end:
                    issues.append("the balance's date is not the end of the claimed period")
            if (
                field == "prior_year_actual"
                and not prior_word
                and not (years(temporal) and years(temporal) == years(value.period))
            ):
                issues.append("prior-year actual lacks a period attached to its own amount")
            near = " ".join(re.findall(r"[a-z-]+", prefix[: window[0]])[-2:])
            local = f"{near} {text[window[0] : token[0]]}"
            basis_source = (
                after if basis_of(after) else re.sub(r"[$€£¥]?\(?-?\d[\d,.]*.*$", "", local)
            )
            basis_issues, basis = _basis_issues(fact, value, basis_source)
            issues.extend(basis_issues)
            if re.match(
                r"[\s,]*(?:excluding|exclusive of|before|ex-)\b", text[after_end:following]
            ) and not re.search(
                r"exclud|\bex-|before|adjusted|non-gaap|underlying",
                f"{fact.metric} {value.basis} {value.comparison_qualifier}",
                re.IGNORECASE,
            ):
                # "ROTCE of 21%, excluding discretionary investment securities losses" is not
                # the reported ROTCE (20%)
                issues.append("the source value excludes items the claim doesn't")
            neutral_source = bool(
                _NEUTRAL.search(text[after_end:following])
                or _NEUTRAL.search(text[max(0, window[0] - 30) : token[0]])
            )
            neutral_claim = bool(_NEUTRAL.search(f"{value.comparison_qualifier} {fact.metric}"))
            if growth_slot and neutral_source and not neutral_claim:
                # Like an adjusted value claimed as reported: "9% on a constant-dollar basis"
                # is not the reported growth
                issues.append("the source growth excludes currency moves, the claim doesn't")
            # And the other way: a currency-neutral claim needs the source to say so of this
            # number, in its own clause. Netflix's "grew 12% year over year (+11% on a foreign
            # exchange (F/X) neutral basis)" makes 11%, not 12%, the F/X-neutral growth
            own_after = re.split(
                r"[,;]|\b(?:and|while|but|whereas)\b", text[after_end:following], maxsplit=1
            )[0]
            own_before = re.split(
                r"[,;]|\b(?:and|while|but|whereas)\b", text[max(0, window[0] - 30) : window[0]]
            )[-1]
            if (
                _neutral_name(fact.metric)
                and (growth_slot or (field == "current_actual" and kind == "%"))
                and not _NEUTRAL.search(f"{own_before} {text[window[0] : token[0]]} {own_after}")
            ):
                issues.append("the claim excludes currency moves, the source value doesn't")
            rate = field == "current_actual" and kind == "%" and _growth_measure(fact.metric)
            if rate and neutral_source != neutral_claim:
                # A rate such as comparable sales is either the currency-neutral one or not
                issues.append("currency-neutral basis differs between the claim and the source")
            money = kind == "count" and bool(
                _MONEY_MARK.search(text[max(0, token[0] - 4) : token[0] + 1])
            )
            scope = terms.expand(_group(fact.model_copy(update={"group": frame.section}), terms))
            stated_group = _clause_names(group, text) or _names_itself(fact, group, text)
            if group and not stated_group and not _clause_names(group, scope):
                issues.append("segment/group is absent from the claim clause")
            elif group and not stated_group:
                # A heading scopes a bare measure, not another named business's measure:
                # "Banking & Payments revenue" beneath CIB is not CIB net revenue.
                # Stay conservative when the measure is named only after the number.
                before = _segment(prefix[: window[0]])
                leading = set(metric_words(before))
                leading -= {stem(w) for w in ("the", "our", "its", "this", "segment", "business")}
                if named_next or leading:
                    issues.append("the section cannot establish this more specific source measure")
            if (
                scope
                and not group
                and not _names_group(scope, metric)
                and not _names_itself(fact, scope, text)
            ):
                issues.append("the source belongs to a business section the claim doesn't name")
            bounds = text[max(0, token[0] - 20) : after_end]
            # "approximately $1.2 billion" claimed as "$1.2 billion" is shown as the source
            # words it; any other difference in a bound is a different claim
            approximate = value.value_qualifier == "exact" and source_qualifiers(bounds) == {
                "approximately"
            }
            if not approximate:
                issues.extend(qualifier_issues(value, bounds))
            span = f"{local} {text[token[0] : after_end]} {after}"
            if growth_slot:
                span = f"{span} {text[after_end:following]}"
            elif field in ("previous_guidance", "new_guidance"):
                # Anywhere in the clause before it: "ASML expects Q4 2026 total net sales
                # between", "Targets assume non-GAAP operating margin of ~44.0%"
                span = f"{prefix} {span}"
                if not _GUIDANCE.search(span) and not named_next:
                    span = f"{span} {frame.near}"  # an "Outlook" heading above the bullet
            elif named_next:
                # A measure named after its number is read with the whole clause before it: "We
                # expect to finish 2026 at ... 15% to 18% revenue growth" is no actual, and
                # "will attend ... four upcoming investor conferences" is a plan
                span = f"{prefix} {span}"
                if re.search(r"\bwill\b|\w'll\b|\bupcoming\b", f"{prefix} {after}"):
                    issues.append("the clause states a plan, not a result")
            issues.extend(_slot_issues(field, value, span, fact.metric, before=prefix[-120:]))
            if field not in ("previous_guidance", "new_guidance", "prior_year_actual") and (
                _guidance_heading(frame.near)
            ):
                issues.append("actual is inside an explicit guidance section")
            shown = value
            if growth_slot:
                compared, shown = _comparison(field, value, f"{text[after_end:following]} {text}")
                issues.extend(compared)
            guide = field in ("previous_guidance", "new_guidance")
            # A forecast's period is named up to its number and just after it, not in the
            # reasons that follow ("...$12.0B ... due primarily to a transaction in Q4")
            period_text = f"{text[:after_end]} {after}" if guide else text
            if named_next:
                # A measure named after its number takes the period its own part of the clause
                # states, when it states one: "Following record fourth quarter revenue, our
                # backlog entering fiscal 2027 strongly supports our guidance of 15% revenue
                # growth" is fiscal 2027's, not the fourth quarter's
                own = _segment(prefix) + re.split(r"[,;:]", text[len(prefix) :], maxsplit=1)[0]
                if years(own) or quarter(own) or durations_in(own):
                    period_text = own
            if not guide and frame.reported:
                # A later year used as a name is no period of an actual: the "ROAD 2030 goals"
                # a fiscal 2026 count was made under
                period_text = _named_years_dropped(clause, period_text, int(frame.reported[0]))
            period_near = f"{ref.quote}\n{frame.near}"
            if field == "current_actual" and frame.reported:
                # A year attached to an explicit comparison after this amount describes its
                # comparator. Keep earlier period labels authoritative and require the
                # comparator's sole year to be the release's prior year before trimming it.
                comparison = re.search(
                    r"\b(?:compared (?:with|to)|versus|vs\.?|"
                    r"(?:up|down)\s+[\d,.]+%\s+from)\b",
                    text[after_end:],
                )
                if comparison and years(text[after_end + comparison.start() :]) == {
                    str(int(frame.reported[0]) - 1)
                }:
                    period_text = text[: after_end + comparison.start()]
                    period_near = f"{period_text}\n{frame.near}"
            issues.extend(
                period_issues(
                    value,
                    period_text,
                    period_near,
                    frame.far,
                    comparative=False,
                    clause=True,
                    prior=field == "prior_year_actual"
                    and years(period_text) != years(value.period),
                    offset=frame.offset,
                    reported=frame.reported,
                    change=growth_slot,
                    guide=field in ("previous_guidance", "new_guidance"),
                )
            )
            currency_issues, currency = (
                _currency_issues(value, clause, context)
                if kind in ("money", "per share") or money
                else ([], "")
            )
            issues.extend(currency_issues)
            _, unit = _unit_issues(value, f"{fact.metric} {text}")
            if money:
                shown = shown.model_copy(update={"unit": "money"})
            if approximate:
                shown = shown.model_copy(update={"value_qualifier": "approximately"})
            if not issues:
                kept = shown.model_copy(update={"basis": basis})
                return [], ref.block_id, _accepted(kept, currency, unit)
            failures.extend(issues)
    return list(dict.fromkeys(failures)) or ["value is not stated in the cited passage"], "", None


def _pair_issues(fact: ReferencedFact) -> dict[str, tuple[str, ...]]:
    issues = {}
    current = fact.current_actual
    prior = fact.prior_year_actual
    if current and prior:
        current_year = years(current.period)
        prior_year = years(prior.period)
        if (
            len(current_year) != 1
            or len(prior_year) != 1
            or int(next(iter(current_year))) != int(next(iter(prior_year))) + 1
            or quarter(current.period) != quarter(prior.period)
            or unit_kind(current.unit) != unit_kind(prior.unit)
            or current.currency != prior.currency
            or basis_of(current.basis) != basis_of(prior.basis)
            or words(current.comparison_qualifier) != words(prior.comparison_qualifier)
            or duration(current.duration or current.period)
            != duration(prior.duration or prior.period)
        ):
            issues["prior_year_actual"] = (
                "prior-year actual is not comparable to the current actual",
            )
    for field in ("reported_growth", "comparable_growth"):
        change = getattr(fact, field)
        if (
            current
            and change
            and (
                (quarter(change.period), years(change.period))
                != (quarter(current.period), years(current.period))
                or basis_of(change.basis) != basis_of(current.basis)
            )
        ):
            issues[field] = ("growth is for a different reporting period or accounting basis",)
    if fact.previous_guidance and fact.new_guidance:
        old, new = fact.previous_guidance, fact.new_guidance
        if (
            years(old.period) != years(new.period)
            or quarter(old.period) != quarter(new.period)
            or unit_kind(old.unit) != unit_kind(new.unit)
            or old.currency != new.currency
            or basis_of(old.basis) != basis_of(new.basis)
        ):
            issues["previous_guidance"] = (
                "previous guidance is not comparable to the new guidance",
            )
    return issues


_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])|\n+|\s+•\s+|;\s+")


def _bind_update(fact: ReferencedFact, document: SourceDocument) -> tuple[str, str]:
    """The update shown is the source's own sentence. The model's version must agree with it:
    every number it states, with its sign, and most of its words."""
    quotes = [ref.quote for ref in fact.update_evidence if not _reference_issues(ref, document)]
    if not quotes:
        return "", "update evidence does not resolve in the document"
    claim = normalized(fact.update)
    stated = [(t[2], t[3], t[4]) for t in number_tokens(claim)]
    content = set(re.findall(r"[a-z]{4,}", claim))
    best, overlap = "", 0.0
    for quote in quotes:
        for sentence in _SENTENCES.split(quote):
            text = normalized(sentence)
            if " ".join(claim.split()) in " ".join(text.split()):
                return " ".join(sentence.split()), ""
            numbers = [(t[2], t[3], t[4]) for t in number_tokens(text)]
            if not years(claim) <= years(text):
                continue  # a different year is a different claim
            if not all(
                any(
                    kind == dimension and matches((amount, step), source, source_step)
                    for source, source_step, dimension in numbers
                )
                for amount, step, kind in stated
            ):
                continue
            shared = len(content & set(re.findall(r"[a-z]{4,}", text))) / max(len(content), 1)
            if shared > overlap:
                best, overlap = " ".join(sentence.split()), shared
    if best and overlap >= 0.5:
        return best, ""
    return "", "update is not supported by its cited sentence"


def screen_answer(answer: str) -> tuple[list[ReferencedFact], list[Rejection]]:
    """Each fact on its own: a malformed fact is rejected alone, and an update given without
    evidence is dropped while the fact's values are kept."""
    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        return [], [Rejection("", "brief", ("the answer is not JSON",))]
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != "source-values/1"
        or not isinstance(data.get("facts"), list)
    ):
        return [], [Rejection("", "brief", ("the answer is not a source-values/1 brief",))]
    facts: list[ReferencedFact] = []
    rejected: list[Rejection] = []
    for raw in data["facts"]:
        metric = str(raw.get("metric", "")) if isinstance(raw, dict) else ""
        # From JSON, as the model sent it: strict mode reads JSON arrays as tuples
        try:
            facts.append(ReferencedFact.model_validate_json(json.dumps(raw)))
            continue
        except ValidationError as error:
            reason = error.errors()[0].get("msg", "invalid fact")
        if isinstance(raw, dict) and raw.get("update") and not raw.get("update_evidence"):
            try:
                facts.append(ReferencedFact.model_validate_json(json.dumps(raw | {"update": ""})))
                rejected.append(Rejection(metric, "update", ("an update needs its evidence",)))
                continue
            except ValidationError:
                pass
        rejected.append(Rejection(metric, "fact", (str(reason)[:200],)))
    return facts, rejected


def bind_answer(answer: str, document: SourceDocument) -> BindingReport:
    facts, rejected = screen_answer(answer)
    report = _bind(facts, document)
    return BindingReport(report.facts, tuple(rejected) + report.rejected)


def bind_brief(candidate: UnverifiedBrief, document: SourceDocument) -> BindingReport:
    return _bind(list(candidate.facts), document)


_BOUNDARY = r"[,;]|\b(?:and|while|but|whereas)\b"


def _title(table: SourceTable, value: SourceCell) -> str:
    """The nearest short title row above a value, however wide: GE's "Non-GAAP Metrics" over
    its "Operating Profit" row."""
    for row in range(value.row - 1, -1, -1):
        cells = [c for c in _row(table, row) if c.text.strip() not in _DECORATION]
        if len(cells) == 1 and cells[0].column == 0 and not _numeric(cells[0]):
            return cells[0].text if len(cells[0].text) <= 60 else ""
    return ""


def bound_scope(document: SourceDocument, value: SourcedValue, metric: str = "") -> str:
    """The text that states a bound value: for a table cell its row label, section, column
    headers and the cell; in running text, from just before the metric's name (else the
    clause's start) to the number and on to the next comma or "and", or to the sentence's
    end when it closes with "all in constant currency". Plus a short caption just above ("GE
    Aerospace (Non-GAAP)"). What a label claims about the value must be said here."""
    claims, dimension = claimed_amounts(value)
    claim = [w for w in metric_words(_NEUTRAL.sub(" ", metric)) if w not in _CHANGE_STEMS]
    claim = [w for w in claim if w not in _GUIDANCE_STEMS]
    texts: list[str] = []
    for ref in value.evidence:
        block = document.resolve(ref.document_sha256, ref.block_id)
        frame = _context(document, ref.block_id)
        texts += [line for line in f"{frame.near}\n{frame.lead}".splitlines() if len(line) <= 100]
        passage = ref.quote
        if isinstance(block, SourceTable):
            cells = [c for c in block.cells if c.id in ref.cell_ids]
            if cells and not _prose(cells[0]):
                cell = cells[0]
                label, section = _label_and_group(block, cell)
                texts += [label, section, _headers(block, cell), _heading(block, cell), cell.text]
                texts.append(_title(block, cell))
                continue
            passage = cells[0].text if cells else passage
        for clause in _clauses(passage):
            text = normalized(clause)
            caption = "" if dimension in ("%", "bps") else (source_scale(clause) or "")
            tokens = number_tokens(text, caption)
            for index, token in enumerate(tokens):
                run = tokens[index : index + len(claims)]
                if len(run) == len(claims) and all(
                    t[4] == dimension and matches((abs(c[0]), c[1]), abs(t[2]), t[3])
                    for c, t in zip(claims, run, strict=True)
                ):
                    window = _metric_window(claim, text[: token[0]]) if claim else None
                    start = 0
                    if window is not None:
                        # with the two words before the name: "non-GAAP EPS", "adjusted EBITDA"
                        before = re.split(_BOUNDARY, text[: window[0]])[-1]
                        start = window[0] - len(" ".join(before.split()[-2:])) - 1
                    run_end = token[1]
                    if len(claims) == 2 and index + 1 < len(tokens):
                        run_end = tokens[index + 1][1]
                    after = re.split(_BOUNDARY, text[run_end:], maxsplit=1)[0]
                    whole = re.search(r"\ball (?:in|on|at)\b[^;]*", text)
                    texts.append(
                        f"{text[max(0, start) : run_end]}{after} {whole[0] if whole else ''}"
                    )
    return "\n".join(texts)


def release_bases(document: SourceDocument) -> str:
    """A basis the release declares for all it reports: JPMorgan's "information is presented
    on a managed basis, which is a non-GAAP financial measure, unless otherwise noted"."""
    return "\n".join(
        sentence
        for sentence in _SENTENCES.split(document.text[:20_000])
        if re.search(
            r"\bunless otherwise (?:noted|stated|indicated|specified)\b", sentence, re.IGNORECASE
        )
        and re.search(r"\bbasis\b", sentence, re.IGNORECASE)
    )


def _group_stated(fact: ReferencedFact, document: SourceDocument, terms: _Terms) -> bool:
    """Whether the evidence of every kept value names the fact's group: in its passage, or
    for a cell in the row's label or the section above it."""
    group = terms.expand(fact.group)
    if not words(group):
        return True
    for value in fact.values():
        if value is None:
            continue
        for ref in value.evidence:
            block = document.resolve(ref.document_sha256, ref.block_id)
            if isinstance(block, SourceTable):
                cells = [c for c in block.cells if c.id in ref.cell_ids]
                # A value cell names its segment by its row and section; a paragraph laid out
                # in a cell names it in its own text
                if cells and not _prose(cells[0]):
                    label, section = _label_and_group(block, cells[0])
                    section = section or _context(document, ref.block_id).section
                    if not _names_group(group, terms.expand(f"{label} | {section}")):
                        return False
                    continue
            scope = _context(document, ref.block_id).section
            if not _names_group(group, terms.expand(f"{scope}\n{ref.quote}")):
                return False
    return True


def _prose_cells(ref: EvidenceRef, table: SourceTable) -> list[EvidenceRef]:
    """Release text laid out inside a table (a whole paragraph in a cell) is read as text."""
    return [
        ref.model_copy(update={"cell_ids": (cell.id,), "quote": cell.text})
        for cell in table.cells
        if cell.id in ref.cell_ids and _prose(cell)
    ]


def _prose(cell: SourceCell) -> bool:
    return len(cell.text) > 40 and len(re.findall(r"[A-Za-z]{3,}", cell.text)) >= 6


# Measures that count people or things. Duolingo's daily active users and paid subscribers
# were once claimed in USD; a measure of money about them ("revenue per user", "customer
# deposits", Interactive Brokers' "customer credits", "Dasher pay") names its money and stays
# money
_COUNTED = re.compile(
    r"\b(?:users?|DAUs?|MAUs?|WAUs?|subscribers?|members?|customers?|accounts?|employees?|"
    r"headcount|vehicles?|deliveries|stores?|restaurants?|locations?|households?|merchants?|"
    r"dashers?|drivers?|couriers?|riders?|guests?|visitors?|downloads?|installs?)\b",
    re.IGNORECASE,
)
_MONEY_MEASURE = re.compile(
    r"\b(?:per|average|ARPU|ARPA|ARPPU|revenues?|sales|income|earnings|spend(?:ing)?|value|"
    r"costs?|prices?|fees?|bookings|margins?|cash|deposits?|assets|balances?|loans?|GMV|GOV|"
    r"TPV|volumes?|payments?|profits?|EBITDA|dividends?|equity|debt|capital|ARR|RPO|"
    r"billings?|orders?|backlog|expenses?|LTV|pay|wages|compensation|tips|credits?|funds?|"
    r"reserves?|AUM|claims)\b",
    re.IGNORECASE,
)


_ANNOUNCED_QUARTER = re.compile(
    r"\b(?:reports?|reported|announces?|announced)\b[^.\n]{0,40}?\b(first|second|third|fourth)"
    r"[- ]quarter\b"
)
_QUARTER_NUMBER = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _counted(metric: str) -> bool:
    return bool(_COUNTED.search(metric)) and not _MONEY_MEASURE.search(metric)


def _bind(facts: list[ReferencedFact], document: SourceDocument) -> BindingReport:
    accepted = []
    rejected = []
    offset = fiscal_offset(document.text[:20_000])
    reported = reporting_year(document.text[:20_000])
    named_period = reported_period(document.text[:20_000])
    if named_period is None and reported:
        # "Walmart reports second quarter results": the quarter announced without a year,
        # in the year the release reports
        announced = set(_ANNOUNCED_QUARTER.findall(document.text[:600].casefold()))
        if len(announced) == 1:
            named_period = (int(reported[0]), _QUARTER_NUMBER[announced.pop()])
    terms = _terms(document)
    end = terms.end
    shown_updates: set[str] = set()
    for fact in facts:
        changes: dict[str, object] = {}
        locations = []
        for field, claimed in zip(VALUE_FIELDS, fact.values(), strict=True):
            if claimed is None:
                continue
            value = _normal(claimed)
            if unit_kind(value.unit) == "money" and _counted(fact.metric):
                # Wherever its number appears, a count of people or things isn't money
                rejected.append(Rejection(fact.metric, field, ("a count is claimed as money",)))
                changes[field] = None
                continue
            failures: list[str] = []
            bound: SourcedValue | None = None
            resolvable = [ref for ref in value.evidence if not _reference_issues(ref, document)]
            if not resolvable:
                failures.append("evidence does not resolve in the document")
            for ref in resolvable:
                block = document.resolve(ref.document_sha256, ref.block_id)
                context = replace(
                    _context(document, ref.block_id),
                    offset=offset,
                    reported=reported,
                    terms=terms,
                    end=end,
                )
                if named_period and not quarter(context.far):
                    # A harmless layout table can precede the title. The release's own
                    # explicit reporting announcement supplies the missing opening scope;
                    # it never supplies missing comparative column headers.
                    year, q = named_period
                    fiscal = "FY" if reported and reported[1] else ""
                    context = replace(context, far=f"{context.far}\nQ{q} {fiscal}{year}")
                used = ref
                if isinstance(block, SourceTable):
                    issues, location, kept = _table_binding(fact, field, value, ref, block, context)
                    if kept is not None:
                        used = ref.model_copy(update={"cell_ids": (location,)})
                    for prose in _prose_cells(ref, block) if kept is None else []:
                        issues, location, kept = _paragraph_binding(
                            fact, field, value, prose, context
                        )
                        if kept is not None:
                            used, location = prose, prose.cell_ids[0]
                            break
                else:
                    issues, location, kept = _paragraph_binding(fact, field, value, ref, context)
                if kept is not None and not issues:
                    # The evidence kept is what established the value: its cell or passage
                    bound = kept.model_copy(update={"evidence": (used,)})
                    locations.append(location)
                    break
                failures.extend(issues)
            if bound is None:
                rejected.append(Rejection(fact.metric, field, tuple(dict.fromkeys(failures))))
                changes[field] = None
            else:
                changes[field] = bound
        if fact.update:
            shown, reason = _bind_update(fact, document)
            if reason:
                rejected.append(Rejection(fact.metric, "update", (reason,)))
                changes.update(update="", update_evidence=())
            elif shown in shown_updates:
                changes.update(update="", update_evidence=())  # one sentence, shown once
            else:
                shown_updates.add(shown)
                changes["update"] = shown
        kept_fact = fact.model_copy(update=changes)
        if _group(kept_fact, terms) != kept_fact.group or not _group_stated(
            kept_fact, document, terms
        ):
            # A group the evidence doesn't name is the model's label, never shown as a segment
            kept_fact = kept_fact.model_copy(update={"group": ""})
        pair_issues = _pair_issues(kept_fact)
        for field, reasons in pair_issues.items():
            rejected.append(Rejection(fact.metric, field, reasons))
        kept_fact = kept_fact.model_copy(update=dict.fromkeys(pair_issues))
        if any(kept_fact.values()) or kept_fact.update:
            accepted.append(BoundFact(kept_fact, tuple(locations)))
    return BindingReport(tuple(accepted), tuple(rejected))
