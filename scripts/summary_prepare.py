"""Freeze Ember comparison requests without loading keys or calling APIs."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from earnings_bot.summary_policy import DEFAULT_COMPARISON, HAIKU, POLICY_VERSION
from earnings_bot.summary_request import structured_request


def prepare(tickers: list[str], docs: Path, include_haiku: bool = False) -> dict[str, Any]:
    settings = (*DEFAULT_COMPARISON, HAIKU) if include_haiku else DEFAULT_COMPARISON
    requests = []
    for ticker in tickers:
        raw = (docs / f"{ticker}.json").read_bytes()
        doc = json.loads(raw)
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
        for setting in settings:
            request = structured_request(ticker, doc["release"], context)
            request["model"] = setting.model
            request["output_config"]["effort"] = setting.effort
            requests.append(
                {
                    "ticker": ticker,
                    "setting": setting.name,
                    "document_sha256": hashlib.sha256(raw).hexdigest(),
                    "request": request,
                }
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
    print("Prepared requests only; no API calls or credentials loaded.")


if __name__ == "__main__":
    main()
