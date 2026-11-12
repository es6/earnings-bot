"""Offline replay of saved summary experiments. No providers, credentials or network."""

import re
from dataclasses import dataclass
from typing import Any

from earnings_bot.summary_format import render_facts
from earnings_bot.summary_policy import SummaryFact, evidence_issues, normalized

EVALUATOR_VERSION = "2026-11-11.3"

_ADJUSTED = re.compile(r"\badj(?:usted)?\b|non[- ]gaap", re.IGNORECASE)
_RATIO = re.compile(r"\bmargin\b|\bratio\b", re.IGNORECASE)
_TOTALS = {
    "revenue",
    "revenues",
    "total revenue",
    "total revenues",
    "net revenue",
    "net revenues",
    "net sales",
    "total net sales",
    "sales",
}


def metric_words(text: str) -> str:
    """Ignore presentation punctuation and redundant accounting labels, not meaning.

    For example, 'Adjusted (non-GAAP) operating earnings' and 'adjusted operating
    earnings' identify the same measure. Keep margin, segment and exclusion words.
    """
    words = re.sub(r"[\W_]+", " ", normalized(text)).strip()
    words = re.sub(r"\bnon gaap\b|\badj\b", "adjusted", words)
    return re.sub(r"\badjusted(?:\s+adjusted)+\b", "adjusted", words)


def metric_match(alias: str, metric: str) -> bool:
    """Conservative editorial matching, never a claim of numeric/factual correctness.

    Keep aliases explicit in gold annotations rather than fuzzy-token matching a profit
    amount to its margin or an adjusted measure to a reported one.
    """
    alias, metric = metric_words(alias), metric_words(metric)
    if not alias:
        return False
    if bool(_ADJUSTED.search(alias)) != bool(_ADJUSTED.search(metric)):
        return False
    if bool(_RATIO.search(alias)) != bool(_RATIO.search(metric)):
        return False
    if alias in _TOTALS:
        # A segment's revenue is not coverage of consolidated revenue.
        clean = re.sub(
            r"\b(?:q[1-4]|[1-4]q)(?:\s+\d{2,4})?\b|"
            r"\b(?:fy\s*\d{2,4}|20\d\d|outlook|guidance|guide)\b",
            "",
            metric,
        )
        return normalized(clean) in _TOTALS
    return bool(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", metric))


@dataclass(frozen=True)
class Coverage:
    covered: tuple[str, ...]
    missing: tuple[str, ...]
    unassigned: tuple[str, ...]


def coverage(lines: list[dict[str, Any]], gold: list[list[Any]]) -> Coverage:
    """One line per annotated item. Coverage measures selection, not value correctness."""
    taken: set[int] = set()
    unassigned = []
    for line in lines:
        kind = line.get("kind", "actual")
        kind = "actual" if kind == "segment" else kind
        metric = line["metric"]
        words = f"{metric} {line.get('value', '')}"
        matches = []
        for index, (_, aliases, expected_kind) in enumerate(gold):
            if index in taken or kind != expected_kind:
                continue
            for alias in aliases:
                match = metric_match(alias, metric)
                if line.get("kind") == "segment" and normalized(alias) in _TOTALS:
                    match = False
                if kind == "update":
                    match = bool(alias.strip()) and normalized(alias) in normalized(words)
                if match:
                    matches.append((len(alias), index))
        if matches:
            taken.add(max(matches)[1])
        else:
            unassigned.append(metric)
    return Coverage(
        tuple(row[0] for index, row in enumerate(gold) if index in taken),
        tuple(row[0] for index, row in enumerate(gold) if index not in taken),
        tuple(unassigned),
    )


def replay_row(row: dict[str, Any], gold: dict[str, Any], document: str) -> dict[str, Any]:
    lines = row["lines"]
    selected = coverage(lines, gold["must"])
    issues = []
    for index, line in enumerate(lines):
        fact = SummaryFact(
            **{
                name: line.get(name, "actual" if name == "kind" else "")
                for name in SummaryFact.__dataclass_fields__
            }
        )
        issues.extend({"line": index, "issue": issue} for issue in evidence_issues(fact, document))
    return {
        "ticker": row["ticker"],
        "run": row.get("run", 1),
        "covered": selected.covered,
        "missing": selected.missing,
        "unassigned": selected.unassigned,
        "issues": issues,
        "lines": len(lines),
        "editorial_coverage": len(selected.covered) / max(len(gold["must"]), 1),
        # Cached provider timings/costs only. Never present them as a new benchmark.
        "historical_seconds": row.get("total"),
        "historical_cost": row.get("cost"),
    }


def common_cases(
    results: dict[str, list[dict[str, Any]]], models: list[str]
) -> set[tuple[str, int]]:
    """Compare the same issuer/run pairs; expose omitted cases in the report."""
    if not models:
        raise ValueError("Select at least one model")
    cases = [{(r["ticker"], r.get("run", 1)) for r in results[model]} for model in models]
    return set.intersection(*cases)


def display_lines(ticker: str, lines: list[dict[str, Any]]) -> str:
    """Same formatter for every model; preserves all selected facts without a length cap."""
    facts = [
        SummaryFact(
            **{
                name: line.get(name, "actual" if name == "kind" else "")
                for name in SummaryFact.__dataclass_fields__
            }
        )
        for line in lines
    ]
    return render_facts(ticker, facts)
