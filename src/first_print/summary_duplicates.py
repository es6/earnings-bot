"""Combine repeated, already verified claims; different bases and periods remain distinct."""

import re
from dataclasses import replace

from first_print.binding_text import metric_key
from first_print.summary_policy import SummaryFact, change_reference


def _key(text: str) -> str:
    text = re.sub(r"\bnon[- ]?(?:gaap|ifrs)\b|\badjusted\b", "adjusted", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:as reported|reported|gaap|ifrs|unaudited)\b", "", text, flags=re.IGNORECASE
    )
    return " ".join(re.findall(r"\w+", text.casefold()))


def _growth(fact: SummaryFact) -> bool:
    return not fact.value.strip() or bool(
        re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*%", fact.value.strip())
        and re.search(r"\b(?:growth|increase|decrease|change)\b", fact.metric, re.IGNORECASE)
    )


def _identity(fact: SummaryFact) -> tuple[str, ...]:
    metric = fact.metric
    if _growth(fact):
        metric = re.sub(r"\b(?:growth|increase|decrease|change)\b", "", metric, flags=re.IGNORECASE)
    # Accounting bases are checked separately: metric_key intentionally strips them.
    basis = _key(fact.basis)
    accounting = bool(
        re.search(r"adjusted|non[- ]?(?:gaap|ifrs)", f"{fact.metric} {fact.basis}", re.IGNORECASE)
    )

    def plain(text: str) -> str:
        return " ".join(re.findall(r"\w+", text.casefold()))

    return (
        metric_key(metric),
        plain(fact.group),
        plain(fact.period),
        str(accounting),
        " ".join(w for w in basis.split() if w != "adjusted"),
        "guide" if fact.kind == "guide" else "results",
    )


def _same(left: str, right: str) -> bool:
    # Only equivalent spellings, never rounding a count or treating close numbers as duplicates.
    def normalized(value: str) -> str:
        value = re.sub(r"(?<=\d)\.0+(?=\D|$)", "", value)
        return " ".join(value.casefold().split()).replace(",", "")

    return normalized(left) == normalized(right)


def combine_duplicates(facts: list[SummaryFact]) -> list[SummaryFact]:
    """No relevance filter or length budget. Keep a conflicting optional component separately."""
    result: list[SummaryFact] = []
    for fact in facts:
        if fact.kind == "update":
            # Narrative paraphrases can contain different constraints. Only exact content repeats.
            if not any(
                old.kind == "update"
                and _identity(old) == _identity(fact)
                and _same(old.value, fact.value)
                and _same(old.change, fact.change)
                for old in result
            ):
                result.append(fact)
            continue
        for index, old in enumerate(result):
            if _identity(old) != _identity(fact):
                continue
            old_rate, rate = _growth(old), _growth(fact)
            if old_rate != rate:
                level, growth = (fact, old) if old_rate else (old, fact)
                change = growth.change or growth.value
                comparison = change_reference(growth)
                if (
                    not level.value
                    or not change
                    or not comparison
                    or (level.currency and growth.currency and level.currency != growth.currency)
                    or (level.change and not _same(level.change, change))
                    or (change_reference(level) and change_reference(level) != comparison)
                ):
                    continue
                result[index] = replace(level, change=change, comparison=comparison)
                break
            if (
                not _same(old.value, fact.value)
                or _key(old.unit) != _key(fact.unit)
                or old.currency != fact.currency
                or any(
                    a and b and not _same(a, b)
                    for a, b in (
                        (old.change, fact.change),
                        (old.prior, fact.prior),
                        (change_reference(old), change_reference(fact)),
                        (old.estimate, fact.estimate),
                        (old.estimate_result, fact.estimate_result),
                    )
                )
            ):
                continue
            result[index] = replace(
                old,
                change=old.change or fact.change,
                prior=old.prior or fact.prior,
                comparison=old.comparison or fact.comparison,
                estimate=old.estimate or fact.estimate,
                estimate_result=old.estimate_result or fact.estimate_result,
            )
            break
        else:
            result.append(fact)
    return result
