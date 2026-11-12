"""Conservative source-local checks. Passing is not proof of universal semantic accuracy."""

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from earnings_bot.summary_policy import SummaryFact, normalized

_SCALE = {
    "": Decimal(1),
    "thousand": Decimal(1000),
    "million": Decimal(10**6),
    "billion": Decimal(10**9),
    "trillion": Decimal(10**12),
    "k": Decimal(1000),
    "m": Decimal(10**6),
    "b": Decimal(10**9),
    "t": Decimal(10**12),
}
_NUMBER = re.compile(
    r"(?<![\w.])([+-]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(thousands?|millions?|billions?|trillions?|[kmbt]|%|bps)?(?!\w)",
    re.IGNORECASE,
)
_MONTHS = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "3": 3, "6": 6, "9": 9, "12": 12}
_COUNTS = re.compile(
    r"accounts|users|MAU|shares|restaurants|engines|deals|\bcount\b", re.IGNORECASE
)


def _loose(text: str) -> str:
    """Text compared without table-cell breaks or the spacing a model may drop when quoting a
    row ("18 | %" and "18 %" both read "18%"; "$ | 14,118" reads "$14,118"). The words and
    numbers themselves must still match."""
    text = normalized(text).replace("|", " ")
    text = re.sub(r"([$€£¥(])\s+", r"\1", text)
    text = re.sub(r"\s+([%)])", r"\1", text)
    return " ".join(text.split())


def _passages(source: str) -> list[str]:
    """A source may quote several passages: "Net income was $15.3B." "EPS | 5.57"."""
    quoted = re.findall(r'"([^"]{4,})"', source)
    return quoted if source.strip().startswith('"') and quoted else [source]


# "$2.649 - $2.653 billion": the scale word written once covers both ends of the range
_RANGE_SCALE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)(\s*(?:-|\u2013|to)\s*[$\u20ac\u00a3\u00a5]?\s*\d[\d,]*(?:\.\d+)?\s*"
    r"(thousands?|millions?|billions?|trillions?)\b)",
    re.IGNORECASE,
)
# The quoted source itself names the comparison ("grew 13% year-over-year")
_YOY_WORDS = re.compile(
    r"year[- ]over[- ]year|\by/y\b|\byoy\b|from a year (?:ago|earlier)|"
    r"(?:prior|previous) year|same (?:quarter|period) (?:of )?last year",
    re.IGNORECASE,
)
_QOQ_WORDS = re.compile(r"quarter[- ]over[- ]quarter|sequential|\bq/q\b|\bqoq\b", re.IGNORECASE)


def _stated_comparison(fact: SummaryFact) -> SummaryFact:
    """Fill a missing YoY label only when the quoted passage says year-over-year and nothing
    else; the current period alone never establishes it."""
    if fact.comparison or not re.search(r"\d\s*(?:%|bps|percent)", fact.change):
        return fact
    if _YOY_WORDS.search(fact.source) and not _QOQ_WORDS.search(fact.source):
        return replace(fact, comparison="YoY")
    return fact


def _numbers(text: str, unit: str = "") -> list[tuple[Decimal, Decimal, str]]:
    """Amount, rounding tolerance and dimension; Decimal preserves table precision."""
    text = _RANGE_SCALE.sub(lambda m: f"{m[1]} {m[3]}{m[2]}", text)
    text = re.sub(r"([+-])([$€£¥])", r"\2\1", text)
    text = re.sub(r"(\d)\s*\|\s*(%|bps)", r"\1\2", text)
    text = re.sub(
        r"\(\s*(\d[\d,]*(?:\.\d+)?)\s*(?:\|\s*)?(%|bps)?\s*\)",
        lambda match: "-" + match[1] + (match[2] or ""),
        text,
    )
    for word, number in {"one": "1", "two": "2", "three": "3", "five": "5", "ten": "10"}.items():
        text = re.sub(rf"\b{word}\b", number, text, flags=re.IGNORECASE)
    hint = next((v for k, v in _SCALE.items() if len(k) > 1 and k in unit.lower()), Decimal(1))
    result = []
    for match in _NUMBER.finditer(text):
        digits, suffix = match.groups()
        digits = digits.replace(",", "")
        suffix = (suffix or "").lower().removesuffix("s")
        scale = Decimal(1) if suffix in ("%", "bp") else _SCALE.get(suffix) if suffix else hint
        if scale is None:
            scale = hint
        dimension = suffix if suffix in ("%", "bp") else "number"
        decimals = len(digits.partition(".")[2])
        result.append(
            (Decimal(digits) * scale, Decimal("0.5") * Decimal(10) ** -decimals * scale, dimension)
        )
    return result


def _supported(
    claim: tuple[Decimal, Decimal, str], evidence: list[tuple[Decimal, Decimal, str]]
) -> bool:
    amount, tolerance, dimension = claim
    return any(
        dimension == kind and abs(amount - value) <= tolerance for value, _, kind in evidence
    )


def _duration(period: str) -> int | None:
    if match := re.search(r"(three|six|nine|twelve|3|6|9|12)[ -]*months?", period, re.IGNORECASE):
        return _MONTHS[match[1].lower()]
    if "ytd" in period.lower() and (q := re.search(r"q([1-4])", period, re.IGNORECASE)):
        return int(q[1]) * 3
    if re.search(r"q[1-4]|[1-4]q|quarter", period, re.IGNORECASE):
        return 3
    return 12 if re.search(r"\bFY|full.year|fiscal year|^20\d\d$", period, re.IGNORECASE) else None


def _table_duration(fact: SummaryFact, context: str, source_unit: str) -> int | None:
    durations = [
        _MONTHS[m.lower()]
        for m in re.findall(
            r"\b(three|six|nine|twelve|3|6|9|12) months? ended", context, re.IGNORECASE
        )
    ]
    if not durations:
        return None
    # A combined quarter/YTD table has two current/prior pairs, in header order.
    durations = list(dict.fromkeys(durations[-2:]))
    if len(durations) == 1:
        return durations[0]
    cells = [
        v
        for part in fact.source.split("|")
        if (v := _numbers(part, source_unit)) and re.fullmatch(r"\s*[$€£¥(+-]*[\d,.]+\)?\s*", part)
    ]
    current = _numbers(fact.value, fact.unit)
    if current and len(cells) == 4:
        matches = [i for i, value in enumerate(cells) if _supported(current[0], value)]
        if len(matches) == 1:
            return durations[matches[0] // 2]
    # A row quoted from its label onward covers the leading columns: the first duration
    if (
        current
        and len(cells) in (2, 3)
        and re.match(r'\s*"?[A-Za-z]', fact.source)
        and _supported(current[0], cells[0])
    ):
        return durations[0]
    return None  # Ambiguous columns are not repaired by guessing.


# A table's caption gives its scale: "(in millions)", "Dollars in thousands", and also
# "In USD $ millions" (Nebius), "(US$ millions, except per share amounts)" (Brookfield) and
# "(Millions of dollars)". Read from loosened text, where "$ millions" is "$millions".
_TABLE_SCALE = re.compile(
    r"(?:\bin\s+|\b(?:usd|us|eur|gbp|cad|rmb)\s*|[$€£¥]\s*)"
    r"(?:(?:u\.?s\.?|usd|us|eur|gbp|cad|rmb)\s*)?[$€£¥]?\s*(thousands|millions|billions)\b"
    r"|\((thousands|millions|billions) of\b"
)


def validate_fact(fact: SummaryFact, release: str) -> list[str]:
    """Return reasons to omit an uncertain fact; bind numbers to its quote, not the whole file."""
    issues = []
    document = _loose(release)
    passages = [_loose(part) for part in _passages(fact.source)]
    positions = [document.find(part) if part else -1 for part in passages]
    if not passages or min(positions) < 0:
        return ["source passage is empty or not exact"]
    quote = " ".join(passages)
    position = positions[0]
    context = document[max(0, position - 1800) : position + len(passages[0])]
    # Cells quoted from a table, with or without the cell breaks: adjacent bare numbers
    table = "|" in fact.source or bool(re.search(r"[\d)%]\s+[$€£¥(]?\d", quote))
    if not fact.metric.strip() or not (fact.value.strip() or fact.change.strip()):
        issues.append("empty displayed fact")
    if not fact.period.strip():
        issues.append("missing fiscal period")
    if re.search(r"q[1-4].*(?:and|;)\s*(?:fy|full.year)", fact.period, re.IGNORECASE):
        issues.append("mixed quarter and full-year periods")
    if _COUNTS.search(fact.unit) and fact.currency:
        issues.append("count has currency metadata")
    if re.search(r"percent|%", fact.unit, re.IGNORECASE) and re.search(r"[$€£¥]\s*\d", fact.value):
        issues.append("money value has percent unit")
    if re.search(r"\d", fact.unit):
        issues.append("unit contains a numeric value")
    if (
        fact.change
        and re.search(r"\d\s*(?:%|bps|pp)", fact.change)
        and not (
            fact.comparison
            or re.search(
                r"yoy|qoq|versus|year.over.year|quarter.over.quarter", fact.change, re.IGNORECASE
            )
        )
    ):
        issues.append("numeric change lacks a comparison reference")
    if fact.comparison == "YoY" and re.search(
        r"versus (?:reported|gaap)|vs\.? gaap", fact.change, re.IGNORECASE
    ):
        issues.append("same-period adjustment is labeled YoY")
    if fact.comparison == "versus prior guidance" and re.search(
        r"yoy|year.over.year", fact.change, re.IGNORECASE
    ):
        issues.append("growth reference conflicts with prior-guidance comparison")
    unit_match = [match[1] or match[2] for match in _TABLE_SCALE.finditer(context)]
    # A per-share figure keeps its own units in a table "in millions, except per-share data"
    per_share = re.search(r"per.share|\beps\b", f"{fact.metric} {fact.unit}", re.IGNORECASE)
    source_unit = unit_match[-1] if unit_match and table and not per_share else ""
    evidence = _numbers(quote, source_unit)
    current, prior = _numbers(fact.value, fact.unit), _numbers(fact.prior, fact.unit)
    changes = _numbers(fact.change)
    if (
        current
        and changes
        and changes[0][2] == "%"
        and fact.comparison in ("YoY", "QoQ")
        and not prior
        and not re.search(
            # A signed percentage beside the figure ("$19.0B, +49%") is a change, not a level
            r"grew|growth|\bup\b|\bdown\b|increas|decreas|yoy|qoq|year.over.year|"
            r"quarter.over.quarter|year on year|o/\(u\)|(?:^|[\s,(])[+\u2212-]\d+(?:\.\d+)?%",
            quote + " " + " ".join(re.findall(r"year on year|o/\(u\)", context)),
        )
    ):
        issues.append("percentage is not established as growth of this measure")
    computed = False
    if len(current) == len(prior) == len(changes) == 1 and fact.comparison == "YoY":
        value, _, dimension = current[0]
        before, _, prior_dimension = prior[0]
        change, tolerance, change_dimension = changes[0]
        if (
            dimension == prior_dimension
            and _supported(current[0], evidence)
            and _supported(prior[0], evidence)
        ):
            if before > 0 and change_dimension == "%" and dimension == "number":
                computed = True
                if abs((value / before - 1) * 100 - change) > tolerance:
                    issues.append(
                        "YoY growth disagrees with source-supported current/prior arithmetic"
                    )
            elif dimension == "%" and change_dimension == "bp":
                computed = True
                if abs((value - before) * 100 - change) > tolerance:
                    issues.append(
                        "margin bps disagree with source-supported current/prior arithmetic"
                    )
    for label, claims in [
        ("value", current),
        ("prior", prior),
        ("change", [] if computed else changes),
    ]:
        if any(not _supported(number, evidence) for number in claims):
            issues.append(f"{label} number is not supported by its source passage")
    duration = _table_duration(fact, context, source_unit)
    cash_metric = "margin" not in fact.metric.lower() and re.search(
        r"cash.flow|cash from|operating cash|payments|capex|capital expenditure",
        fact.metric,
        re.IGNORECASE,
    )
    if cash_metric and duration and _duration(fact.period) != duration:
        issues.append("cash-flow duration disagrees with source table headers/column")
    if cash_metric and table and _duration(fact.period) and not duration:
        issues.append("cash-flow table duration/column is not established")
    # Material exclusions must belong to the quoted measure, rather than nearby unrelated data.
    # ("excluding fuel" is supported by "ex. fuel" or "comp sales, fuel excluded" in the quote)
    exclusion = re.search(r"excluding\s+([a-z]+)", fact.basis, re.IGNORECASE)
    if exclusion and exclusion[1].lower() not in quote:
        issues.append("comparison exclusion is not supported by its quoted measure")
    if fact.kind == "guide" and re.search(
        r"raised|increased|cut|lowered", fact.change, re.IGNORECASE
    ):
        nearest = max(
            context.rfind("for q"), context.rfind("full year"), context.rfind("updated fy")
        )
        local = context[nearest:] if nearest >= 0 else quote
        if not re.search(r"rais|increas|cut|lower|updated", local):
            issues.append("guidance revision is not established by its local source context")
    return issues


def validate_facts(facts: list[SummaryFact], release: str) -> tuple[list[SummaryFact], list[str]]:
    accepted, rejected = [], []
    for fact in map(_stated_comparison, facts):
        try:
            issues = validate_fact(fact, release)
        except (InvalidOperation, ValueError, OverflowError):
            issues = ["ambiguous numeric or table binding"]
        if issues:
            rejected.append(f"{fact.metric}: {'; '.join(issues)}")
            # Preserve an independently supported current value when only its optional
            # prior/change fails. Never repair a quote, scope, duration, unit or number.
            optional = (
                "prior number",
                "change number",
                "YoY growth",
                "margin bps",
                "percentage is not established",
                "numeric change lacks",
                "same-period adjustment",
                "growth reference conflicts",
            )
            if fact.value.strip() and all(issue.startswith(optional) for issue in issues):
                current_only = replace(fact, prior="", change="", comparison="")
                if not validate_fact(current_only, release):
                    accepted.append(current_only)
        else:
            accepted.append(fact)
    return accepted, rejected
