"""Opt-in request variants for measuring examples, selection checks and bounded repair.

These helpers do not make calls or change the live writer's default request. Every
repair answer must go through source binding again; model assurances are not evidence.
"""

import copy
import json
from typing import Any

from earnings_bot.summary_binding import BindingReport

# The owner's November 11 reviews: round 3 FICO ("perfect") and round 6 Uber C
# ("clearly the best"). Selection and values are preserved; segment units are explicit
# and outlook periods are labelled. These are historical demonstrations, never evidence.
BRIEF_EXAMPLES = (
    {
        "ticker": "FICO",
        "review": "Owner selected November 11, 2026, round 3",
        "brief": (
            "Revenue $657M (+27% YoY); non-GAAP EPS $11.10 (+43%).\n"
            "Segments: Scores $413.8M (+33%); Software $243.2M (+19%).\n"
            "Free cash flow $284.3M (+35%). No FY2027 guidance provided.\n"
            "Mortgage pricing: moving to bespoke pricing; FICO Preferred Program "
            "announced, programs cover CY2027."
        ),
    },
    {
        "ticker": "UBER",
        "review": "Owner selected November 11, 2026, round 6 C",
        "brief": (
            "Revenue $14.77B (+10% YoY); Gross Bookings $59.82B (+20%).\n"
            "Non-GAAP operating income $2.3B (+38%); non-GAAP EPS $0.88 (+35%).\n"
            "Segment Gross Bookings: Mobility $29.51B (+18%); Delivery $28.54B "
            "(+22%); Freight $1.77B (+35%).\n"
            "Q4 Gross Bookings guide $63.25 to $65.25B (+18% to +22% YoY constant currency); "
            "non-GAAP EPS guide $0.95 to $0.99 (+34% to +39%).\n"
            "Autonomous vehicles: about 700 fully driverless by year-end; over "
            "2,000 in 2027."
        ),
    },
)

EXAMPLE_RULE = """
The advisory context includes owner-reviewed historical brief examples. Learn their
selection, compact grouping and period/basis labels; do not copy their facts or metric
checklists. They cannot support a claim in this release. Use only source_document for
evidence. Related figures may render together, but remain separate typed facts/values.
"""
SELECTION_CHECK = """
Before returning, briefly check selection internally: did you retain the current
release's material headline profit measure, operating driver, relevant segments and
guided measures with comparisons? Did any important new development outrank the usual
profile? Remove routine repetition. Check each selected value's exact metric, period,
basis, unit and reference. Do not add commentary or a checklist to the JSON response.
"""


def review_variant(
    request: dict[str, Any], *, examples: bool = False, selection_check: bool = False
) -> dict[str, Any]:
    variant = copy.deepcopy(request)
    if examples:
        data = json.loads(variant["messages"][0]["content"])
        data.setdefault("issuer_context", {})["owner_reviewed_brief_examples"] = list(
            BRIEF_EXAMPLES
        )
        variant["messages"][0]["content"] = json.dumps(
            data, ensure_ascii=False, separators=(",", ":")
        )
        variant["system"] += EXAMPLE_RULE
    if selection_check:
        variant["system"] += SELECTION_CHECK
    return variant


def headline_repair_request(
    request: dict[str, Any], answer: str, report: BindingReport
) -> dict[str, Any] | None:
    """One repair may address code-rejected headline values. No second repair, voting,
    silent fallback or automatic publication is provided by this request preparer."""
    try:
        raw = json.loads(answer)
        facts = raw["facts"]
    except (ValueError, KeyError, TypeError):
        return None
    headlines = (
        {
            fact.get("metric")
            for fact in facts
            if isinstance(fact, dict) and fact.get("role") in {"headline", "guidance"}
        }
        if isinstance(facts, list)
        else set()
    )
    rejected = [
        {"metric": issue.metric, "field": issue.field, "reasons": issue.reasons}
        for issue in report.rejected
        if issue.metric in headlines
    ]
    if not rejected:
        return None
    repair = copy.deepcopy(request)
    repair["messages"].extend(
        [
            {"role": "assistant", "content": answer},
            {
                "role": "user",
                "content": (
                    "Code could not bind these headline/guidance values: "
                    + json.dumps(rejected, ensure_ascii=False)
                    + ". Return a complete replacement candidate using the same schema. "
                    "Repair the references or metadata only when source_document supports "
                    "the claim. Otherwise omit that value; do not weaken the claim's basis "
                    "or change its metric to obtain acceptance. Keep other material facts "
                    "and new developments. This is the only repair attempt."
                ),
            },
        ]
    )
    return repair
