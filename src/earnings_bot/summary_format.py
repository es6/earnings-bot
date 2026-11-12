"""Conservative display formatting. Preserve all selected facts and exact range endpoints."""

# En dashes are intentional range separators in the display.
# ruff: noqa: RUF001, RUF002

import re
from dataclasses import replace
from decimal import Decimal

from earnings_bot.summary_policy import SummaryFact, change_reference

_SCALES = {
    "": Decimal(1),
    "K": Decimal(1000),
    "M": Decimal(10**6),
    "B": Decimal(10**9),
    "T": Decimal(10**12),
}
_MARKERS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SUFFIX = r"trillions?|billions?|millions?|thousands?|bn|[TBMK%]"
_AMOUNT = re.compile(rf"\s*([$€£¥])?\s*({_NUMBER})\s*({_SUFFIX})?\s*", re.IGNORECASE)
_RANGE = re.compile(
    rf"\s*([$€£¥])?\s*({_NUMBER})\s*({_SUFFIX})?\s*(?:to|–|—|-)\s*"
    rf"([$€£¥])?\s*({_NUMBER})\s*({_SUFFIX})?\s*",
    re.IGNORECASE,
)


def _suffix(text: str) -> str:
    lowered = text.lower().removesuffix("s")
    if lowered in ("billion", "bn"):
        return "B"
    if lowered in ("million", "thousand", "trillion"):
        return {"million": "M", "thousand": "K", "trillion": "T"}[lowered]
    return text.upper()


def _unit_scale(unit: str) -> str:
    if re.search(r"per share|/share", unit, re.IGNORECASE):
        return ""
    if re.search(r"percent|%", unit, re.IGNORECASE):
        return "%"
    match = re.search(rf"\b({_SUFFIX})\b", unit, re.IGNORECASE)
    return _suffix(match[1]) if match else ""


def _digits(value: Decimal) -> str:
    return (
        format(value, "f").rstrip("0").rstrip(".")
        if value != value.to_integral()
        else str(int(value))
    )


def format_value(value: str, unit: str = "", currency: str = "") -> str:
    """Parse complete scalars/ranges and scalar margin notes; preserve other mixed text.

    A trailing unit applies to both bounds: $2,963 to $2,964 million becomes
    $2.963B–$2.964B, never $2,963 to $2.96B. No rounding can erase a narrow range.
    """
    if approximate := re.match(
        r"(~\s*|(?:about|around|approximately)\s+)(.+)", value, re.IGNORECASE
    ):
        rendered = format_value(approximate[2], unit, currency)
        if rendered != approximate[2]:
            return approximate[1] + rendered
    hint = _unit_scale(unit)
    if ";" in unit or re.search(r"\bratio\b", unit, re.IGNORECASE):
        return value
    # A complete leading scalar followed by a margin note has its own boundary.
    # Never scale numbers inside the note or run a money regex across a mixed range.
    if annotated := re.fullmatch(r"([^();]+)(\s*\([^()]+\))", value):
        head = annotated[1].rstrip()
        if _AMOUNT.fullmatch(head):
            return format_value(head, unit, currency) + " " + annotated[2].lstrip()
    marker = _MARKERS.get(currency, "")
    # A provider can erroneously put USD on an order/customer count. Only a money
    # dimension or a bare scale supports adding a currency symbol to a bare value.
    dimension = re.sub(
        rf"\b(?:{_SUFFIX}|USD|EUR|GBP|JPY|dollars?|euros?|pounds?|yen|money|currency|of)\b",
        "",
        unit,
        flags=re.IGNORECASE,
    )
    if re.search(r"[a-z]", dimension, re.IGNORECASE) and not re.search(
        r"per share|/share", unit, re.IGNORECASE
    ):
        marker = ""
    if match := _RANGE.fullmatch(value):
        left_marker, left, left_unit, right_marker, right, right_unit = match.groups()
        if left_marker and right_marker and left_marker != right_marker:
            return value
        if any(m and marker and m != marker for m in (left_marker, right_marker)):
            return value
        first = _suffix(left_unit or right_unit or hint)
        second = _suffix(right_unit or left_unit or hint)
        if "%" in (first, second):
            if first != second:
                return value
            lower = _digits(Decimal(left.replace(",", "")))
            upper = _digits(Decimal(right.replace(",", "")))
            return f"{lower}%–{upper}%"
        low = Decimal(left.replace(",", "")) * _SCALES[first]
        high = Decimal(right.replace(",", "")) * _SCALES[second]
        marker = left_marker or right_marker or marker
        scale, suffix = next(
            ((v, k) for k, v in reversed(_SCALES.items()) if max(abs(low), abs(high)) >= v),
            (Decimal(1), ""),
        )
        return f"{marker}{_digits(low / scale)}{suffix}–{marker}{_digits(high / scale)}{suffix}"
    if match := _AMOUNT.fullmatch(value):
        explicit, number, suffix = match.groups()
        suffix = _suffix(suffix or hint)
        if suffix == "%":
            return f"{_digits(Decimal(number.replace(',', '')))}%"
        if explicit and marker and explicit != marker:
            return value
        if not (explicit or marker or suffix):
            return value
        amount = Decimal(number.replace(",", "")) * _SCALES[suffix]
        scale, display = next(
            ((v, k) for k, v in reversed(_SCALES.items()) if abs(amount) >= v), (Decimal(1), "")
        )
        return f"{explicit or marker}{_digits(amount / scale)}{display}"
    return value


def format_period(value: str) -> str:
    text = " ".join(value.split())
    text = re.sub(r"\b([1-4])Q\s*(20\d{2})\b", r"Q\1 \2", text, flags=re.IGNORECASE)
    if re.search(
        r"\b(?:YTD|year[- ]to[- ]date|TTM|LTM|trailing|[1-4]H|H[12]|"
        r"first half|second half|[369]M|12M|"
        r"(?:three|six|nine|twelve|3|6|9|12)[ -]*months?)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return text  # A quarter/year label must never erase the observation's duration.
    if re.search(r"\band\b|;|/", text, re.IGNORECASE):
        return text  # A legacy object can contain multiple periods; never collapse them to one.
    quarter = re.search(r"\b(Q[1-4])\s*(FY|fiscal(?: year)?)?\s*(20\d{2})\b", text, re.IGNORECASE)
    if quarter:
        return f"{quarter[1].upper()} {'FY' if quarter[2] else ''}{quarter[3]}"
    annual = re.search(
        r"\b(?:(?:full[- ]year\s+)?fiscal(?: year)?|full[- ]year|(?:FY\s*)+)\s*(20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    return f"FY{annual[1]}" if annual else text


def format_label(value: str) -> str:
    text = re.sub(r"fee[- ]related earnings", "FRE", value, flags=re.IGNORECASE)
    text = re.sub(r"total operating earnings", "TOE", text, flags=re.IGNORECASE)
    return re.sub(r"\b([A-Z][A-Z0-9]{1,8})\s*\(\1\)", r"\1", text)


def _basis_key(text: str) -> str:
    # Adjusted and non-GAAP are equivalent display labels; GAAP remains distinct.
    text = re.sub(r"non[- ]?gaap", "adjusted", text, flags=re.IGNORECASE)
    return " ".join(re.findall(r"\w+", text.casefold()))


def format_change(value: str, comparison: str = "") -> str:
    """Normalize notation, never invent a comparison period from the current quarter."""
    text = re.sub(r"year[- ]over[- ]year|Y/Y", "YoY", value, flags=re.IGNORECASE)
    text = re.sub(r"quarter[- ]over[- ]quarter|Q/Q", "QoQ", text, flags=re.IGNORECASE)
    match = re.fullmatch(
        r"\s*(\()?([+-]?\d+(?:\.\d+)?)\)?\s*(%|bps|pp)\)?"
        r"\s*(YoY|QoQ|versus prior guidance)?\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        return text
    if text.count("(") != text.count(")"):
        return text
    parenthesis, number, unit, explicit = match.groups()
    amount = Decimal(number)
    if parenthesis:
        amount = -abs(amount)
    sign = "+" if amount > 0 else ""
    reference = explicit or comparison
    gap = "" if unit == "%" else " "
    return f"{sign}{_digits(amount)}{gap}{unit}" + (f" {reference}" if reference else "")


_ADJUSTED_BASIS = re.compile(r"adjust|non.?gaap", re.IGNORECASE)


def _repeats_label(part: str, label: str) -> bool:
    """A basis tag the label already states. GAAP and non-GAAP differ: "GAAP EPS (non-GAAP)"
    keeps its tag, "Adjusted revenue (Adjusted (Non-GAAP))" doesn't need it."""
    if re.search(r"gaap|adjust", part, re.IGNORECASE):
        return bool(_ADJUSTED_BASIS.search(part)) and bool(_ADJUSTED_BASIS.search(label))
    words = set(re.findall(r"[a-z]{4,}", part.lower()))
    return bool(words & set(re.findall(r"[a-z]{4,}", label.lower())))


def format_fact(fact: SummaryFact) -> str:
    stamp = format_period(fact.period)
    prefix = f"{stamp} · " if stamp else ""
    label = format_label(fact.metric)
    if stamp:
        label = re.sub(rf"^{re.escape(stamp)}(?:\s+|:\s*)", "", label, flags=re.IGNORECASE)
    scope = " ".join(fact.group.split())
    scope_key = _basis_key(scope)
    # A neutral consolidation label adds no identity; an actual region/segment does.
    if (
        scope_key
        and scope_key not in {"consolidated", "total", "total company", "company"}
        and f" {scope_key} " not in f" {_basis_key(label)} "
    ):
        label = f"{scope} · {label}"
    reference = change_reference(fact)
    known = f" {_basis_key(label)} {_basis_key(reference) if fact.change else ''} "
    basis = [
        part.strip()
        for part in re.split(r"[,;]", fact.basis)
        if part.strip()
        and f" {_basis_key(part)} " not in known
        # A short tag ("GAAP", "constant currency"), not a definition, and not one the label
        # already says ("Adjusted revenue (Adjusted (Non-GAAP))")
        and len(part.split()) <= 4
        and not _repeats_label(part, label)
    ]
    if basis:
        label += f" ({', '.join(basis)})"
    value = format_value(fact.value, fact.unit, fact.currency)
    if value == fact.value and fact.unit and re.search(r"\d", value) and ";" in value:
        value += f" [{fact.unit}]"
    change = format_change(fact.change, reference)
    if not value.strip():  # a growth rate given without a level
        return f"{prefix}{label}: {change}" if change.strip() else f"{prefix}{label}"
    text = f"{prefix}{label}: {value}"
    return f"{text}; {change}" if change.strip() else text


def discord_brief(facts: list[SummaryFact]) -> str:
    """The card's highlights: facts grouped under their period and kind, in the layout the
    user reviewed ("**Q3 2026 · Results**", 🔹 figures, 🔸 business updates)."""
    groups: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        kind = {"guide": "Outlook", "update": "Business updates"}.get(fact.kind, "Results")
        text = format_fact(replace(fact, period=""))
        metric, _, value = text.partition(": ")
        marker = "🔸" if kind == "Business updates" else "🔹"
        line = f"{marker} **{metric}**: {value}" if value else f"{marker} **{metric}**"
        groups.setdefault((kind, format_period(fact.period)), []).append(line)
    return "\n\n".join(
        f"**{f'{stamp} · {kind}'.strip(' ·')}**\n" + "\n".join(lines)
        for (kind, stamp), lines in groups.items()
    )


def render_facts(ticker: str, facts: list[SummaryFact]) -> str:
    # No length budget and no automatic importance filter. Selection belongs upstream.
    return "\n".join([f"${ticker}", *(format_fact(fact) for fact in facts)])
