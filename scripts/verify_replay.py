"""Replay saved live summaries through the item 6 verifier, offline and without model calls.

Each fact the live checker keeps (summary_validation) is turned into the verifier's
candidate from its own fields, and offered every paragraph and table cell of the release
that holds its number. The verifier (summary_binding) keeps a value only where the release
states that measure, for that period and basis. A refusal is not proof of an error: read
the refused facts before counting them as wrong.

    uv run python scripts/verify_replay.py --data <folder of saved runs> --out replay.json

Each saved run is a folder with requests.json and raw-results.json, and the release's HTML
as html/<TICKER>.html or docs/<TICKER>.html. A release with neither, a PDF, is read from the
request's text as the live bot reads a PDF.
"""

import argparse
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from earnings_bot.binding_text import number_tokens
from earnings_bot.source_document import SourceDocument, document_from_html, document_from_text
from earnings_bot.summary_binding import bind_brief
from earnings_bot.summary_evidence import (
    EvidenceRef,
    ReferencedFact,
    SourcedValue,
    UnverifiedBrief,
)
from earnings_bot.summary_policy import SummaryFact
from earnings_bot.summary_validation import validate_facts

RUNS = ("ember-effort-2026-11-12", "ember-fresh-effort-2026-11-12")
# Wrong facts the live checker shows (docs/summaries.md): Netflix's 12% reported growth labeled
# F/X neutral, Synopsys's nine-month cash flow labeled Q3, Duolingo's users and subscribers in
# USD, and DoorDash's cash labeled year over year
KNOWN_WRONG = (
    ("NFLX-low-1", "Revenue growth"),
    ("SNPS-low-1", "Operating cash flow"),
    ("SNPS-low-2", "Operating cash flow"),
    ("DUOL-low-1", "Daily Active Users"),
    ("DUOL-low-1", "Paid Subscribers"),
    ("DUOL-low-2", "Paid Subscribers at period end"),
    ("DASH-low-2", "Cash and cash equivalents"),
)
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
    (("more than", "over ", ">", "above"), "more_than"),
    (("nearly", "almost"), "nearly"),
    (("at least",), "at_least"),
    (("less than", "under ", "below", "<"), "less_than"),
    (("up to", "at most"), "at_most"),
)
ROLES = {"headline", "driver", "guidance", "material_context", "update", "supporting"}
SLOTS = ("current_actual", "new_guidance", "reported_growth", "prior_year_actual")


def parse(text: str) -> tuple[str, str | None, str, str] | None:
    """Amount, range end, scale and qualifier of a live value such as "$6.76 billion"."""
    text = text.strip()
    lowered = text.casefold()
    qualifier = next(
        (name for words, name in _QUALIFIERS if any(w in lowered for w in words)), "exact"
    )
    found = list(_NUMBER.finditer(text))
    if not found or len(found) > 2:
        return None
    amounts, scale = [], "one"
    for index, match in enumerate(found):
        # A dash between two numbers is a range, not a minus
        negative = match["neg"] in DASHES and not (len(found) == 2 and index == 1)
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


def unit_of(fact: SummaryFact, value: str) -> str:
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


def evidence(document: SourceDocument, amount: str, scale: str, unit: str) -> list[EvidenceRef]:
    claim = abs(Decimal(amount)) * (1 if unit in ("%", "bps", "per share") else _FACTOR[scale])
    tables = {table.id: table for table in document.tables}
    refs = []
    for block in document.blocks:
        table = tables.get(block.id)
        if table is not None:
            cells = tuple(
                cell.id
                for cell in table.cells
                if not table.nested
                and not cell.header
                and any(_near(abs(t[2]), claim, unit) for t in number_tokens(cell.text))
            )
            if cells:
                quote = table.text.strip()[:200]
                refs.append(
                    EvidenceRef(
                        document_sha256=document.sha256,
                        block_id=table.id,
                        cell_ids=cells,
                        quote=quote,
                    )
                )
        elif any(_near(abs(t[2]), claim, unit) for t in number_tokens(block.text)):
            refs.append(
                EvidenceRef(
                    document_sha256=document.sha256,
                    block_id=block.id,
                    cell_ids=(),
                    quote=block.text,
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


def verdict(fact: SummaryFact, document: SourceDocument) -> dict[str, object]:
    built = candidate(fact, document)
    if built is None:
        return {"verdict": "unparsed"}
    report = bind_brief(UnverifiedBrief(schema_version="source-values/1", facts=(built,)), document)
    kept = (
        {slot: getattr(report.facts[0].fact, slot) is not None for slot in SLOTS}
        if report.facts
        else {}
    )
    claimed = [slot for slot in SLOTS if getattr(built, slot) is not None]
    main = next((s for s in ("new_guidance", "current_actual") if s in claimed), "reported_growth")
    if not kept.get(main):
        outcome = "refused"
    else:
        outcome = "confirmed" if all(kept.get(s) for s in claimed) else "main confirmed"
    reasons = {r.field: list(r.reasons) for r in report.rejected}
    row: dict[str, object] = {"verdict": outcome, "refused_fields": reasons}
    if outcome == "refused" and main in claimed:
        row["cited"] = cited_reasons(built, main, fact.source, document)
    return row


def cited_reasons(
    built: ReferencedFact, slot: str, quote: str, document: SourceDocument
) -> dict[str, object]:
    """Why the passage the live answer quoted didn't bind its main value. The refusal lists
    every reason from every passage holding the number; this one is the cause to read."""
    value = getattr(built, slot)
    wanted = " ".join(quote.split())[:80]
    best: dict[str, object] = {"passage": "", "reasons": ["the quoted passage holds no match"]}
    fewest = 10**6
    for ref in value.evidence:
        if ref.document_sha256 != document.sha256:
            continue  # the number is nowhere in the release
        block = document.resolve(ref.document_sha256, ref.block_id)
        holds = wanted and wanted in " ".join(block.text.split())
        alone = built.model_copy(update={slot: value.model_copy(update={"evidence": (ref,)})})
        report = bind_brief(
            UnverifiedBrief(schema_version="source-values/1", facts=(alone,)), document
        )
        found = [r for r in report.rejected if r.field == slot]
        issues = list(found[0].reasons) if found else []
        # The quoted passage first; otherwise the passage that came closest
        rank = (0 if holds else 1, len(issues))
        if rank < (0 if best.get("quoted") else 1, fewest):
            fewest = len(issues)
            best = {
                "quoted": bool(holds),
                "table": hasattr(block, "cells"),
                "passage": " ".join(block.text.split())[:300],
                "reasons": issues,
            }
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--data", type=Path, required=True, help="folder of saved runs")
    parser.add_argument("--effort", action="append", help="efforts to replay (default low)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    efforts = set(args.effort or ["low"])
    rows: list[dict[str, object]] = []
    documents: dict[str, SourceDocument] = {}
    for run in RUNS:
        folder = args.data / run
        requests = json.loads((folder / "requests.json").read_text(encoding="utf-8"))["requests"]
        content = {row["id"]: row["request"]["messages"][0]["content"] for row in requests}
        calls = json.loads((folder / "raw-results.json").read_text(encoding="utf-8"))["calls"]
        for call in (c for c in calls if c["effort"] in efforts):
            ticker = call["ticker"]
            release = content[call["id"]].split("<release>", 1)[1].rsplit("</release>", 1)[0]
            if ticker not in documents:
                html = next(
                    (
                        p
                        for p in (
                            folder / "html" / f"{ticker}.html",
                            folder / "docs" / f"{ticker}.html",
                        )
                        if p.exists()
                    ),
                    None,
                )
                documents[ticker] = (
                    document_from_html(html.read_text(encoding="utf-8", errors="replace"))
                    if html
                    else document_from_text(release.strip())
                )
            facts = [SummaryFact(**line) for line in call["lines"]]
            for fact in validate_facts(facts, release.strip())[0]:
                if fact.kind == "update" and not fact.value.strip():
                    continue  # a business update shows the release's own sentence
                rows.append(
                    {
                        "run": run,
                        "id": call["id"],
                        "ticker": ticker,
                        "metric": fact.metric,
                        "group": fact.group,
                        "kind": fact.kind,
                        "period": fact.period,
                        "basis": fact.basis,
                        "value": fact.value,
                        "change": fact.change,
                        "prior": fact.prior,
                        "quote": fact.source,
                    }
                    | verdict(fact, documents[ticker])
                )
    counts = Counter(str(row["verdict"]) for row in rows)
    args.out.write_text(
        json.dumps({"counts": counts, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(rows)} facts kept by the live checker: {dict(counts)}")
    for ticker in sorted(documents):
        mine = Counter(str(row["verdict"]) for row in rows if row["ticker"] == ticker)
        print(f"  {ticker:5} {dict(mine)}")
    cited = Counter(
        reason
        for row in rows
        for reason in row.get("cited", {}).get("reasons", [])  # type: ignore[union-attr]
    )
    print("Why the quoted passage refused the main value:")
    for reason, count in cited.most_common(8):
        print(f"  {count:4} {reason}")
    print("Known wrong facts (the Nov 12 study), which must stay refused:")
    for ident, metric in KNOWN_WRONG:
        found = [str(r["verdict"]) for r in rows if r["id"] == ident and r["metric"] == metric]
        status = "refused" if found and set(found) == {"refused"} else f"NOT REFUSED {found}"
        print(f"  {ident} {metric}: {status}")


if __name__ == "__main__":
    main()
