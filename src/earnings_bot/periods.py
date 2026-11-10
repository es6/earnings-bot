"""Fiscal periods as issuers write them in release headlines."""

import re

NTH = ("first", "second", "third", "fourth")
ORD = ("1st", "2nd", "3rd", "4th")
_QUARTER = rf"(?:Q\s*([1-4])|({'|'.join(NTH + ORD)})[-\s]+quarter)"
_PERIOD = re.compile(
    rf"\b{_QUARTER}"
    r"\s+(?:(?:and|&)\s+(?:(?:full|first|second)[-\s]+)?(?:half[-\s]+)?|of\s+)?"
    r"(?:fiscal\s+)?(?:year\s+)?(?:FY\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_YEAR_FIRST = re.compile(
    rf"\b(?:(?:fiscal(?:\s+year)?|FY)\s*)?(20\d{{2}})\s+{_QUARTER}\b",
    re.IGNORECASE,
)
_COMPACT = re.compile(r"\b(?:([1-4])Q\s*|Q([1-4])\s*['\u2019]?)(?:20)?([0-9]{2})\b", re.IGNORECASE)
_SHORT_YEAR = re.compile(r"\bFY\s?(\d{2})\b", re.IGNORECASE)
_QUARTER_ONLY = re.compile(rf"\b(?:{_QUARTER}|([1-4])Q)\b", re.IGNORECASE)


def _quarter(digit: str | None, ordinal: str | None) -> int:
    return (
        int(digit)
        if digit
        else next(
            i
            for i, (name, short) in enumerate(zip(NTH, ORD, strict=True), 1)
            if ordinal and ordinal.lower() in (name, short)
        )
    )


def title_period(title: str) -> tuple[int, int] | None:
    """Read an explicit fiscal year and quarter, including Q3'26 and 3Q2026."""
    title = _SHORT_YEAR.sub(r"FY20\1", title)
    match = min(
        (found for pattern in (_PERIOD, _YEAR_FIRST, _COMPACT) if (found := pattern.search(title))),
        key=lambda found: found.start(),
        default=None,
    )
    if match is None:
        return None
    if match.re is _PERIOD:
        digit, ordinal, year = match.groups()
    elif match.re is _YEAR_FIRST:
        year, digit, ordinal = match.groups()
    else:
        first, second, short_year = match.groups()
        return 2000 + int(short_year), int(first or second)
    return int(year), _quarter(digit, ordinal)


def agrees_with_period(title: str, expected: str | None) -> bool:
    if not expected:
        return True
    year, quarter = int(expected[2:6]), int(expected[-1])
    if period := title_period(title):
        return period == (year, quarter)
    if match := _QUARTER_ONLY.search(title):
        digit, ordinal, reversed_digit = match.groups()
        return _quarter(digit or reversed_digit, ordinal) == quarter
    return True
