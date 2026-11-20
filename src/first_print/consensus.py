"""Period/currency/basis matching for prefetched analyst estimates. No network here."""

import csv
import io
import re
from calendar import monthrange
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from first_print.binding_text import period_end
from first_print.sec_results import reported_period
from first_print.source_document import SourceDocument
from first_print.summary_format import format_period, format_value
from first_print.summary_policy import SummaryFact
from first_print.summary_verify import parse, unit_of


def decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def calendar_rows(raw: str) -> list[dict[str, str]]:
    if raw.lstrip().startswith("{"):
        raise ValueError("Alpha Vantage returned a notice instead of its earnings calendar")
    rows = csv.DictReader(io.StringIO(raw))
    required = {"symbol", "reportDate", "fiscalDateEnding", "currency"}
    if not required <= set(rows.fieldnames or ()):
        raise ValueError("Alpha Vantage calendar fields are missing")
    return list(rows)


def estimate_rows(data: dict[str, Any], symbol: str, currency: str) -> list[dict[str, str]]:
    if data.get("symbol") != symbol or not isinstance(data.get("estimates"), list):
        raise ValueError("Alpha Vantage estimate identity/data unavailable")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Estimate currency is not verified")
    result = []
    for row in data["estimates"]:
        if not isinstance(row, dict) or row.get("horizon") not in ("fiscal quarter", "fiscal year"):
            continue
        try:
            ending = date.fromisoformat(row["date"])
        except (ValueError, KeyError, TypeError):
            continue
        for metric in ("revenue", "eps"):
            amount = decimal(row.get(f"{metric}_estimate_average"))
            analysts = decimal(row.get(f"{metric}_estimate_analyst_count"))
            if amount is None or analysts is None or analysts < 1:
                continue
            if metric == "revenue" and amount < 0:
                continue
            # The calendar's EPS currency does not establish revenue-estimate currency.
            # Current AV estimate responses omit this metadata: cache it, hide comparison.
            stated_currency = str(row.get(f"{metric}_currency") or row.get("currency") or "")
            metric_currency = stated_currency if re.fullmatch(r"[A-Z]{3}", stated_currency) else ""
            if metric == "eps" and not metric_currency:
                metric_currency = currency
            # EPS alone does not establish adjusted/GAAP, or ordinary-share/ADS comparability.
            basis = str(row.get("eps_basis") or "").casefold() if metric == "eps" else "reported"
            result.append(
                {
                    "metric": metric,
                    "amount": str(amount),
                    "ending": ending.isoformat(),
                    "horizon": row["horizon"],
                    "currency": metric_currency,
                    "basis": basis,
                    "unit": str(row.get("eps_unit") or "") if metric == "eps" else "money",
                    "analysts": str(analysts),
                }
            )
    return result


def _end_for(period: str, current: tuple[int, int], end: date) -> tuple[date, str] | None:
    stamp = format_period(period)
    quarter = re.fullmatch(r"Q([1-4]) (?:FY)?(20\d{2})", stamp)
    annual = re.fullmatch(r"FY(20\d{2})", stamp)
    if quarter:
        target_q, year, horizon = int(quarter[1]), int(quarter[2]), "fiscal quarter"
    elif annual:
        target_q, year, horizon = 4, int(annual[1]), "fiscal year"
    else:
        return None  # YTD, rolling windows and snapshots are not quarterly consensus.
    months = (year - current[0]) * 12 + (target_q - current[1]) * 3
    total = end.year * 12 + end.month - 1 + months
    next_year, month = divmod(total, 12)
    month += 1
    return date(next_year, month, min(end.day, monthrange(next_year, month)[1])), horizon


def attach_estimates(
    facts: list[SummaryFact],
    document: SourceDocument,
    snapshot: dict[str, Any],
    *,
    ticker: str,
    published_at: datetime | None = None,
) -> list[SummaryFact]:
    """Require pre-window and pre-publication retrieval plus matching financial metadata."""
    try:
        fetched = datetime.fromisoformat(snapshot["fetched_at"])
        cutoff = datetime.fromisoformat(snapshot["window_start"])
    except (KeyError, TypeError, ValueError):
        return facts
    current = reported_period(document.text)
    if (
        snapshot.get("symbol") != ticker
        or current is None
        or snapshot.get("period") != f"FY{current[0]}Q{current[1]}"
    ):
        return facts
    end = period_end(document.text)
    if end is None:
        return facts
    if (
        fetched.tzinfo is None
        or cutoff.tzinfo is None
        or published_at is None
        or published_at.tzinfo is None
        or fetched >= published_at
        or fetched >= cutoff
        or cutoff - fetched > timedelta(days=1)
    ):
        return facts
    result = []
    for fact in facts:
        result.append(fact)
        if fact.kind not in ("actual", "guide") or fact.group.strip():
            continue
        metric = re.sub(
            r"\b(?:total|net|diluted|basic|adjusted|non-gaap|non-ifrs|gaap|ifrs|guidance|outlook)\b",
            "",
            fact.metric,
            flags=re.IGNORECASE,
        )
        metric = " ".join(re.findall(r"[a-z]+", metric.casefold()))
        if metric in ("revenue", "revenues", "sales"):
            name = "revenue"
            if re.search(
                r"adjusted|non[- ]?(?:gaap|ifrs)|constant|organic|neutral",
                f"{fact.metric} {fact.basis}",
                re.IGNORECASE,
            ):
                continue
        elif metric in ("eps", "earnings per share"):
            name = "eps"
        else:
            continue
        target = _end_for(fact.period, current, end)
        value = parse(fact.value)
        if target is None or value is None or not fact.currency:
            continue
        if unit_of(fact, fact.value) != ("money" if name == "revenue" else "per share"):
            continue
        wanted, horizon = target
        candidates = []
        for row in snapshot.get("estimates", []):
            try:
                dated = date.fromisoformat(row["ending"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                row.get("metric") != name
                or row.get("horizon") != horizon
                or row.get("currency") != fact.currency
            ):
                continue
            # Vendor calendar month-end versus a 52/53-week issuer's period end.
            if abs(dated - wanted) > timedelta(days=7):
                continue
            if name == "eps":
                if not re.search(
                    r"\b(?:adjusted|non[- ]?(?:gaap|ifrs)|gaap|ifrs)\b",
                    f"{fact.metric} {fact.basis}",
                    re.IGNORECASE,
                ):
                    continue
                basis = (
                    "adjusted"
                    if re.search(
                        r"adjusted|non[- ]?(?:gaap|ifrs)",
                        f"{fact.metric} {fact.basis}",
                        re.IGNORECASE,
                    )
                    else "gaap"
                )
                if row.get("basis") != basis or row.get("unit") != "per share":
                    continue
                if re.search(r"\bADS\b|depositary", f"{fact.metric} {fact.unit}", re.IGNORECASE):
                    continue
            candidates.append(row)
        if len(candidates) != 1:
            continue
        expected = decimal(candidates[0].get("amount"))
        if expected is None:
            continue
        factor = {
            "one": 1,
            "thousand": 10**3,
            "million": 10**6,
            "billion": 10**9,
            "trillion": 10**12,
        }[value[2]]
        # A unit-supplied scale must be explicit; never compare 43.4 to 43.4 billion.
        unit_scale = re.search(
            r"\b(thousand|million|billion|trillion)s?\b", fact.unit, re.IGNORECASE
        )
        if factor == 1 and name == "revenue" and unit_scale:
            factor = {"thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}[
                unit_scale[1].lower()
            ]
        lower, upper = Decimal(value[0]) * factor, Decimal(value[1] or value[0]) * factor
        mark = ""
        if value[3] == "exact" and not re.search(
            r"plus or minus|±|\+/-", fact.value, re.IGNORECASE
        ):
            mark = "above" if lower > expected else "below" if upper < expected else "inline"
        rendered = format_value(
            str(expected), "per share" if name == "eps" else "money", fact.currency
        )
        if fact.currency not in ("USD", "EUR", "GBP", "JPY"):
            compact = format_value(str(expected), "money", "USD").replace("$", "")
            rendered = f"{fact.currency} {compact}"
        result[-1] = replace(fact, estimate=rendered, estimate_result=mark)
    return result
