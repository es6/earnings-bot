"""Fail-closed source binding; a number elsewhere in a quote is never enough."""

import re
from dataclasses import dataclass

from earnings_bot.binding_text import (
    duration,
    metadata_issues,
    metric_key,
    numeric_match,
    qualifier_issues,
    quarter,
    source_scale,
    words,
)
from earnings_bot.source_document import SourceCell, SourceDocument, SourceTable
from earnings_bot.summary_evidence import (
    VALUE_FIELDS,
    EvidenceRef,
    ReferencedFact,
    SourcedValue,
    UnverifiedBrief,
    parse_referenced_brief,
)

_ATOM = r"[$€£¥(+-]*\s*\d[\d,.]*\s*\)?\s*(?:%|bps|thousand|million|billion|[KMBT])?"
_NUMERIC = re.compile(rf"{_ATOM}(?:\s*(?:to|[-\u2013\u2014])\s*{_ATOM})?", re.IGNORECASE)
_DECORATION = {"", "$", "€", "£", "¥", "(", ")", "%", "bps"}


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


def _context(document: SourceDocument, block_id: str) -> str:
    """Only the preamble and the local text since the preceding table; never other rows."""
    prefix: list[str] = []
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
            prefix.append(block.text)
    metadata = [
        line
        for line in prefix + local
        if re.search(
            r"quarter|\bQ[1-4]\b|\bFY\s*\d|fiscal year|full.year|months ended|"
            r"\bin (?:USD |US\$ )?(?:millions|thousands|billions)"
            r"|(?:USD|EUR|GBP|CAD|RMB)\s+(?:millions|thousands|billions)?",
            line,
            re.IGNORECASE,
        )
    ]
    return "\n".join(metadata)


def _numeric(cell: SourceCell) -> bool:
    text = re.sub(
        r"^(?:nearly|almost|about|approximately|roughly|around|more than|at least|"
        r"less than|up to|at most|over|under|>=|<=|>|<)\s*",
        "",
        cell.text.strip(),
        flags=re.IGNORECASE,
    )
    return bool(_NUMERIC.fullmatch(text))


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


def _label_and_group(table: SourceTable, value: SourceCell) -> tuple[str, str]:
    label = " ".join(
        c.text
        for c in _row(table, value.row)
        if c.column < value.column and c.text.strip() not in _DECORATION and not _numeric(c)
    )
    section = ""
    for row in range(value.row):
        cells = [c for c in _row(table, row) if c.text.strip()]
        if (
            len(cells) == 1
            and cells[0].column == 0
            and cells[0].colspan == 1
            and not _numeric(cells[0])
        ):
            section = cells[0].text
    return label, section


def _slot_issues(field: str, value: SourcedValue, text: str) -> list[str]:
    guidance = bool(re.search(r"guidance|outlook|forecast|expects?|expected", text, re.IGNORECASE))
    old = bool(re.search(r"previous|prior guidance|old guidance", text, re.IGNORECASE))
    growth = bool(
        re.search(
            r"growth|change|year.over.year|yoy|qoq|increas|decreas|grew|\bup\b|\bdown\b",
            text,
            re.IGNORECASE,
        )
    )
    issues = []
    if field in ("previous_guidance", "new_guidance"):
        if not guidance or (field == "previous_guidance") != old:
            issues.append("guidance status is not established by its source column")
    elif field in ("reported_growth", "comparable_growth"):
        if not growth or not value.comparison:
            issues.append("reported growth/comparison is not established")
        if field == "comparable_growth" and not value.comparison_qualifier:
            issues.append("comparable growth needs its source exclusions")
    elif guidance or (growth and value.unit in ("%", "bps")):
        issues.append("actual is attached to a guidance/growth column")
    return issues


def _table_binding(
    fact: ReferencedFact,
    field: str,
    value: SourcedValue,
    ref: EvidenceRef,
    table: SourceTable,
    context: str,
) -> tuple[list[str], str]:
    candidates = []
    failures = []
    for cell in table.cells:
        if cell.id not in ref.cell_ids or cell.header or not _numeric(cell):
            continue
        row = _row(table, cell.row)
        index = row.index(cell)
        left = index
        right = index + 1
        while left and row[left - 1].text.strip() in _DECORATION:
            left -= 1
        while right < len(row) and row[right].text.strip() in _DECORATION:
            right += 1
        raw = " ".join(c.text for c in row[left:right])
        label, group = _label_and_group(table, cell)
        headers = _headers(table, cell)
        claimed_label = fact.metric
        if fact.group and words(fact.group) in words(label):
            label = re.sub(re.escape(fact.group), "", label, flags=re.IGNORECASE).strip(" :·-")
        issues = []
        if len([c for c in row if _numeric(c)]) > 1 and not re.search(r"20\d{2}", headers):
            issues.append("comparative values need a period on the source column")
        metric_label = re.sub(rf"\(\s*{re.escape(value.unit)}\s*\)", "", label, flags=re.IGNORECASE)
        if metric_key(metric_label) != metric_key(claimed_label):
            issues.append("metric does not match the cited source row")
        if group and words(group) != words(fact.group):
            issues.append("segment/group does not match the source section")
        elif (
            fact.group
            and not group
            and words(fact.group) not in words(_label_and_group(table, cell)[0])
        ):
            issues.append("segment/group is not established by the source row")
        per_share = "per share" in words(label) or "eps" in words(label)
        monetary = words(value.unit) in ("money", "per share", "money per share")
        caption = source_scale(f"{label} {headers}", per_share)
        if caption is None and monetary:
            caption = source_scale(context)
        if caption == "ambiguous":
            issues.append("source scale is ambiguous")
        numeric_source = raw
        if (
            value.unit in ("%", "bps")
            and re.search(r"%|percent|bps|basis points", f"{label} {headers}", re.IGNORECASE)
            and not re.search(r"%|bps", raw, re.IGNORECASE)
        ):
            numeric_source += value.unit
        if not numeric_match(value, numeric_source, caption or ""):
            issues.append("value/sign/scale/unit does not match the cited cell")
        issues.extend(metadata_issues(value, label + " " + raw, headers, context))
        issues.extend(qualifier_issues(value, raw))
        issues.extend(_slot_issues(field, value, headers))
        if ("per share" in words(label) or "eps" in words(label)) and "per share" not in words(
            value.unit
        ):
            issues.append("per-share unit is missing")
        if not issues:
            candidates.append(cell.id)
        failures.extend(issues)
    if len(candidates) != 1:
        return list(dict.fromkeys(failures)) or ["value-cell binding is missing or ambiguous"], ""
    return [], candidates[0]


def _paragraph_binding(
    fact: ReferencedFact,
    field: str,
    value: SourcedValue,
    ref: EvidenceRef,
    context: str,
) -> tuple[list[str], str]:
    quote = ref.quote
    metric = re.escape(fact.metric).replace(r"\ ", r"\s+")
    match = re.search(rf"\b{metric}\b", quote, re.IGNORECASE)
    if not match:
        return ["metric is absent from the cited claim clause"], ""
    tail = re.split(r";|,|\band\b", quote[match.end() :], maxsplit=1, flags=re.IGNORECASE)[0]
    if not re.match(
        r"\s*(?:was\b|were\b|is\b|are\b|grew\b|increas|decreas|declin|total|reached\b|rose\b|fell\b|of\b|at\b|[:=$€£¥(\d+-])",
        tail,
        re.IGNORECASE,
    ):
        return ["metric phrase is part of a different measure"], ""
    caption = source_scale(quote, "per share" in words(fact.metric) or "eps" in words(fact.metric))
    issues = []
    if not numeric_match(value, tail, caption or ""):
        issues.append("value/sign/scale/unit is not bound to this metric clause")
    issues.extend(metadata_issues(value, quote, quote, context))
    issues.extend(qualifier_issues(value, quote))
    issues.extend(_slot_issues(field, value, quote))
    if fact.group and words(fact.group) not in words(quote):
        issues.append("segment/group is absent from the claim clause")
    return issues, ref.block_id


def _pair_issues(fact: ReferencedFact) -> dict[str, tuple[str, ...]]:
    issues = {}
    current = fact.current_actual
    prior = fact.prior_year_actual
    if current and prior:
        current_year = re.findall(r"20\d{2}", current.period)
        prior_year = re.findall(r"20\d{2}", prior.period)
        if (
            len(current_year) != 1
            or len(prior_year) != 1
            or int(current_year[0]) != int(prior_year[0]) + 1
            or quarter(current.period) != quarter(prior.period)
            or any(
                words(getattr(current, key)) != words(getattr(prior, key))
                for key in ("unit", "currency", "basis", "comparison_qualifier")
            )
            or duration(current.duration) != duration(prior.duration)
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
                (quarter(change.period), re.findall(r"20\d{2}", change.period))
                != (quarter(current.period), re.findall(r"20\d{2}", current.period))
                or duration(change.duration) != duration(current.duration)
                or words(change.basis) != words(current.basis)
            )
        ):
            issues[field] = ("growth is for a different reporting period or accounting basis",)
    if fact.previous_guidance and fact.new_guidance:
        old, new = fact.previous_guidance, fact.new_guidance
        if any(
            getattr(old, key) != getattr(new, key)
            for key in ("period", "duration", "unit", "currency", "basis")
        ):
            issues["previous_guidance"] = (
                "previous guidance is not comparable to the new guidance",
            )
    return issues


def bind_brief(candidate: UnverifiedBrief, document: SourceDocument) -> BindingReport:
    candidate = parse_referenced_brief(candidate.model_dump_json(), document)
    accepted = []
    rejected = []
    for fact in candidate.facts:
        changes: dict[str, object] = {}
        locations = []
        for field, value in zip(VALUE_FIELDS, fact.values(), strict=True):
            if value is None:
                continue
            failures = []
            matches = []
            for ref in value.evidence:
                block = document.resolve(ref.document_sha256, ref.block_id)
                context = _context(document, ref.block_id)
                if isinstance(block, SourceTable):
                    issues, location = _table_binding(fact, field, value, ref, block, context)
                else:
                    issues, location = _paragraph_binding(fact, field, value, ref, context)
                failures.extend(issues)
                if not issues:
                    matches.append(location)
            if failures or not matches:
                rejected.append(Rejection(fact.metric, field, tuple(dict.fromkeys(failures))))
                changes[field] = None
            else:
                locations.extend(matches)
        if fact.update:
            quotes = " ".join(ref.quote for ref in fact.update_evidence)
            update = " ".join(fact.update.casefold().split())
            if update not in " ".join(quotes.casefold().split()) or (
                fact.group and words(fact.group) not in words(fact.update)
            ):
                rejected.append(
                    Rejection(fact.metric, "update", ("update is not an exact supported clause",))
                )
                changes.update(update="", update_evidence=())
        kept = fact.model_copy(update=changes)
        pair_issues = _pair_issues(kept)
        for field, reasons in pair_issues.items():
            rejected.append(Rejection(fact.metric, field, reasons))
        kept = kept.model_copy(update=dict.fromkeys(pair_issues))
        if any(kept.values()) or kept.update:
            accepted.append(BoundFact(kept, tuple(locations)))
    return BindingReport(tuple(accepted), tuple(rejected))
