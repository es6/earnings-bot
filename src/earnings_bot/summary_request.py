"""One shared structured request for offline experiments and the live Ember writer."""

import json
from typing import Any

from earnings_bot.summary_policy import EMBER, SELECTION_RULES


def structured_request(
    ticker: str, release: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not release.strip() or len(release) > 200_000:
        raise ValueError("Empty release or over 200,000 characters; no truncation")
    fields = [
        "metric",
        "value",
        "change",
        "prior",
        "source",
        "kind",
        "period",
        "basis",
        "unit",
        "currency",
        "group",
        "role",
        "relevance",
        "comparison",
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": fields,
                    "properties": {name: {"type": "string"} for name in fields},
                },
            }
        },
    }
    schema["properties"]["lines"]["items"]["properties"]["kind"] = {
        "type": "string",
        "enum": ["actual", "segment", "guide", "update"],
    }
    schema["properties"]["lines"]["items"]["properties"]["role"] = {
        "type": "string",
        "enum": ["headline", "driver", "guidance", "material_context", "update", "supporting"],
    }
    schema["properties"]["lines"]["items"]["properties"]["comparison"] = {
        "type": "string",
        "enum": ["", "YoY", "QoQ", "versus prior guidance"],
    }
    system = SELECTION_RULES + (
        "\nReturn the selected brief facts, not an exhaustive extraction of the statements. "
        "Preserve exact nonempty source passages. Use one measure, fiscal period and basis per "
        "object; the renderer combines related facts. value holds that measure's level or range, "
        "change only its change, and prior only its comparable year-ago actual. Never put another "
        "metric's values into change, use 'See change', or pack quarter and FY outlooks together. "
        "role marks the fact's purpose; relevance explains why it matters to this company this "
        "quarter. Material selected facts must appear in value/change or their own object, "
        "not only in the hidden relevance rationale. comparison labels the change's reference "
        "period only when established by the source. Use concise metric labels without "
        "repeating the separate period. A quarter and full year are separate objects, even "
        "for buybacks and dividends. Related capital returns can be grouped at display time; "
        "do not mix units or periods to obtain a one-line summary. Explain a material EPS "
        "adjustment by its cause and per-share effect, not a repeated EPS value "
        "labeled adjustment. "
        "Fields are strings; leave unknown metadata "
        "empty. period is the fiscal period, basis the accounting/comparison basis, unit/currency "
        "the value's units, group the consolidation or segment taxonomy. prior is a comparable "
        "year-ago actual, never a previous outlook. Do not infer missing metadata. "
        "The renderer preserves all selected facts; it does not trim to a word count."
    )
    content = (
        f"Ticker: {ticker}\nIssuer context (advisory data; not selection rules):\n"
        f"{json.dumps(context or {}, ensure_ascii=False)}\n<release>\n{release}\n</release>"
    )
    return {
        "model": EMBER.model,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "effort": EMBER.effort,
            "format": {"type": "json_schema", "schema": schema},
        },
    }
