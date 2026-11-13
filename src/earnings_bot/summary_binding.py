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
from decimal import Decimal

from pydantic import ValidationError

from earnings_bot.binding_text import (
    MEASURE_STEMS,
    basis_of,
    claimed_amounts,
    count_words,
    currencies,
    duration,
    fiscal_offset,
    matches,
    metric_words,
    normalized,
    number_tokens,
    period_issues,
    qualifier_issues,
    quarter,
    reporting_year,
    source_scale,
    stem,
    stems,
    unit_kind,
    words,
    years,
)
from earnings_bot.sec_results import reported_period
from earnings_bot.source_document import SourceCell, SourceDocument, SourceTable
from earnings_bot.summary_evidence import (
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
_GUIDANCE = re.compile(
    r"guidance|outlook|forecast|\bexpects?\b|\bexpected\b|\btargets?\b|\banticipat|"
    r"\bprojects?\b|\bwill be\b|\bestimat|\bplans? (?:to|for)\b",
    re.IGNORECASE,
)
_DECREASE = re.compile(r"\b(?:decrease|decreased|decline|declined|down|lower|fell|drop|dropped)\b")
_PREVIOUS = re.compile(r"previous|prior (?:guidance|outlook)|old guidance|initial", re.IGNORECASE)


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
    r"\bin (?:USD |US\$ )?(?:millions|thousands|billions)|targets?|outlook|guidance"
    r"|(?:USD|EUR|GBP|CAD|RMB)\s+(?:millions|thousands|billions)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Context:
    near: str  # the text just before the cited block, since the previous table
    far: str  # the release's opening, which names its reporting period
    offset: int = 0  # how far the release's fiscal year label runs ahead of the calendar
    reported: tuple[str, bool] | None = None  # the year the release reports, and if fiscal

    def __str__(self) -> str:
        return f"{self.near}\n{self.far}"


def _context(document: SourceDocument, block_id: str) -> _Context:
    """The text that frames a block: what comes just before it, and the release's opening.
    Never other tables' rows."""
    opening: list[str] = []
    local: list[str] = []
    passed_table = False
    for block in document.blocks:
        if block.id == block_id:
            break
        if block.kind == "table":
            passed_table = True
            local.clear()
        elif passed_table:
            local.append(block.text)
        else:
            opening.append(block.text)
            local.append(block.text)
    # A bullet's period can be in a heading several paragraphs up
    near = [line for line in local[-10:] if _METADATA.search(line)][-2:]
    far = [line for line in opening[:12] if _METADATA.search(line)]
    return _Context("\n".join(near), "\n".join(far))


# Words after a growth rate that say what it compares, not what it measures
_COMPARED = r"(?:year[- ]over[- ]year|yoy|y/y|growth|increase|decrease|sequentially|qoq|q/q)"


def _numeric_text(text: str) -> bool:
    text = re.sub(
        r"^(?:nearly|almost|about|approximately|roughly|around|more than|at least|"
        r"less than|up to|at most|over|under|>=|<=|>|<|~)\s*",
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
    headers = []
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
            )
        )
    return " ".join(headers)


def _generic_heading(text: str) -> bool:
    """A table's corner cell that names no segment: "Metric", "($ in millions)"."""
    plain = words(text)
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
    for row in range(value.row):
        cells = [c for c in _row(table, row) if c.text.strip()]
        if not cells or cells[0].column != 0 or _numeric(cells[0]):
            continue
        if len(cells) == 1 and cells[0].colspan == 1:
            section = cells[0].text
        elif (
            not _generic_heading(cells[0].text)
            and not any(_numeric(c) and not c.header for c in cells[1:])
            and any(
                re.search(r"\bQ[1-4]\b|\bFY|20\d{2}|months|quarter", c.text, re.IGNORECASE)
                for c in cells[1:]
            )
        ):
            # A header row's first cell titles the table: "Walmart U.S. | Q2 FY27 | Q2 FY26"
            section = cells[0].text
    return label, section


def _group(fact: ReferencedFact) -> str:
    return "" if words(fact.group) in _NEUTRAL_GROUPS else fact.group


def _unit_note(note: str, unit: str) -> bool:
    """A label's parenthetical that only states units: "(in millions)", "(square feet)"."""
    text = words(note)
    counted = set(count_words(unit)) if unit_kind(unit) == "count" else set()
    return bool(
        re.fullmatch(
            r"(?:in )?(?:usd |us |eur )?(?:thousands?|millions?|billions?|percent|dollars|"
            r"unaudited)(?: of (?:us )?dollars)?(?: except per share(?: data| amounts)?)?",
            text,
        )
        or (counted and set(text.split()) <= counted | {"in", "of", "thousands", "millions"})
    )


def _identity_issues(claim: str, label: str, group: str, unit: str = "") -> list[str]:
    """A row names the claimed measure when they differ by at most generic words; a row that
    is a narrower or different measure (a margin, subscription revenue) does not bind. A
    label's own unit note is not part of its name."""
    label = re.sub(r"\([^()]*\)", lambda m: "" if _unit_note(m[0], unit) else m[0], label)
    claimed = set(metric_words(claim)) - set(metric_words(group))
    named = set(metric_words(label)) - set(metric_words(group))
    if not claimed:
        return ["metric names no measure"]
    generic = {stem("consolidated"), "group"}
    if "eps" in claimed:
        generic |= {stem("diluted"), stem("attributable")}
    if claimed - named - {stem("diluted")} or named - claimed - generic:
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


def _slot_issues(field: str, value: SourcedValue, local: str, metric: str = "") -> list[str]:
    guidance = bool(_GUIDANCE.search(local))
    old = bool(_PREVIOUS.search(local))
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
        growth and unit_kind(value.unit) in ("%", "bps") and "margin" not in local.lower()
    ):
        issues.append("actual is attached to a guidance/growth column")
    return issues


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
    if value.comparison_qualifier and words(value.comparison_qualifier) not in words(text):
        if field == "comparable_growth":
            return ["comparison qualifier is not established"], value
        value = value.model_copy(update={"comparison_qualifier": ""})
    return [], value


def _normal(value: SourcedValue) -> SourcedValue:
    """Model metadata reduced to what binding checks: a basis tag only when it is a basis, a
    comparison qualifier the model put in `basis`."""
    qualifier = value.comparison_qualifier
    if not qualifier and re.search(
        r"constant currency|organic|ex-?\s?acquisitions?|excluding|fx[- ]neutral|"
        r"currency[- ]neutral|comparable",
        value.basis,
        re.IGNORECASE,
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
    group = _group(fact)
    context = str(frame)
    candidates: list[tuple[str, SourcedValue]] = []
    failures: list[str] = []
    for cell in table.cells:
        if cell.id not in ref.cell_ids or cell.header or not _numeric(cell):
            continue
        prefix, body = _prefixed(cell)
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
        label, section = _label_and_group(table, cell)
        headers = f"{_headers(table, cell)} {prefix}".strip()
        issues = _identity_issues(fact.metric, label, group, value.unit)
        if field not in ("previous_guidance", "new_guidance", "prior_year_actual") and (
            _guidance_heading(frame.near)
        ):
            issues.append("actual is inside an explicit guidance section")
        sectioned = bool(words(section)) and words(section) not in _NEUTRAL_GROUPS
        if (
            group
            and not (words(group) in words(section) or words(group) in words(label))
            and (sectioned or not _names_itself(fact, group, label))
        ):
            issues.append("segment/group is not established by the source row")
        elif (
            not group
            and words(section)
            and words(section) not in _NEUTRAL_GROUPS
            and not set(metric_words(section)) <= set(metric_words(fact.metric))
        ):
            issues.append("the row belongs to a section the claim doesn't name")
        per_share = kind == "per share" or bool(re.search(r"per share|\beps\b", normalized(label)))
        caption = source_scale(f"{label} {headers}", per_share)
        if caption is None and kind == "money":
            caption = source_scale(context) or source_scale(table.text[:400])
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
        if len(found) != len(claims) or not all(
            matches(claim, token[2], token[3]) for claim, token in zip(claims, found, strict=False)
        ):
            issues.append("value/sign/scale/unit does not match the cited cell")
        if re.search(r"per share|\beps\b", normalized(label)) and kind != "per share":
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
        comparative = bool(others) and not years(_headers(table, cell)) and not change_column
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
        status = headers
        if field in ("previous_guidance", "new_guidance") and not _GUIDANCE.search(headers):
            status = f"{headers} {frame.near}"
        issues.extend(_slot_issues(field, value, status, fact.metric))
        shown = value
        if field in ("reported_growth", "comparable_growth"):
            compared, shown = _comparison(field, value, f"{headers} {label} {raw} {context}")
            issues.extend(compared)
        currency_issues, currency = (
            _currency_issues(value, f"{raw} {label} {headers}", f"{context} {table.text[:400]}")
            if kind in ("money", "per share")
            else ([], "")
        )
        issues.extend(currency_issues)
        _, unit = _unit_issues(value, f"{fact.metric} {label} {headers} {context}")
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


_CLAUSES = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(\"“•])|;\s*|\n+|\s+•\s+")


def _metric_window(claim: list[str], prefix: str) -> tuple[int, int] | None:
    """The last place before a number where the claimed measure is named: all its words,
    in order, within a few words of each other."""
    tokens = [(m.start(), m.end(), m[0]) for m in re.finditer(r"[a-z0-9&]+", prefix)]
    names = [stem(t[2]) for t in tokens]
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


def _names_itself(fact: ReferencedFact, group: str, label: str) -> bool:
    """A metric that names its own segment ("Business Professionals & Consumers subscription
    revenue") needs no separate group: the row or clause that names it establishes it."""
    specific = set(metric_words(fact.metric)) - set(metric_words(group)) - MEASURE_STEMS
    specific -= {stem(w) for w in ("net", "total", "diluted", "basic", "subscription", "operating")}
    return bool(specific) and specific <= set(metric_words(label))


_MODIFIER_STEMS = frozenset(stem(w) for w in _MODIFIERS)
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
    group = _group(fact)
    context = str(frame)
    claim = [w for w in metric_words(fact.metric) if w not in set(metric_words(group))] or (
        metric_words(fact.metric)
    )
    if field in ("reported_growth", "comparable_growth"):
        # The slot says it is a growth rate: "ARR growth" is named by "ARR grew"
        claim = [w for w in claim if w not in _CHANGE_STEMS] or claim
    failures: list[str] = []
    for clause in _CLAUSES.split(ref.quote):
        text = normalized(clause)
        caption = source_scale(clause) or (
            source_scale(context)
            if kind == "money" and not re.search(r"billion|million", text)
            else None
        )
        tokens = number_tokens(text, "" if kind in ("%", "bps") else (caption or ""))
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
                r"\s*(?:to|-|–|—|and)\s*[$€£¥]?\s*", text[run[0][1] : run[1][0]]
            ):
                continue
            issues: list[str] = []
            prefix = text[: token[0]]
            window = _metric_window(claim, prefix)
            if window is None and (
                kind == "count" or (kind == "%" and re.match(r"\s*of\b", text[run[-1][1] :]))
            ):
                # A count can name what it counts after its number ("repurchased 9.5 million
                # shares", "1 billion monthly active users"), and a share what it is a share of
                # ("represented 38.8% of total food and beverage revenue")
                following_words = stems(text[run[-1][1] :])[:7]
                preceding_words = stems(prefix)[-6:]
                if set(claim) & set(following_words) and set(claim) <= set(following_words) | set(
                    preceding_words
                ):
                    window = (token[0], token[0])
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
            issues.extend(_modifier_issues(prefix, window[0], claim))
            issues.extend(_positional_issues(text, tokens, index, len(claims), dimension))
            after_end = run[-1][1]
            following = (
                tokens[index + len(claims)][0] if index + len(claims) < len(tokens) else len(text)
            )
            after = re.split(r"\band\b|,|;", text[after_end:following], maxsplit=1)[0]
            near = " ".join(re.findall(r"[a-z-]+", prefix[: window[0]])[-2:])
            local = f"{near} {text[window[0] : token[0]]}"
            basis_source = (
                after if basis_of(after) else re.sub(r"[$€£¥]?\(?-?\d[\d,.]*.*$", "", local)
            )
            basis_issues, basis = _basis_issues(fact, value, basis_source)
            issues.extend(basis_issues)
            if group and words(group) not in words(text) and not _names_itself(fact, group, text):
                issues.append("segment/group is absent from the claim clause")
            issues.extend(qualifier_issues(value, text[max(0, token[0] - 20) : after_end]))
            span = f"{local} {text[token[0] : after_end]} {after}"
            growth_slot = field in ("reported_growth", "comparable_growth")
            if growth_slot:
                span = f"{span} {text[after_end:following]}"
            elif field in ("previous_guidance", "new_guidance") and not _GUIDANCE.search(span):
                span = f"{span} {frame.near}"  # an "Outlook" heading above the bullet
            issues.extend(_slot_issues(field, value, span, fact.metric))
            if field not in ("previous_guidance", "new_guidance", "prior_year_actual") and (
                _guidance_heading(frame.near)
            ):
                issues.append("actual is inside an explicit guidance section")
            shown = value
            if growth_slot:
                compared, shown = _comparison(field, value, f"{text[after_end:following]} {text}")
                issues.extend(compared)
            issues.extend(
                period_issues(
                    value,
                    text,
                    f"{ref.quote}\n{frame.near}",
                    frame.far,
                    comparative=False,
                    clause=True,
                    prior=field == "prior_year_actual",
                    offset=frame.offset,
                    reported=frame.reported,
                    change=growth_slot,
                    guide=field in ("previous_guidance", "new_guidance"),
                )
            )
            currency_issues, currency = (
                _currency_issues(value, clause, context)
                if kind in ("money", "per share")
                else ([], "")
            )
            issues.extend(currency_issues)
            _, unit = _unit_issues(value, f"{fact.metric} {text}")
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


def _group_stated(fact: ReferencedFact, document: SourceDocument) -> bool:
    """Whether the evidence of every kept value names the fact's group: in its passage, or
    for a cell in the row's label or the section above it."""
    group = words(fact.group)
    if not group:
        return True
    for value in fact.values():
        if value is None:
            continue
        for ref in value.evidence:
            block = document.resolve(ref.document_sha256, ref.block_id)
            if isinstance(block, SourceTable):
                cells = [c for c in block.cells if c.id in ref.cell_ids]
                if cells and not ref.quote.startswith(cells[0].text):
                    label, section = _label_and_group(block, cells[0])
                    if group not in words(f"{label} {section}"):
                        return False
                    continue
            if group not in words(ref.quote):
                return False
    return True


def _prose_cells(ref: EvidenceRef, table: SourceTable) -> list[EvidenceRef]:
    """Release text laid out inside a table (a whole paragraph in a cell) is read as text."""
    return [
        ref.model_copy(update={"cell_ids": (cell.id,), "quote": cell.text})
        for cell in table.cells
        if cell.id in ref.cell_ids
        and len(cell.text) > 40
        and len(re.findall(r"[A-Za-z]{3,}", cell.text)) >= 6
    ]


def _bind(facts: list[ReferencedFact], document: SourceDocument) -> BindingReport:
    accepted = []
    rejected = []
    offset = fiscal_offset(document.text[:20_000])
    reported = reporting_year(document.text[:20_000])
    named_period = reported_period(document.text[:20_000])
    shown_updates: set[str] = set()
    for fact in facts:
        changes: dict[str, object] = {}
        locations = []
        for field, claimed in zip(VALUE_FIELDS, fact.values(), strict=True):
            if claimed is None:
                continue
            value = _normal(claimed)
            failures: list[str] = []
            bound: SourcedValue | None = None
            resolvable = [ref for ref in value.evidence if not _reference_issues(ref, document)]
            if not resolvable:
                failures.append("evidence does not resolve in the document")
            for ref in resolvable:
                block = document.resolve(ref.document_sha256, ref.block_id)
                context = replace(
                    _context(document, ref.block_id), offset=offset, reported=reported
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
        if _group(kept_fact) != kept_fact.group or not _group_stated(kept_fact, document):
            # A group the evidence doesn't name is the model's label, never shown as a segment
            kept_fact = kept_fact.model_copy(update={"group": ""})
        pair_issues = _pair_issues(kept_fact)
        for field, reasons in pair_issues.items():
            rejected.append(Rejection(fact.metric, field, reasons))
        kept_fact = kept_fact.model_copy(update=dict.fromkeys(pair_issues))
        if any(kept_fact.values()) or kept_fact.update:
            accepted.append(BoundFact(kept_fact, tuple(locations)))
    return BindingReport(tuple(accepted), tuple(rejected))
