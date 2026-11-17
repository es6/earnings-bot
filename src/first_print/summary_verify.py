# ruff: noqa: RUF001
"""Verified live summaries: each fact the live answer gives, checked by the source binder.

The live writer (summary.py) asks Muse Ember for facts as text ("$6.76 billion", "+13%"), and
the quote check (summary_validation) keeps those whose numbers appear in their quoted passage.
That does not establish the measure, period or basis. Here each kept fact becomes a binder
candidate from its own fields, offered every paragraph and table cell of the release that holds
its number; the binder (summary_binding) keeps a value only where the release states that
measure, for that period and basis. A fact whose main value is not confirmed is dropped, and so
is a growth rate or prior-year value that is not. No model call: it runs on the release the bot
already has.
"""

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from first_print.binding_text import number_tokens
from first_print.source_document import SourceDocument
from first_print.summary_binding import bind_brief
from first_print.summary_evidence import (
    EvidenceRef,
    ReferencedFact,
    SourcedValue,
    UnverifiedBrief,
)
from first_print.summary_policy import SummaryFact

_SCALES = {
    "trillion": "trillion",
    "t": "trillion",
    "billion": "billion",
    "bn": "billion",
    "b": "billion",
    "million": "million",
    "mn": "million",
    "m": "million",
    "thousand": "thousand",
    "k": "thousand",
}
_FACTOR = {"one": 1, "thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_NUMBER = re.compile(
    r"(?P<neg>[-\u2212\u2013]|\()?\s*[$€£¥]?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|million|thousand|bn|mn|[tbmk](?![a-z]))?",
    re.IGNORECASE,
)
# A minus written as a hyphen, a minus sign or an en dash, or a parenthesis
DASHES = ("-", "\u2212", "\u2013", "(")
_QUALIFIERS = (
    (("approximately", "about", "~", "roughly", "around"), "approximately"),
    (("more than", "in excess of", "over ", ">", "above"), "more_than"),
    (("nearly", "almost"), "nearly"),
    (("at least",), "at_least"),
    (("less than", "under ", "below", "<"), "less_than"),
    (("up to", "at most"), "at_most"),
)
# A tolerance around one value, not a second value: NVIDIA's "$54.0 billion, plus or minus 2%"
_TOLERANCE = re.compile(
    r",?\s*(?:plus or minus|\+/-|\+/−|±)\s*[$€£¥]?\s*\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|percent\b|basis points?\b|bps?\b|trillion\b|billion\b|million\b|[tbmk](?![a-z]))?",
    re.IGNORECASE,
)
ROLES = {"headline", "driver", "guidance", "material_context", "update", "supporting"}
SLOTS = ("current_actual", "new_guidance", "reported_growth", "prior_year_actual")


def parse(text: str) -> tuple[str, str | None, str, str] | None:
    """Amount, range end, scale and qualifier of a live value such as "$6.76 billion"."""
    text = _TOLERANCE.sub("", text).strip()
    lowered = text.casefold()
    qualifier = next(
        (name for words, name in _QUALIFIERS if any(w in lowered for w in words)), "exact"
    )
    found = list(_NUMBER.finditer(text))
    if not found:
        return None
    worded = False  # the range is joined by a word, so a dash on its second end is a minus
    if len(found) > 1:
        # Two numbers are a range only when joined as one ("$4.8 - $5.0 billion", "3.0% to
        # 3.75%", "$(1.2)-$(1.3)B"); otherwise the first is the value and the rest describe it
        # ("+5.9% reported, +5.1% cc", "$2.4B (vs. $2.1B)")
        gap = text[found[0].end() : found[1].start()]
        joined = re.fullmatch(r"\s*\)?\s*%?\s*(?:to|and|–|—|-)?\s*[$€£¥]?\s*", gap) and (
            gap.strip(" %)$€£¥") or found[1]["neg"] in DASHES
        )
        worded = bool(joined) and bool(re.search(r"\b(?:to|and)\b", gap))
        found = found[:2] if joined and len(found) == 2 else found[:1]
    amounts, scale = [], "one"
    for index, match in enumerate(found):
        # A dash between two numbers is a range, not a minus; a parenthesis is always one
        negative = match["neg"] in DASHES and (
            index == 0 or len(found) == 1 or match["neg"] == "(" or worded
        )
        amounts.append(("-" if negative else "") + match["num"].replace(",", ""))
        if match["scale"]:
            scale = _SCALES[match["scale"].casefold()]
    try:
        values = sorted(Decimal(a) for a in amounts)
    except InvalidOperation:
        return None
    if len(values) == 2:
        return str(values[0]), str(values[1]), scale, qualifier
    return amounts[0], None, scale, qualifier


def _several(value: str) -> bool:
    """Whether a value states more amounts than it is read as: not one, nor one range."""
    parsed = parse(value)
    stated = len(_NUMBER.findall(_TOLERANCE.sub("", value)))
    return parsed is not None and stated > (2 if parsed[1] is not None else 1)


def unit_of(fact: SummaryFact, value: str) -> str:
    value = _TOLERANCE.sub("", value)  # "$108.0 billion, plus or minus 2%" is money
    text = f"{value} {fact.unit}".casefold()
    if "bps" in text or "basis point" in text:
        return "bps"
    if "%" in text or "percent" in text:
        return "%"
    named = f"{fact.unit} {fact.metric}".casefold()
    if re.search(r"per (?:diluted |basic )?share|/share|per ads|\beps\b", named):
        return "per share"
    if fact.currency or re.search(r"[$€£¥]", value) or re.search(r"\b(?:usd|eur|dollars?)\b", text):
        return "money"
    unit = re.sub(
        r"\b(?:thousands?|millions?|billions?|trillions?)\b", "", fact.unit, flags=re.IGNORECASE
    )
    return unit.strip() or "units"


def duration_of(period: str) -> str:
    text = period.casefold()
    for pattern, duration in (
        (r"\bq[1-4]\b|quarter", "three months"),
        (r"nine|9m|ytd", "nine months"),
        (r"\bh[12]\b|half|six months", "six months"),
        (r"\bfy|full[- ]year|year", "twelve months"),
    ):
        if re.search(pattern, text):
            return duration
    return ""


def _near(source: Decimal, claim: Decimal, unit: str) -> bool:
    """Whether a number in the release could be the claim, at any scale (the verifier decides)."""
    if unit in ("%", "bps", "per share"):
        return abs(source - claim) <= max(Decimal("0.051"), claim * Decimal("0.01"))
    return any(
        claim and abs(source * factor - claim) <= claim * Decimal("0.06")
        for factor in (1, 10**3, 10**6, 10**9, 10**12)
    )


_NUMBERS: dict[str, list[tuple[str, list[tuple[str, list[Decimal]]], list[Decimal]]]] = {}


def _numbers(
    document: SourceDocument,
) -> list[tuple[str, list[tuple[str, list[Decimal]]], list[Decimal]]]:
    """Each block's numbers, read once per release: (block id, [(cell id, amounts)] for a
    table, amounts for a passage)."""
    read = _NUMBERS.get(document.sha256)  # once: a worker thread may clear it after
    if read is None:
        tables = {table.id: table for table in document.tables}
        read = []
        for block in document.blocks:
            table = tables.get(block.id)
            if table is not None:
                cells = [
                    (cell.id, [abs(t[2]) for t in number_tokens(cell.text)])
                    for cell in table.cells
                    if not table.nested and not cell.header
                ]
                read.append((block.id, cells, []))
            else:
                read.append((block.id, [], [abs(t[2]) for t in number_tokens(block.text)]))
        if len(_NUMBERS) > 16:
            _NUMBERS.clear()
        _NUMBERS[document.sha256] = read
    return read


def evidence(document: SourceDocument, amount: str, scale: str, unit: str) -> list[EvidenceRef]:
    claim = abs(Decimal(amount)) * (1 if unit in ("%", "bps", "per share") else _FACTOR[scale])
    tables = {table.id: table for table in document.tables}
    blocks = {block.id: block for block in document.blocks}
    refs = []
    for block_id, cells, amounts in _numbers(document):
        if block_id in tables:
            held = tuple(cell for cell, found in cells if any(_near(a, claim, unit) for a in found))
            if held:
                refs.append(
                    EvidenceRef(
                        document_sha256=document.sha256,
                        block_id=block_id,
                        cell_ids=held,
                        quote=tables[block_id].text.strip()[:200],
                    )
                )
        elif any(_near(a, claim, unit) for a in amounts):
            refs.append(
                EvidenceRef(
                    document_sha256=document.sha256,
                    block_id=block_id,
                    cell_ids=(),
                    quote=blocks[block_id].text,
                )
            )
    return refs


def sourced(
    fact: SummaryFact, value: str, document: SourceDocument, comparison: str = ""
) -> SourcedValue | None:
    parsed = parse(value)
    if parsed is None:
        return None
    amount, upper, scale, qualifier = parsed
    unit = unit_of(fact, value)
    scale = "one" if unit in ("%", "bps", "per share") else scale
    refs = evidence(document, amount, scale, unit) or [
        # Nowhere in the release: the verifier refuses it as unresolvable
        EvidenceRef(document_sha256="none", block_id="none", cell_ids=(), quote="none")
    ]
    money = unit in ("money", "per share")
    return SourcedValue(
        amount=amount,
        upper=upper,
        unit=unit,
        scale=scale,  # type: ignore[arg-type]
        currency=(fact.currency or ("USD" if "$" in value else "")) if money else "",
        value_qualifier=qualifier,  # type: ignore[arg-type]
        period=fact.period,
        duration=duration_of(fact.period),
        basis=fact.basis,
        comparison=comparison,  # type: ignore[arg-type]
        comparison_qualifier="",
        evidence=tuple(refs),
    )


def candidate(fact: SummaryFact, document: SourceDocument) -> ReferencedFact | None:
    kind = fact.kind if fact.kind in ("actual", "segment", "guide", "update") else "actual"
    guide = kind == "guide"
    main = sourced(fact, fact.value, document) if fact.value.strip() else None
    stated = fact.comparison if fact.comparison in ("YoY", "QoQ", "versus prior guidance") else ""
    growth = sourced(fact, fact.change, document, stated or "YoY") if "%" in fact.change else None
    prior = sourced(fact, fact.prior, document) if fact.prior.strip() and not guide else None
    if main is None and growth is None:
        return None
    try:
        return ReferencedFact(
            metric=fact.metric,
            kind=kind,  # type: ignore[arg-type]
            role=fact.role if fact.role in ROLES else "supporting",  # type: ignore[arg-type]
            group=fact.group,
            relevance="",
            current_actual=None if guide else main,
            prior_year_actual=prior,
            reported_growth=growth,
            comparable_growth=None,
            previous_guidance=None,
            new_guidance=main if guide else None,
            update="",
            update_evidence=(),
        )
    except ValueError:
        return None


SLOTS = ("current_actual", "new_guidance", "reported_growth", "prior_year_actual")


def check(
    fact: SummaryFact, document: SourceDocument
) -> tuple[dict[str, bool], dict[str, list[str]]]:
    """Which of a fact's values the binder confirms, by slot, and its reasons for the rest. An
    empty result means the fact gave no value the binder can read."""
    built = candidate(fact, document)
    if built is None:
        return {}, {}
    report = bind_brief(UnverifiedBrief(schema_version="source-values/1", facts=(built,)), document)
    claimed = [slot for slot in SLOTS if getattr(built, slot) is not None]
    bound = report.facts[0].fact if report.facts else None
    kept = {slot: bound is not None and getattr(bound, slot) is not None for slot in claimed}
    return kept, {r.field: list(r.reasons) for r in report.rejected}


def main_slot(kept: dict[str, bool]) -> str:
    return next((s for s in ("new_guidance", "current_actual") if s in kept), "reported_growth")


def _unscaled(fact: SummaryFact, value: str) -> bool:
    """Money copied from a table without the table's scale: "$96,221" in a table "in millions"."""
    parsed = parse(value)
    return (
        parsed is not None
        and parsed[2] == "one"
        and unit_of(fact, value) == "money"
        and abs(Decimal(parsed[0])) >= 1000
    )


def _rescaled(
    fact: SummaryFact, document: SourceDocument
) -> tuple[SummaryFact, dict[str, bool], dict[str, list[str]]] | None:
    """The fact with its table's scale written in, if the binder confirms its main value at that
    scale. The binder checks the scale against the table's own caption or header, so a scale the
    table doesn't state can't pass."""
    for scale in ("million", "thousand", "billion"):
        trial = replace(
            fact,
            value=f"{fact.value.strip()} {scale}",
            prior=f"{fact.prior.strip()} {scale}" if _unscaled(fact, fact.prior) else fact.prior,
        )
        kept, reasons = check(trial, document)
        if kept.get(main_slot(kept)):
            return trial, kept, reasons
    return None


def verify_facts(
    facts: list[SummaryFact], document: SourceDocument
) -> tuple[list[SummaryFact], list[str]]:
    """The facts whose main value the release confirms, without any growth rate or prior-year
    value it doesn't; and a reason for each fact or part dropped. A business update with no
    value shows the release's own sentence, which the quote check has already matched."""
    shown: list[SummaryFact] = []
    dropped: list[str] = []
    for given in facts:
        fact = given  # replaced below if it's confirmed at its table's scale
        if fact.kind == "update" and not fact.value.strip():
            shown.append(fact)
            continue
        if _several(fact.value):
            # "Total Customer Group $6.56B; Creative & Marketing Professionals $4.65B; ...":
            # only the first amount could be checked, and the rest would show unchecked
            dropped.append(f"{fact.metric}: several values in one fact")
            continue
        kept, reasons = check(fact, document)
        if (
            not kept.get(main_slot(kept))
            and _unscaled(fact, fact.value)
            and (rescaled := _rescaled(fact, document))
        ):
            fact, kept, reasons = rescaled
        if not kept:
            if not re.search(r"\d", f"{fact.value} {fact.change} {fact.prior}"):
                # No number to mislabel ("low single digit range", a strategy update): the
                # quote check has matched its words in the release
                shown.append(fact)
            else:
                dropped.append(f"{fact.metric}: no value the verifier can read")
            continue
        main = main_slot(kept)
        if not kept.get(main):
            dropped.append(f"{fact.metric}: {'; '.join(reasons.get(main, ['not confirmed']))}")
            continue
        trimmed = fact
        if "reported_growth" in kept and not kept["reported_growth"] and main != "reported_growth":
            trimmed = replace(trimmed, change="", comparison="")
            dropped.append(f"{fact.metric} growth: {'; '.join(reasons.get('reported_growth', []))}")
        if "prior_year_actual" in kept and not kept["prior_year_actual"]:
            trimmed = replace(trimmed, prior="")
            dropped.append(
                f"{fact.metric} prior year: {'; '.join(reasons.get('prior_year_actual', []))}"
            )
        shown.append(trimmed)
    return shown, dropped
