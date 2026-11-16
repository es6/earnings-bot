"""Source-bound identities and metadata. Unknown or conflicting context stays unknown.

A claim is checked for what it asserts: its measure, value, sign, scale, period and segment,
and an adjusted or reported basis. Descriptive metadata the source can't confirm (a model's
own label in `basis`, a currency on a count) is not a claim and is dropped, not trusted.
"""

# En dashes and minus signs are real separators and signs in release text.
# ruff: noqa: RUF001

import re
from decimal import Decimal, InvalidOperation

from earnings_bot.summary_evidence import SourcedValue
from earnings_bot.summary_validation import _duration

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
    r"|\((thousands|millions|billions)(?: of (?:U\.?S\.? )?dollars)?\)"
    r"|\b(?:dollars|amounts?) in (thousands|millions|billions)\b"
    # ServiceNow's column header "Amount ($ millions)", JPM's "($ millions, except per share
    # data)"
    r"|\(\s*[$€£¥]\s*(thousands|millions|billions)\b",
    re.IGNORECASE,
)
_ADJUSTED = re.compile(r"non[- ]?(?:gaap|ifrs)|\badjusted\b|\badj\.?(?=\s)", re.IGNORECASE)
_REPORTED = re.compile(r"(?<!non-)(?<!non )\b(?:gaap|ifrs|as reported|reported)\b", re.IGNORECASE)
# Words that name no measure: "Total revenue" and "revenue" are one measure
_FILLER = {
    "total",
    "the",
    "a",
    "an",
    "record",
    "of",
    "for",
    "in",
    "its",
    "our",
    "quarterly",
    "reported",
    "gaap",
    "ifrs",
    "non",
    "basis",
    "on",
    "company",
    "consolidated",
}
# A different measure between a metric's name and a number means the number is its value
MEASURES = {
    "revenue",
    "sales",
    "income",
    "earnings",
    "eps",
    "ebitda",
    "ebit",
    "margin",
    "profit",
    "loss",
    "cash",
    "flow",
    "expense",
    "expenses",
    "bookings",
    "arr",
    "rpo",
    "crpo",
    "backlog",
    "assets",
    "capital",
    "dividend",
    "dividends",
    "users",
    "customers",
    "orders",
    "deliveries",
    "shares",
    "capex",
    "expenditures",
    "liquidity",
    "debt",
    "ratio",
    "rate",
    "yield",
    "volume",
    "subscribers",
    "members",
    "transactions",
    "gmv",
    "gov",
    "tpv",
    "units",
    "headcount",
    "spending",
    "spend",
    "investment",
    "investments",
    "obligations",
    "deposits",
    "loans",
    # Banks' return and capital ratios, as their releases abbreviate them
    "roe",
    "rotce",
    "roa",
    "cet1",
}
_ALIASES = [
    (r"\bearnings per diluted (?:ordinary |common )?share\b", "diluted eps"),
    (r"\bdiluted earnings per (?:ordinary |common )?share\b", "diluted eps"),
    (r"\bbasic earnings per (?:ordinary |common )?share\b", "basic eps"),
    (r"\bearnings per (?:ordinary |common )?share\b", "eps"),
    (r"\bnet loss per share\b", "eps"),
    (r"\bnet income per share\b", "eps"),
    (r"\brevenues\b", "revenue"),
    (r"\bannualized recurring revenue\b", "arr"),
    (r"\bcurrent remaining performance obligations?\b", "crpo"),
    (r"\bremaining performance obligations?\b", "rpo"),
    (r"\bcurrent rpo\b", "crpo"),
    (r"\bnet cash provided by operating activities\b", "operating cash flow"),
    (r"\bcash flows? from operations\b", "operating cash flow"),
    (r"\bcash flows? from operating activities\b", "operating cash flow"),
    (r"\breturn on (?:average )?tangible common equity\b", "rotce"),
    (r"\breturn on (?:average )?(?:common )?equity\b", "roe"),
    (r"\bcommon equity tier 1\b", "cet1"),
    (r"\bads? revenues?\b", "advertising revenue"),
    (r"\bincome from operations\b", "operating income"),
    (r"\boperating income margin\b", "operating margin"),
    (r"\bmaus?\b", "monthly active users"),
    (r"\bdaus?\b", "daily active users"),
    (r"\byear[- ]over[- ]year\b|\by/y\b", "yoy"),
    (r"\bquarter[- ]over[- ]quarter\b|\bq/q\b", "qoq"),
]


# A word, with an ampersand inside it kept: "sg&a", "r&d", but not "engines & services"
WORD = r"[a-z0-9]+(?:&[a-z0-9]+)*"
# A prefix that is also written fused: "pre-tax" is "pretax", "e-commerce" is "ecommerce".
# Others stay apart, as releases write them both ways ("ex-fuel", "ex. fuel")
_PREFIXES = r"pre|post|co|after|multi|e"
_COMPOUND = re.compile(rf"\b({_PREFIXES})-(?=[a-z])")
FUSED = rf"(?:\b(?:{_PREFIXES})-)?"  # a fused prefix before a WORD, for spans of the text


def words(text: str) -> str:
    return " ".join(re.findall(WORD, _COMPOUND.sub(r"\1", text.casefold())))


def normalized(text: str) -> str:
    """Lower case, footnote digits dropped (share1 -> share), aliases applied."""
    # A footnote digit follows a word of at least three letters, alone or in a list
    # ("LTM12,13"); Q3 and FY27 keep theirs, and so does an amount ("EUR1.2 billion")
    text = re.sub(r"(?<=[A-Za-z]{3})\d{1,2}(?:,\d{1,2})*\b(?![.,]\d)|\(\d{1,2}\)|\*+", "", text)
    text = text.casefold().replace("’", "'")
    for pattern, alias in _ALIASES:
        text = re.sub(pattern, alias, text)
    return text


def stem(word: str) -> str:
    """A crude stem, so a measure's name matches its other forms: openings / opened,
    restaurants / restaurant, repurchases / repurchased. Used on both sides of a match."""
    if len(word) < 4 or word.isdigit():
        return word
    for suffix in ("ings", "ing", "ed", "es", "e", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def stems(text: str) -> list[str]:
    return [stem(w) for w in re.findall(WORD, _COMPOUND.sub(r"\1", text))]


MEASURE_STEMS = frozenset(stem(w) for w in MEASURES)


# A period is not part of a measure's name: "openings in 2026", "full year tax rate"
_PERIOD_WORDS = re.compile(
    r"^(?:20\d{2}|q[1-4]|[1-4]q|fy\d{0,4}|h[12]|fiscal|quarter|quarterly|full|year|annual|"
    r"estimated|ytd|month|months|three|six|nine|twelve)$"
)


def metric_words(metric: str) -> list[str]:
    """The words that identify a measure, in order and stemmed, without its basis, period
    or abbreviation."""
    text = re.sub(r"\(([A-Za-z]{2,6})\)", "", metric)  # "(RPO)", "(ARR)"
    # A definition in parentheses: "Operating margin (income from operations as a percentage
    # of total revenue)". One that qualifies the number ("excluding Deliveroo") or states
    # numbers of its own ("-$50m, offset by +$36m") stays, as nothing else would check it.
    text = re.sub(
        r"\((?![^()]*\d)(?![^()]*\b(?:exclud\w*|ex|before|after|organic|constant|neutral|cc|"
        r"adjusted|"
        r"non-?gaap|gaap|ifrs|reported|underlying|comparable|pro forma)\b)[^()]{16,}\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<=\))\d{1,2}\b", "", text)  # a footnote mark: "(non-GAAP)3"
    text = re.sub(
        r"\((?:non-?gaap|gaap|ifrs|non-?ifrs|adjusted|reported)\)", "", text, flags=re.IGNORECASE
    )
    text = _ADJUSTED.sub(" ", text)
    return [
        stem(w)
        for w in words(normalized(text)).split()
        if w not in _FILLER and not _PERIOD_WORDS.match(w)
    ]


def metric_key(text: str) -> str:
    return " ".join(metric_words(text))


def basis_of(text: str) -> set[str]:
    """Which accounting bases a text names: adjusted (non-GAAP) and/or reported (GAAP)."""
    found = set()
    if _ADJUSTED.search(text):
        found.add("adjusted")
    if _REPORTED.search(_ADJUSTED.sub(" ", text)):
        found.add("reported")
    return found


def unit_kind(unit: str) -> str:
    """money, per share, %, bps, or count."""
    text = unit.casefold()
    if re.search(r"per (?:diluted |basic )?share|/share|\bper ads\b", text):
        return "per share"
    if re.search(r"basis points?|\bbps\b", text):
        return "bps"
    if re.search(r"%|percent|percentage|\bpct\b|\bpp\b|percentage points?", text):
        return "%"
    rest = re.sub(
        r"\b(?:money|dollars?|usd|us|eur|euros?|gbp|pounds?|jpy|yen|cad|aud|chf|rmb|cny|inr|"
        r"currency|u\.?s\.?|in|of|thousands?|millions?|billions?|trillions?|bn|m|k)\b|[$€£¥.]",
        " ",
        text,
    )
    return "money" if not rest.strip() else "count"


def count_words(unit: str) -> list[str]:
    """What a count counts: "million monthly active users" -> monthly active users."""
    text = re.sub(
        r"\b(?:thousands?|millions?|billions?|trillions?|number of|count|units? of)\b",
        " ",
        unit.casefold(),
    )
    return [w for w in words(text).split() if w not in ("of", "the", "a")]


def quarter(text: str) -> set[str]:
    text = normalized(text)
    for word, number in _QUARTERS.items():
        text = re.sub(rf"\b{word}[- ]quarter\b", f"q{number}", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfq([1-4])\b", r"q\1", text)
    return {
        a or b
        for a, b in re.findall(
            r"\bq([1-4])(?=\b|20\d{2}|'?\d{2}\b)|\b([1-4])q(?=\b|20\d{2}|'?\d{2})", text
        )
    }


def years(text: str) -> set[str]:
    """Four-digit years, with FY27 / Q3'26 / fiscal 27 read as 2027 / 2026. A footnote digit
    after a year ("July 31, 20261") is not part of it."""
    found = set(re.findall(r"(?<![\d.,$])(20\d{2})(?=\d?(?:\b|$))(?!\d{2})", text))
    found |= {
        f"20{y}"
        for y in re.findall(r"\b(?:FY|Q[1-4]|[1-4]Q|fiscal)\s*'?(\d{2})\b", text, re.IGNORECASE)
    }
    return found


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def fiscal_offset(text: str) -> int:
    """How far a release's fiscal year label runs ahead of the calendar year its period ends
    in: Walmart's second quarter of fiscal 2027 ended July 31, 2026, so 1. Read from the
    release itself, a fiscal label next to an end date; 0 when it says nothing."""
    for match in re.finditer(
        r"(?:fiscal(?: year)?|\bFY)\s*'?(20\d{2}|\d{2})\b", text, re.IGNORECASE
    ):
        fiscal = int(match[1]) if len(match[1]) == 4 else 2000 + int(match[1])
        window = text[max(0, match.start() - 250) : match.end() + 250]
        date = re.search(
            rf"(?:ended|ending|as of)\s+{_MONTH}\s+\d{{1,2}},?\s+(20\d{{2}})", window, re.IGNORECASE
        )
        if date and fiscal - int(date[1]) in (0, 1):
            return fiscal - int(date[1])
    return 0


def reporting_year(text: str) -> tuple[str, bool] | None:
    """The year of the period a release reports, from the first place it names it ("third
    quarter of fiscal 2026", "Q3 2026", "3Q26"), and whether that is a fiscal label."""
    match = re.search(
        r"(?:\b(?:first|second|third|fourth) quarter|\bQ[1-4]|\b[1-4]Q|full[- ]year|fiscal)"
        r"[^.\n]{0,30}?(?:(?:fiscal(?: year)?|FY)\s*'?)?(20\d{2}|'?\d{2}\b)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    found = years(match[0])
    if len(found) != 1:
        return None
    return next(iter(found)), bool(re.search(r"\bFY|fiscal", match[0], re.IGNORECASE))


def calendar_years(period: str, source: str, offset: int) -> set[str]:
    """A claim's years as the source writes them: a fiscal label ("Q2 FY27") compared with a
    column dated by the calendar ("July 31, 2026") moves back by the fiscal offset."""
    found = years(period)
    fiscal = re.search(r"\bFY|fiscal", period, re.IGNORECASE)
    if offset and fiscal and not re.search(r"\bFY|fiscal", source, re.IGNORECASE):
        return {str(int(y) - offset) for y in found}
    return found


def duration(text: str) -> int | None:
    """Months a period covers; 0 for a point in time (a balance at a date)."""
    # "Three months ending December 31" is a window, not a balance at that date.
    explicit = re.search(r"\b(three|six|nine|twelve|3|6|9|12)[ -]*months?\b", text, re.IGNORECASE)
    if explicit:
        return _duration(explicit[0])
    if re.search(r"\byear end(?:ed|ing)\b", text, re.IGNORECASE):
        return 12  # "Year ending December 31, 2026" is a year, not a balance at its end
    if re.search(
        r"\bas of\b|\bas at\b|point.in.time|quarter.end|\bexit(?:ing)?\b|\bending\b|"
        r"end of (?:the )?(?:quarter|period|year)|balance",
        text,
        re.IGNORECASE,
    ):
        return 0
    if re.fullmatch(r"\s*quarter(?:ly)?\s*", text, re.IGNORECASE):
        return 3
    if re.fullmatch(r"\s*(?:fiscal |full )?year\s*", text, re.IGNORECASE):
        return 12
    if re.search(
        r"\byear ended\b|\btwelve months\b|\bfiscal year\b|\bfull[- ]year\b", text, re.IGNORECASE
    ):
        return 12
    found = _duration(normalized(text))
    if (
        found is None
        and re.search(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? 20\d{2}\b",
            text,
            re.IGNORECASE,
        )
        and not re.search(r"ended|ending|months|quarter|year", text, re.IGNORECASE)
    ):
        return 0  # a bare date heads a balance-sheet column
    return found


def durations_in(text: str) -> set[int]:
    """Every duration a passage mentions, in months (0 for a point in time)."""
    text = re.sub(r"quarter of (?:the )?fiscal(?: year)?", "quarter", normalized(text))
    found = set()
    if re.search(r"\bas (?:of|at)\b|\bat (?:the )?(?:end of|quarter.end)|quarter.end\b", text):
        found.add(0)
    for word, months in (("three", 3), ("six", 6), ("nine", 9), ("twelve", 12)):
        if re.search(rf"\b(?:{word}|{months})[ -]months?\b", text):
            found.add(months)
    if re.search(r"\bq[1-4]\b|\b(?:first|second|third|fourth) quarter\b|\bquarterly\b", text):
        found.add(3)
    if re.search(r"\byear[- ]to[- ]date\b|\bytd\b|\bfirst half\b", text):
        found.add(-1)  # some year-to-date span
    if re.search(
        r"\b(?:full|fiscal)[- ]year\b|\byear end(?:ed|ing)\b|\bfy ?\d{2,4}\b(?! q)", text
    ) and not (re.search(r"\bq[1-4] ?fy", text)):
        found.add(12)
    return found


def _one(found: set[int]) -> int | None:
    return next(iter(found)) if len(found) == 1 else None


def period_issues(
    value: SourcedValue,
    local: str,
    near: str,
    far: str = "",
    *,
    comparative: bool,
    clause: bool = False,
    prior: bool = False,
    offset: int = 0,
    reported: tuple[str, bool] | None = None,
    change: bool = False,
    guide: bool = False,
) -> list[str]:
    """Year, quarter and duration. The column (or the claim's own clause) decides when it
    names them; then the nearby text, then the release's opening, where the latest year is
    the reporting period's. A passage naming several durations decides none. A fiscal label
    is compared with calendar dates through the release's own fiscal offset."""
    issues = []
    claimed = years(value.period)

    def reporting(found: set[str]) -> set[str]:
        # A clause naming this year and last year reports this year; its prior-year value
        # is last year's
        latest = int(max(found))
        return {str(latest - 1)} if prior else {str(latest)}

    if len(claimed) != 1:
        issues.append("period year is missing or ambiguous")
    elif local_years := years(local):
        mapped = calendar_years(value.period, local, offset)
        base = {str(int(y) - 1) for y in mapped}
        if change and local_years == base:
            pass  # a change column headed by the year it is compared with ("3Q25 O/(U) %")
        elif (mapped != local_years) if not clause else (mapped != reporting(local_years)):
            issues.append("period year conflicts with the source column")
    elif comparative:
        issues.append("period year is not established by the source column")
    else:
        source = near if years(near) else far
        found = years(source)
        mapped = calendar_years(value.period, source, offset)
        if guide:
            # An outlook is for a later period, which the text around it names
            if not found or not mapped <= found:
                issues.append("period year is not established by the source")
        else:
            if reported and reported[0] in found:
                found = {reported[0]}  # the release's own year, not an outlook year nearby
            elif reported and not found:
                found = {reported[0]}
                mapped = calendar_years(value.period, "FY" if reported[1] else "", offset)
            if not found or mapped != reporting(found):
                issues.append("period year is not established by the source")
    claimed_q = quarter(value.period)
    # Text near the value that names several quarters ("second quarter results ... guidance
    # for Q3") decides none for an actual; the release's own reporting quarter does
    near_q = quarter(near)
    source_q = (
        quarter(local) or (near_q if len(near_q) == 1 or guide else set()) or quarter(far) or near_q
    )
    if claimed_q and (len(claimed_q) != 1 or (source_q and claimed_q - source_q)):
        issues.append("fiscal quarter conflicts with the source")
    if claimed_q and not source_q and not years(local):
        issues.append("fiscal quarter is not established by the source")
    expected = duration(value.duration) if value.duration.strip() else None
    if expected is None:
        expected = duration(value.period)
    if expected in (3, 6, 9) and re.fullmatch(
        r"\s*(?:FY|fiscal year|full[- ]year)\s*20\d{2}\s*", value.period, re.IGNORECASE
    ):
        issues.append("fiscal year label conflicts with the claimed subannual duration")
    source = _one(durations_in(local))
    if guide and source is None and years(local) and not quarter(local) and not durations_in(local):
        # A forecast named by its year alone is the year's: "our expected 2026 FCF forecast"
        source = 12
    if guide:
        # For guidance, the cited passage's own period ("Full year 2026 earnings per share
        # expected..."), then the nearest heading with one ("For full year 2026:" below "For
        # Q4 2026, we expect:"), never the release title's "third quarter ... results"
        lines = near.splitlines()
        near = next(
            (
                line
                for line in lines[:1] + lines[:0:-1]
                if durations_in(line)
                and not re.search(r"\b(?:reports?|announces?|results)\b", line, re.IGNORECASE)
            ),
            "",
        )
    if source is None and not durations_in(local):
        source = _one(durations_in(near))
    if expected in (6, 9) and not durations_in(f"{local}\n{near}"):
        # A year-to-date amount needs the source to say so; a highlight states the quarter
        issues.append("a year-to-date duration is not established by the source")
    # A balance at the quarter's end belongs to that quarter
    if (
        source is not None
        and source >= 0
        and expected is not None
        and source != expected
        and not (source == 0 and expected in (3, 12) and (claimed_q or years(value.period)))
    ):
        issues.append("duration conflicts with the source column")
    return issues


def currencies(text: str) -> set[str]:
    found = set(
        re.findall(r"\b(?:USD|EUR|GBP|CAD|JPY|RMB|CNY|AUD|CHF|SEK|INR|BRL)\b", text.upper())
    )
    if re.search(r"\b(?:C|CA|A|AU|HK|NT|S|R)\$", text):
        found.add("OTHER$")
    elif "$" in text or re.search(r"U\.S\. dollars?|\bdollars\b", text, re.IGNORECASE):
        found.add("USD")
    if "€" in text or re.search(r"\beuros?\b", text, re.IGNORECASE):
        found.add("EUR")
    if "£" in text:
        found.add("GBP")
    if "¥" in text:
        found.add("JPY")
    return found


def source_scale(text: str, per_share: bool = False) -> str | None:
    if per_share:
        return "one"
    scales = {"".join(found).casefold() for found in _SCALE.findall(text)}
    if len(scales) > 1:
        return "ambiguous"
    return next(iter(scales)).removesuffix("s") if scales else None


_QUALIFIERS = {
    "approximately": r"\b(?:about|approximately|roughly|around|approx\.?)\b|~",
    "nearly": r"\b(?:nearly|almost)\b",
    "more_than": r"\b(?:over|more than|in excess of|exceed(?:ed|ing)?|above)\b|(?<![<>=])>(?!=)",
    "at_least": r"\bat least\b|>=",
    "less_than": r"\b(?:under|less than|below)\b|(?<![<>=])<(?!=)",
    "at_most": r"\b(?:up to|at most)\b|<=",
}


def source_qualifiers(text: str) -> set[str]:
    """The bounds or approximations a text puts on a number: "about", "more than", "up to"."""
    # "year-over-year" and "quarter-over-quarter" say nothing about a bound
    text = re.sub(
        r"(?:year|quarter|month|period)[- ]over[- ](?:year|quarter|month|period)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return {key for key, pattern in _QUALIFIERS.items() if re.search(pattern, text, re.IGNORECASE)}


def qualifier_issues(value: SourcedValue, text: str) -> list[str]:
    observed = source_qualifiers(text)
    expected = set() if value.value_qualifier == "exact" else {value.value_qualifier}
    return (
        [] if observed == expected else ["bound/approximation qualifier conflicts with the source"]
    )


_TOKEN = re.compile(
    r"(?P<neg>\((?=\s*(?:US\$|C\$|A\$|[$€£¥])?\s*\d)|(?<![\w.%)/])-|−)?\s*"
    r"(?P<cur>US\$|C\$|A\$|[$€£¥])?\s*(?P<neg2>\(|(?<![\w.%)/])-|−)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<close>\))?\s*"
    r"(?P<suffix>%|percent\b|bps\b|basis points\b|trillion\b|billion\b|million\b|thousand\b|"
    r"bn\b|[KMBT]\b)?",
    re.IGNORECASE,
)
_SUFFIX_SCALE = {
    "trillion": "trillion",
    "t": "trillion",
    "billion": "billion",
    "bn": "billion",
    "b": "billion",
    "million": "million",
    "m": "million",
    "thousand": "thousand",
    "k": "thousand",
}


def _step(digits: str) -> Decimal:
    places = len(digits.partition(".")[2])
    return Decimal(10) ** -places


def number_tokens(text: str, caption: str = "") -> list[tuple[int, int, Decimal, Decimal, str]]:
    """Each number in a text: start, end, amount in base units, its precision in base units,
    and dimension (number, %, or bps). "(37.5)" and "-37.5" are negative; a caption's scale
    applies to numbers that state none; bare years are skipped."""
    found: list[tuple[int, int, Decimal, Decimal, str]] = []
    unscaled = False  # the previous number stated no scale of its own
    for match in _TOKEN.finditer(text):
        raw = match["num"].replace(",", "")
        suffix = (match["suffix"] or "").casefold()
        negative = bool(match["neg"] or match["neg2"])
        if "(" in (match["neg"] or "", match["neg2"] or "") and not match["close"]:
            negative = bool(match["neg"] not in (None, "(") or match["neg2"] not in (None, "("))
        if not suffix and not match["cur"] and re.fullmatch(r"(?:19|20)\d{2}", raw):
            continue  # a year
        try:
            amount, step = Decimal(raw), _step(raw)
        except InvalidOperation:
            continue
        hyphen = "-" in (match["neg"] or "", match["neg2"] or "")
        sign = match.start("neg") if match["neg"] else match.start("neg2")
        # "$2.649 - $2.653 billion": a hyphen right after another number is a range's dash,
        # not a minus, and the scale written once at the end is both ends' scale
        dash = hyphen and bool(found) and not text[found[-1][1] : sign].strip()
        ranged = dash or (
            bool(found)
            and bool(re.fullmatch(r"\s*(?:to|and|-|–|—)\s*", text[found[-1][1] : match.start()]))
        )
        if dash:
            negative = False
        if suffix in ("%", "percent"):
            dimension = "%"
        elif suffix in ("bps", "basis points"):
            dimension = "bps"
        else:
            dimension = "number"
            named = _SUFFIX_SCALE.get(suffix)
            scale = SCALES.get(named or caption or "one", Decimal(1))
            amount, step = amount * scale, step * scale
            if ranged and named and unscaled and found[-1][4] == "number":
                start, end, low, low_step, _ = found[-1]
                found[-1] = (start, end, low * scale, low_step * scale, "number")
        unscaled = dimension == "number" and not suffix and not caption
        start = (
            match.start("num")
            if dash
            else (match.start("neg") if match["neg"] else match.start("num"))
        )
        found.append((start, match.end(), -amount if negative else amount, step, dimension))
    return found


def claimed_amounts(value: SourcedValue) -> tuple[list[tuple[Decimal, Decimal]], str]:
    """The claim's amount (and range end) in base units, each with its stated precision."""
    kind = unit_kind(value.unit)
    dimension = "%" if kind == "%" else "bps" if kind == "bps" else "number"
    scale = Decimal(1) if dimension != "number" else SCALES[value.scale]
    ends = [value.amount] + ([value.upper] if value.upper is not None else [])
    return [(Decimal(end) * scale, _step(end.lstrip("+-")) * scale) for end in ends], dimension


def matches(claim: tuple[Decimal, Decimal], source: Decimal, source_step: Decimal) -> bool:
    """The source states this number: it is at least as precise as the claim and rounds to it.
    "6.76 billion" matches 6,760 million; it does not match a source's 6.8 billion."""
    amount, step = claim
    return source == amount or (source_step <= step and abs(source - amount) <= step / 2)
