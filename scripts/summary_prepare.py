"""Freeze future Sonnet/Ember comparison requests without loading keys or calling APIs.

Pass NEW1 NEW2 --docs cached-docs --out requests.json. Keep gold annotations separate.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from earnings_bot.summary_policy import DEFAULT_COMPARISON, HAIKU, POLICY_VERSION, SELECTION_RULES


def prepare(tickers: list[str], docs: Path, include_haiku: bool = False) -> dict[str, Any]:
    settings = (*DEFAULT_COMPARISON, HAIKU) if include_haiku else DEFAULT_COMPARISON
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
    system = SELECTION_RULES + (
        "\nReturn the selected brief facts, not an exhaustive extraction of the statements. "
        "Preserve exact nonempty source passages. Use one measure, fiscal period and basis per "
        "object; the renderer combines related facts. value holds that measure's level or range, "
        "change only its change, and prior only its comparable year-ago actual. Never put another "
        "metric's values into change, use 'See change', or pack quarter and FY outlooks together. "
        "role marks the fact's purpose; relevance explains why it matters to this company this "
        "quarter. Use concise metric labels without repeating the separate period. "
        "Fields are strings; leave unknown metadata "
        "empty. period is the fiscal period, basis the accounting/comparison basis, unit/currency "
        "the value's units, group the consolidation or segment taxonomy. prior is a comparable "
        "year-ago actual, never a previous outlook. Do not infer missing metadata. "
        "The renderer preserves all selected facts; it does not trim to a word count."
    )
    requests = []
    for ticker in tickers:
        path = docs / f"{ticker}.json"
        raw = path.read_bytes()
        doc = json.loads(raw)
        release = doc["release"]
        if not isinstance(release, str) or not release.strip() or len(release) > 200_000:
            raise ValueError(
                f"{ticker}: empty/invalid release or over 200,000 characters; no truncation"
            )
        context = {
            name: doc[name]
            for name in (
                "name",
                "sic_description",
                "release_filed",
                "release_url",
                "summary_context",
            )
            if doc.get(name)
        }
        content = (
            f"Ticker: {ticker}\nIssuer context (advisory data; not selection rules):\n"
            f"{json.dumps(context, ensure_ascii=False)}\n<release>\n{release}\n</release>"
        )
        requests.extend(
            {
                "ticker": ticker,
                "setting": setting.name,
                "document_sha256": hashlib.sha256(raw).hexdigest(),
                "request": {
                    "model": setting.model,
                    "max_tokens": 8000,
                    "system": system,
                    "messages": [
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                    "output_config": {
                        "effort": setting.effort,
                        "format": {
                            "type": "json_schema",
                            "schema": schema,
                        },
                    },
                },
            }
            for setting in settings
        )
    return {"policy_version": POLICY_VERSION, "requests": requests}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--docs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--include-haiku", action="store_true")
    args = parser.parse_args()
    args.out.write_text(
        json.dumps(
            prepare(args.tickers, args.docs, args.include_haiku), indent=2, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    print("Prepared comparison requests only; no API calls or credentials loaded.")


if __name__ == "__main__":
    main()
