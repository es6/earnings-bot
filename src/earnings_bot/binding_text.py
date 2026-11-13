"""Source-bound identities and metadata. Unknown or conflicting context stays unknown."""

import re
from decimal import Decimal

from earnings_bot.summary_evidence import SourcedValue
from earnings_bot.summary_validation import _duration, _numbers

SCALES = {
    "one": Decimal(1),
    "thousand": Decimal(1000),
    "million": Decimal(10**6),
    "billion": Decimal(10**9),
    "trillion": Decimal(10**12),
}
_QUARTERS = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
_SCALE = re.compile(
    r"\b(?:in|USD|US\$|EUR|GBP|CAD|RMB)\s*\$?\s*(thousands|millions|billions)\b"
    r"|\((thousands|millions|billions)(?: of dollars)?\)",
    re.IGNORECASE,
)
_BASIS = re.compile(r"non[- ]gaap|\badjusted\b|\bgaap\b|\bifrs\b", re.IGNORECASE)


def words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def metric_key(text: str) -> str:
    text = _BASIS.sub("", text)
    text = re.sub(
        r"\([^)]*(?:million|thousand|billion|percent|unaudited)[^)]*\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = words(text)
    aliases = {
        "revenues": "revenue",
        "total revenues": "revenue",
        "total revenue": "revenue",
        "earnings per share": "eps",
        "earnings per diluted share": "diluted eps",
        "diluted earnings per share": "diluted eps",
        "basic earnings per share": "basic eps",
        "net cash provided by operating activities": "operating cash flow",
    }
    return aliases.get(text, text)


def quarter(text: str) -> set[str]:
    for word, number in _QUARTERS.items():
        text = re.sub(rf"\b{word} quarter\b", f"Q{number}", text, flags=re.IGNORECASE)
    return {
        a or b
        for a, b in re.findall(
            r"\bq([1-4])(?=\b|20\d{2})|\b([1-4])q(?=\b|20\d{2})", text.casefold()
        )
    }


def duration(text: str) -> int | None:
    return (
        0
        if re.search(r"\bas of\b|point.in.time|quarter.end", text, re.IGNORECASE)
        else _duration(text)
    )


def period_issues(value: SourcedValue, headers: str, context: str) -> list[str]:
    issues = []
    claimed_years = set(re.findall(r"20\d{2}", value.period))
    local_years = set(re.findall(r"20\d{2}", headers))
    years = local_years or set(re.findall(r"20\d{2}", context))
    if len(claimed_years) != 1 or years != claimed_years:
        issues.append("period year is missing or conflicts with the source column")
    claimed_q = quarter(value.period)
    source_q = quarter(headers) or quarter(context)
    if claimed_q and (len(claimed_q) != 1 or source_q != claimed_q):
        issues.append("fiscal quarter is missing or conflicts with the source")
    source_duration = duration(headers)
    expected = duration(value.duration) if value.duration else duration(value.period)
    if source_duration is None:
        source_duration = duration(context)
    if expected is None or source_duration is None or expected != source_duration:
        issues.append("duration is missing or conflicts with the source column")
    period_duration = duration(value.period)
    if period_duration is not None and expected != 0 and period_duration != expected:
        issues.append("period label conflicts with its duration")
    return issues


def metadata_issues(value: SourcedValue, row: str, headers: str, context: str) -> list[str]:
    issues = period_issues(value, headers, context)
    scope = f"{row} {headers} {context}"
    source_basis = {words(m[0]) for m in _BASIS.finditer(f"{row} {headers}")}
    claimed_basis = words(value.basis)
    if claimed_basis and claimed_basis not in source_basis:
        issues.append("accounting basis is not established by the row/column")
    if not claimed_basis and source_basis:
        issues.append("accounting basis is missing")
    if value.comparison_qualifier and words(value.comparison_qualifier) not in words(scope):
        issues.append("comparison qualifier is not established")
    if value.comparison:
        patterns = {
            "YoY": r"year.over.year|\byoy\b|\by/y\b|prior year",
            "QoQ": r"quarter.over.quarter|sequential|\bqoq\b|\bq/q\b",
            "versus prior guidance": r"previous|prior|old",
        }
        if not re.search(patterns[value.comparison], scope, re.IGNORECASE):
            issues.append("comparison reference is not established")
    currency = currencies(f"{row} {headers}") or currencies(context)
    monetary = words(value.unit) in ("money", "per share", "money per share")
    if monetary:
        if not value.currency or currency != {value.currency.upper()}:
            issues.append("currency is missing or conflicts with source units")
    elif value.currency:
        issues.append("nonmonetary value has currency metadata")
    if not monetary and value.unit not in ("%", "bps") and words(value.unit) not in words(scope):
        issues.append("count/physical unit is not established")
    if value.unit in ("%", "bps") and not re.search(
        r"%|percent|bps|basis points", f"{row} {headers}", re.IGNORECASE
    ):
        issues.append("percentage unit is not established by this row/column")
    return issues


def currencies(text: str) -> set[str]:
    found = set(re.findall(r"\b(?:USD|EUR|GBP|CAD|JPY|RMB|CNY)\b", text.upper()))
    if re.search(r"US\$|U\.S\. dollars?", text, re.IGNORECASE):
        found.add("USD")
    if "€" in text:
        found.add("EUR")
    if "£" in text:
        found.add("GBP")
    return found


def source_scale(text: str, per_share: bool = False) -> str | None:
    if per_share:
        return "one"
    scales = {a or b for a, b in _SCALE.findall(text)}
    if len(scales) > 1:
        return "ambiguous"
    return next(iter(scales)).removesuffix("s") if scales else None


def qualifier_issues(value: SourcedValue, text: str) -> list[str]:
    qualifiers = {
        "approximately": r"\b(?:about|approximately|roughly|around)\b|~",
        "nearly": r"\b(?:nearly|almost)\b",
        "more_than": r"\b(?:over|more than)\b|(?<![<>=])>(?!=)",
        "at_least": r"\bat least\b|>=",
        "less_than": r"\b(?:under|less than)\b|(?<![<>=])<(?!=)",
        "at_most": r"\b(?:up to|at most)\b|<=",
    }
    observed = {
        key for key, pattern in qualifiers.items() if re.search(pattern, text, re.IGNORECASE)
    }
    expected = set() if value.value_qualifier == "exact" else {value.value_qualifier}
    return (
        [] if observed == expected else ["bound/approximation qualifier conflicts with the source"]
    )


def numeric_match(value: SourcedValue, source: str, caption: str) -> bool:
    unit = "" if value.unit in ("%", "bps") else caption
    parsed = _numbers(source, unit)
    expected = [Decimal(value.amount) * SCALES[value.scale]]
    if value.upper is not None:
        expected.append(Decimal(value.upper) * SCALES[value.scale])
    dimension = "%" if value.unit == "%" else "bp" if value.unit == "bps" else "number"
    return len(parsed) == len(expected) and all(
        amount == actual and kind == dimension
        for amount, (actual, _, kind) in zip(expected, parsed, strict=True)
    )
