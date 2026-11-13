"""Presentation and arithmetic from bound values only; selection has no word cap."""

from decimal import ROUND_HALF_UP, Decimal

from earnings_bot.binding_text import SCALES, duration
from earnings_bot.summary_binding import BindingReport
from earnings_bot.summary_evidence import ReferencedFact, SourcedValue
from earnings_bot.summary_format import discord_brief, format_value
from earnings_bot.summary_policy import SummaryFact

_CURRENCY = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CAD": "CAD ",
    "CNY": "CNY ",
    "RMB": "RMB ",
}
_QUALIFIER = {
    "exact": "",
    "approximately": "about ",
    "nearly": "nearly ",
    "more_than": "more than ",
    "at_least": "at least ",
    "less_than": "less than ",
    "at_most": "up to ",
}


def value_text(value: SourcedValue, *, include_comparison_qualifier: bool = True) -> str:
    currency = _CURRENCY.get(value.currency, f"{value.currency} " if value.currency else "")
    scale = "" if value.scale == "one" else " " + value.scale
    raw = f"{currency}{value.amount}"
    if value.upper is not None:
        raw += f" to {currency}{value.upper}"
    raw += scale
    if value.unit in ("%", "bps"):
        raw = f"{value.amount}{value.unit}"
        if value.upper is not None:
            raw = f"{value.amount}{value.unit} to {value.upper}{value.unit}"
    shown = format_value(_QUALIFIER[value.value_qualifier] + raw)
    if value.unit not in ("money", "%", "bps"):
        shown += "/share" if "per share" in value.unit else " " + value.unit
    if include_comparison_qualifier and value.comparison_qualifier:
        shown += f" ({value.comparison_qualifier})"
    return shown


def _change(value: SourcedValue) -> str:
    shown = value_text(value, include_comparison_qualifier=False)
    if Decimal(value.amount) > 0 and value.value_qualifier == "exact":
        shown = "+" + shown
    if value.comparison:
        shown += " " + value.comparison
    if value.comparison_qualifier:
        shown += " " + value.comparison_qualifier
    return shown


def _computed(current: SourcedValue, prior: SourcedValue | None) -> str:
    if prior is None:
        return ""
    now = Decimal(current.amount) * SCALES[current.scale]
    before = Decimal(prior.amount) * SCALES[prior.scale]
    if (
        current.upper is not None
        or prior.upper is not None
        or before <= 0
        or now < 0
        or current.value_qualifier != "exact"
        or prior.value_qualifier != "exact"
    ):
        return f"vs {value_text(prior)} a year ago"
    if current.unit == "%":
        amount = ((now - before) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{amount:+} bps YoY"
    growth = (now / before - 1) * 100
    growth = growth.quantize(
        Decimal(1) if abs(growth) >= 10 else Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{growth:+}% YoY"


def _display(
    fact: ReferencedFact,
    value: SourcedValue,
    *,
    kind: str | None = None,
    label: str | None = None,
    change: str = "",
) -> SummaryFact:
    return SummaryFact(
        metric=label or fact.metric,
        value=value_text(value),
        source=" ".join(ref.quote for ref in value.evidence),
        kind=kind or fact.kind,
        period=_display_period(value),
        basis=value.basis,
        unit=value.unit,
        currency="",
        group=fact.group,
        change=change,
        role=fact.role,
        relevance=fact.relevance,
    )


def _display_period(value: SourcedValue) -> str:
    span = duration(value.duration)
    if value.duration.strip() and span is not None and span != duration(value.period):
        return f"{value.period} ({value.duration})".strip()
    return value.period


def display_facts(report: BindingReport) -> list[SummaryFact]:
    if not isinstance(report, BindingReport):
        raise TypeError("Only a BindingReport can enter the bound renderer")
    result = []
    for bound in report.facts:
        fact = bound.fact
        actual, prior = fact.current_actual, fact.prior_year_actual
        changes = [v for v in (fact.reported_growth, fact.comparable_growth) if v is not None]
        if actual:
            change = (
                _change(fact.reported_growth) if fact.reported_growth else _computed(actual, prior)
            )
            if fact.comparable_growth:
                change = "; ".join(
                    part for part in (change, _change(fact.comparable_growth)) if part
                )
            result.append(
                _display(
                    fact,
                    actual,
                    kind="segment" if fact.kind == "segment" else "actual",
                    change=change,
                )
            )
        elif changes:
            periods: dict[str, list[SourcedValue]] = {}
            for change in changes:
                periods.setdefault(_display_period(change), []).append(change)
            for period, growths in periods.items():
                result.append(
                    SummaryFact(
                        fact.metric,
                        "",
                        " ".join(ref.quote for v in growths for ref in v.evidence),
                        kind="segment" if fact.kind == "segment" else "actual",
                        period=period,
                        basis=growths[0].basis,
                        group=fact.group,
                        change="; ".join(_change(v) for v in growths),
                        role=fact.role,
                        relevance=fact.relevance,
                    )
                )
        elif prior:
            result.append(_display(fact, prior, kind="actual"))
        new, old = fact.new_guidance, fact.previous_guidance
        if new:
            change = f"from {value_text(old)}" if old else ""
            result.append(_display(fact, new, kind="guide", change=change))
        elif old:
            result.append(
                _display(fact, old, kind="guide", label=f"Previous {fact.metric} guidance")
            )
        if fact.update:
            # A topic label is model-selected; display the exact source assertion.
            result.append(
                SummaryFact(
                    "Update",
                    fact.update,
                    " ".join(ref.quote for ref in fact.update_evidence),
                    kind="update",
                    role="update",
                )
            )
    return result


def render_bound(report: BindingReport) -> str:
    return discord_brief(display_facts(report))
