"""Conservative display formatting. Preserve all selected facts and exact range endpoints."""

# En dashes are intentional range separators in the display.
# ruff: noqa: RUF001, RUF002

import re
from collections import Counter
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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


def _short(value: Decimal) -> str:
    """A scaled amount to at most four significant digits and two decimals: $7.9637B reads
    $7.96B and $269.985M reads $270M. Only single amounts shown with K, M, B or T; ranges,
    per-share figures and percentages keep every digit."""
    integer_digits = max(abs(value).adjusted() + 1, 1)
    places = min(2, max(0, 4 - integer_digits))
    return _digits(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


_APPROXIMATE = re.compile(
    r"(~\s*|(?:about|around|approximately|roughly|nearly|almost|over|more than|less than|"
    r"under|above|below|up to|at least|close to)\s+)(.+)",
    re.IGNORECASE,
)
# An accounting negative: (37.5), $(2,047) million, ($2,047)
_PARENTHESIZED = re.compile(
    rf"\s*([$€£¥])?\s*\(\s*([$€£¥])?\s*((?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?)\s*\)"
    rf"\s*({_SUFFIX})?\s*",
    re.IGNORECASE,
)


def format_value(value: str, unit: str = "", currency: str = "") -> str:
    """Parse complete scalars/ranges and scalar margin notes; preserve other mixed text.

    A trailing unit applies to both bounds: $2,963 to $2,964 million becomes
    $2.963B–$2.964B, never $2,963 to $2.96B. No rounding can erase a narrow range.
    A negative amount reads -$37.5M, whether written (37.5), $(37.5) or $-37.5.
    """
    value = value.replace("−", "-")
    # (18)% to (16)% is -18% to -16%
    value = re.sub(r"\((\d[\d,.]*)\)\s*(%|bps\b)", r"-\1\2", value)
    if approximate := _APPROXIMATE.match(value):
        rendered = format_value(approximate[2], unit, currency)
        if rendered != approximate[2]:
            return approximate[1] + rendered
    if negative := _PARENTHESIZED.fullmatch(value):
        outer, inner, number, suffix = negative.groups()
        rendered = format_value(f"{outer or inner or ''}{number}{suffix or ''}", unit, currency)
        return rendered if rendered.startswith("-") else f"-{rendered}"
    if leading := re.fullmatch(r"\s*-\s*([$€£¥].*)", value):
        rendered = format_value(leading[1], unit, currency)
        if rendered != leading[1] and not rendered.startswith("-"):
            return f"-{rendered}"
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

        def end(amount: Decimal) -> str:
            sign = "-" if amount < 0 else ""
            return f"{sign}{marker}{_digits(abs(amount) / scale)}{suffix}"

        return f"{end(low)}–{end(high)}"
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
        shown = _short(abs(amount) / scale) if display else _digits(abs(amount) / scale)
        sign = "-" if amount < 0 else "+" if number.startswith("+") else ""
        return f"{sign}{explicit or marker}{shown}{display}"
    return value


def amount_of(value: str, unit: str = "") -> tuple[Decimal, bool] | None:
    """A single complete amount in base units, and whether it is a percentage. Ranges,
    approximations and mixed text are not amounts."""
    value = value.replace("−", "-").strip()
    sign = Decimal(1)
    if negative := _PARENTHESIZED.fullmatch(value):
        outer, inner, number, suffix = negative.groups()
        value, sign = f"{outer or inner or ''}{number}{suffix or ''}", Decimal(-1)
    elif value.startswith("-") and value[1:2] in _MARKERS.values():
        value, sign = value[1:], Decimal(-1)
    match = _AMOUNT.fullmatch(value)
    if not match:
        return None
    _, number, suffix = match.groups()
    scale = _suffix(suffix or _unit_scale(unit))
    try:
        amount = Decimal(number.replace(",", "")) * sign
    except InvalidOperation:
        return None
    if scale == "%":
        return amount, True
    return amount * _SCALES.get(scale, Decimal(1)), False


_DURATION = re.compile(
    r"\b(?:YTD|year[- ]to[- ]date|TTM|LTM|trailing|[1-4]H|H[12]|"
    r"first half|second half|[369]M|12M|"
    r"(?:three|six|nine|twelve|3|6|9|12)[ -]*months?)\b",
    re.IGNORECASE,
)
_QUARTERS = {"first": "1", "second": "2", "third": "3", "fourth": "4"}


def format_period(value: str) -> str:
    text = " ".join(value.split())
    text = re.sub(r"\b([1-4])Q\s*(20\d{2})\b", r"Q\1 \2", text, flags=re.IGNORECASE)
    # Two-digit years and spelled-out quarters: 3Q'26, FQ1'27, Q3 FY27, FY'27, third quarter 2026
    text = re.sub(r"\b([1-4])Q\s*'?(\d{2})\b", r"Q\1 20\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bFQ([1-4])\s*'?(\d{2})\b", r"Q\1 FY20\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bFY\s*'?(\d{2})\b", r"FY20\1", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(first|second|third|fourth) quarter(?: of)?( fiscal(?: year)?| FY)?\s+(20\d{2})\b",
        lambda m: f"Q{_QUARTERS[m[1].lower()]} {'FY' if m[2] else ''}{m[3]}",
        text,
        flags=re.IGNORECASE,
    )
    durations = _DURATION.findall(text)
    # "Q3 2026 (three months ended Sep 30, 2026)" is the quarter; nine months never is
    quarter_only = bool(re.search(r"\bQ[1-4]\b", text)) and all(
        re.fullmatch(r"(?:three|3)[ -]*months?", found, re.IGNORECASE) for found in durations
    )
    if durations and not quarter_only:
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


# A reporting standard or statement name says nothing a reader needs on every line: reported
# figures are GAAP or IFRS unless the label says adjusted
_STANDARD = re.compile(r"(?<!non[- ])\b(?:u\.?\s?s\.?\s+)?(?:gaap|ifrs)\b", re.IGNORECASE)
_STATEMENT = re.compile(
    r"(?:basis|measures?|figures?|(?:as )?reported|balance sheet|cash flows?(?: statement)?|"
    r"income statement|statements? of (?:cash flows|operations|income)|accounting)?",
    re.IGNORECASE,
)
_NEUTRAL_SCOPE = re.compile(r"consolidated\b.*|total company|company|total|group")
# Scale, currency and generic words; what is left of a unit is its dimension (square feet)
_GENERIC_UNIT = re.compile(
    rf"\b(?:{_SUFFIX}|usd|eur|gbp|jpy|cad|chf|aud|dollars?|euros?|pounds?|yen|money|currency|"
    r"count|number|quantity|amount|value|in|of|units? of|x|times)\b|[$€£¥()]",
    re.IGNORECASE,
)


# Tags that only restate the default or the section: "(reported)", "(Operating KPI)",
# "(Forward outlook)"
_DEFAULT_TAG = re.compile(
    r"(?:as )?reported|(?:operating |key |financial |business )?(?:kpi|metric|indicator)s?|"
    r"(?:forward(?:[- ]looking)? )?(?:outlook|guidance)|forward[- ]looking",
    re.IGNORECASE,
)
# A margin's usual denominator; any other is shown (3.6% of Gross Bookings)
_REVENUE = re.compile(r"(?:total |net |segment )?(?:revenues?|sales)", re.IGNORECASE)


def _shown_basis(part: str, label: str) -> bool:
    if _DEFAULT_TAG.fullmatch(part.strip()):
        return False
    if not _STANDARD.search(part):
        return True
    if _ADJUSTED_BASIS.search(label):
        return True  # "Non-GAAP EPS (GAAP)" keeps the conflict visible
    return not _STATEMENT.fullmatch(_STANDARD.sub("", part).strip(" -"))


def _dimension(unit: str) -> str:
    """What a value counts when it isn't money, a percentage or per-share: square feet,
    vehicles, customers. Empty when the unit says nothing more than scale and currency."""
    if re.search(
        r"%|percent|per[- ]share|/share|\bbps\b|basis points|\bratio\b|;", unit, re.IGNORECASE
    ):
        return ""
    words = " ".join(_GENERIC_UNIT.sub(" ", unit).split()).strip(" ,-")
    return words if re.search(r"[A-Za-z]{2,}", words) and len(words.split()) <= 4 else ""


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
        and not _NEUTRAL_SCOPE.fullmatch(scope_key)
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
        and f" {_basis_key(format_label(part))} " not in known
        # A short tag ("constant currency"), not a definition, and not one the label already
        # says ("Adjusted revenue (Adjusted (Non-GAAP))")
        and len(part.split()) <= 4
        and not _repeats_label(part, label)
        and _shown_basis(part, label)
    ]
    if basis:
        label += f" ({', '.join(basis)})"
    value = format_value(fact.value, fact.unit, fact.currency)
    if value == fact.value and fact.unit and re.search(r"\d", value) and ";" in value:
        value += f" [{fact.unit}]"
    # A count keeps what it counts: 5.3M square feet, not 5.3M
    dimension = _dimension(fact.unit)
    if (
        dimension
        and re.search(r"\d", value)
        and not re.search(r"[A-Za-z]{3,}", re.sub(r"\b[KMBT]\b|bn", "", value))
        and dimension.lower()[:5] not in f"{label} {value}".lower()
    ):
        value = f"{value} {dimension}"
    denominator = re.fullmatch(
        r"\s*(?:%|percent(?:age)?)\s+of\s+(.+?)\s*", fact.unit, re.IGNORECASE
    )
    if (
        denominator
        and value.endswith("%")
        and not _REVENUE.fullmatch(denominator[1])
        and denominator[1].lower() not in label.lower()
    ):
        value = f"{value} of {denominator[1]}"
    change = format_change(fact.change, reference)
    if not value.strip():  # a growth rate given without a level
        return f"{prefix}{label}: {change}" if change.strip() else f"{prefix}{label}"
    text = f"{prefix}{label}: {value}"
    return f"{text}; {change}" if change.strip() else text


def with_change(fact: SummaryFact) -> SummaryFact:
    """Every level gets its comparable change when the release gives the prior figure. The
    checker has bound both numbers to their quote; prior is a year-ago actual by definition."""
    if (
        fact.change.strip()
        or not fact.prior.strip()
        or fact.kind not in ("actual", "segment")
        or fact.comparison not in ("", "YoY", "QoQ")
    ):
        return fact
    current, prior = amount_of(fact.value, fact.unit), amount_of(fact.prior, fact.unit)
    if current is None or prior is None or current[1] != prior[1]:
        return fact
    reference = fact.comparison or "YoY"
    (now, percent), (before, _) = current, prior
    if percent:
        if not re.search(r"margin|\brate\b|ratio|yield", fact.metric, re.IGNORECASE):
            return fact
        points = ((now - before) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return replace(fact, change=f"{points:+} bps", comparison=reference)
    if before <= 0 or now < 0:
        # No growth rate from a loss or zero; show the year-ago figure instead
        earlier = format_value(fact.prior, fact.unit, fact.currency)
        when = "a year ago" if reference == "YoY" else "a quarter ago"
        return replace(fact, change=f"vs {earlier} {when}", comparison="")
    growth = (now / before - 1) * 100
    places = Decimal(1) if abs(growth) >= 10 else Decimal("0.1")
    growth = growth.quantize(places, rounding=ROUND_HALF_UP)
    return replace(
        fact, change=f"{'+' if growth > 0 else ''}{_digits(growth)}%", comparison=reference
    )


_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DATE = re.compile(
    r"\b(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})"
    r"|(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(20\d{2}))\b",
    re.IGNORECASE,
)
_QUARTER_END = {"1": (3, 31), "2": (6, 30), "3": (9, 30), "4": (12, 31)}


def _date(text: str) -> tuple[int, int, int] | None:
    match = _DATE.search(text)
    if not match:
        return None
    month, day, year = (
        (match[1], match[2], match[3]) if match[1] else (match[5], match[4], match[6])
    )
    return int(year), _MONTHS.index(month.lower()[:3]) + 1, int(day)


def _point_in_time(text: str) -> tuple[int, int, int] | None:
    """The date of a balance-sheet stamp: "September 30, 2026", "As of Sep 30, 2026"."""
    bare = re.sub(
        r"^(?:as (?:of|at)|at|end of|balance at|period end)\s+",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    match = _DATE.fullmatch(bare)
    return _date(bare) if match else None


_ROLE_ORDER = {"headline": 0, "driver": 1, "guidance": 1, "supporting": 3}
_COMPANY = re.compile(
    r"\b(?:corporation|corp|incorporated|inc|limited|ltd|plc|holdings|group|company|co|"
    r"n\.?v|s\.?a|ag|se)\.?$",
    re.IGNORECASE,
)


def _line(fact: SummaryFact, marker: str, stamp: str = "") -> str:
    text = format_fact(replace(fact, period=""))
    metric, _, value = text.partition(": ")
    if stamp and stamp.lower() not in text.lower():
        metric = f"{stamp} · {metric}"
    return f"{marker} **{metric}**: {value}" if value else f"{marker} **{metric}**"


def _measure(fact: SummaryFact) -> str:
    """A segment fact's measure without its segment's name: "AWS segment net sales" and
    "North America segment net sales" both measure "net sales"."""
    group = re.escape(" ".join(fact.group.split()))
    text = re.sub(rf"\b{group}\b", "", fact.metric, flags=re.IGNORECASE) if group else fact.metric
    text = re.sub(r"\bsegments?\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:growth|change|increase|decrease)$", "", text.strip(), flags=re.IGNORECASE)
    return " ".join(text.split()).strip(" ·:-–,")


def _scope(fact: SummaryFact) -> str:
    return " ".join(fact.group.split()).casefold()


def _segment_line(facts: list[SummaryFact]) -> str:
    """Segments on one line, largest first; a level and its growth are one entry."""
    entries: dict[str, SummaryFact] = {}
    for fact in facts:
        group = " ".join(fact.group.split())
        if group in entries:
            known = entries[group]
            entries[group] = replace(
                known,
                value=known.value or fact.value,
                change=known.change or fact.change,
                comparison=known.comparison or fact.comparison,
            )
        else:
            entries[group] = fact
    sizes = {group: amount_of(fact.value, fact.unit) for group, fact in entries.items()}
    ordered = list(entries.items())
    known = {group: size[0] for group, size in sizes.items() if size is not None and not size[1]}
    if len(known) == len(entries):
        ordered.sort(key=lambda item: -known[item[0]])
    parts = []
    for group, fact in ordered:
        value = format_value(fact.value, fact.unit, fact.currency)
        change = format_change(fact.change, change_reference(fact))
        parts.append(
            " ".join(f"{group} {value}".split()) + (f", {change}" if change.strip() else "")
        )
    measure = _measure(facts[0])
    measure = measure[:1].upper() + measure[1:]
    return f"🔹 **{measure} by segment**: " + "; ".join(parts)


def _results_lines(facts: list[SummaryFact]) -> list[str]:
    def key(fact: SummaryFact) -> str:
        return _measure(fact).casefold() if fact.group.strip() else ""

    measures: dict[str, list[SummaryFact]] = {}
    for fact in facts:
        if fact.kind == "segment" and key(fact):
            measures.setdefault(key(fact), []).append(fact)
    merged = {k: group for k, group in measures.items() if len({_scope(f) for f in group}) >= 2}
    # A growth rate given on its own joins its segment's level ("Wealth Solutions DE growth")
    for fact in facts:
        group = merged.get(key(fact))
        if (
            group
            and fact.kind != "segment"
            and not fact.value.strip()
            and fact.change.strip()
            and _scope(fact) in {_scope(f) for f in group}
        ):
            group.append(fact)
    members = {id(f): k for k, group in merged.items() for f in group}
    lines, done = [], set()
    for fact in facts:
        if (k := members.get(id(fact))) is not None:
            if k not in done:
                done.add(k)
                lines.append(_segment_line(merged[k]))
            continue
        lines.append(_line(fact, "🔹"))
    return lines


def discord_brief(facts: list[SummaryFact]) -> str:
    """The card's highlights, in the layout the user reviewed ("**Q3 2026 · Results**",
    🔹 figures, 🔸 business updates), most important first:

    - the quarter's headline results (the model marks each fact's role);
    - the outlook;
    - the quarter's other results, with the balances at its end and its segments on one
      line ("More results" when an outlook sits between them and the headline);
    - results for other periods (year to date, trailing twelve months);
    - business updates, under one heading.
    """
    facts = [with_change(fact) for fact in facts]
    # The consolidated company named as a "segment" on most lines repeats on every line
    groups = Counter(" ".join(f.group.split()) for f in facts if f.group.strip())
    if groups:
        top, count = groups.most_common(1)[0]
        if (count >= 2 and _COMPANY.search(top)) or (count >= 3 and count > len(facts) / 2):
            facts = [replace(f, group="") if " ".join(f.group.split()) == top else f for f in facts]
    stamps = {fact.period: format_period(fact.period) for fact in facts}
    # "Three months ended Sep 30, 2026" is the brief's Q3 2026 when another fact says so
    ends: dict[tuple[int, int, int], str] = {}
    for period, stamp in stamps.items():
        if re.fullmatch(r"Q[1-4] (?:FY)?20\d{2}", stamp) and (end := _date(period)):
            ends.setdefault(end, stamp)
    for period, stamp in stamps.items():
        if (
            _DURATION.search(stamp)
            and all(
                re.fullmatch(r"(?:three|3)[ -]*months?", d, re.IGNORECASE)
                for d in _DURATION.findall(stamp)
            )
            and (end := _date(period)) in ends
        ):
            stamps[period] = ends[end]
    results = [f for f in facts if f.kind not in ("guide", "update")]
    headline = next((f for f in results if f.role == "headline"), None)
    counts = Counter(stamps[f.period] for f in results)
    main = stamps[headline.period] if headline else (counts.most_common(1)[0][0] if counts else "")
    # The main quarter's end date, so its balance-sheet figures join it
    main_end = next((end for end, stamp in ends.items() if stamp == main), None)
    if main_end is None and (calendar := re.fullmatch(r"Q([1-4]) (20\d{2})", main)):
        main_end = (int(calendar[2]), *_QUARTER_END[calendar[1]])
    if main_end:
        for period, stamp in stamps.items():
            if _point_in_time(stamp) == main_end:
                stamps[period] = main

    def ranked(group: list[SummaryFact]) -> list[SummaryFact]:
        return sorted(group, key=lambda f: _ROLE_ORDER.get(f.role, 2))

    sections: dict[tuple[str, str], list[SummaryFact]] = {}
    for fact in facts:
        kind = {"guide": "Outlook", "update": "Business updates"}.get(fact.kind, "Results")
        stamp = "" if kind == "Business updates" else stamps[fact.period]
        sections.setdefault((kind, stamp), []).append(fact)
    # Headline results, then the outlook, then the rest of the quarter
    quarter = ranked(sections.get(("Results", main), []))
    lead = [f for f in quarter if f.role == "headline"]
    if lead and len(lead) < len(quarter) and any(kind == "Outlook" for kind, _ in sections):
        sections[("Results", main)] = lead
        sections[("More results", main)] = quarter[len(lead) :]
    rank = {"Outlook": 1, "More results": 2, "Results": 3, "Business updates": 4}
    order = sorted(sections, key=lambda key: 0 if key == ("Results", main) else rank[key[0]])
    blocks = []
    for kind, stamp in order:
        group = ranked(sections[(kind, stamp)])
        if kind == "Business updates":
            lines = [
                _line(f, "🔸", "" if stamps[f.period] == main else stamps[f.period]) for f in group
            ]
        elif kind in ("Results", "More results"):
            lines = _results_lines(group)
        else:
            lines = [_line(f, "🔹") for f in group]
        blocks.append(f"**{f'{stamp} · {kind}'.strip(' ·')}**\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def render_facts(ticker: str, facts: list[SummaryFact]) -> str:
    # No length budget and no automatic importance filter. Selection belongs upstream.
    return "\n".join([f"${ticker}", *(format_fact(fact) for fact in facts)])
