"""Freeze Ember comparison requests without loading keys or calling APIs."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from earnings_bot.source_document import document_from_html, document_from_text
from earnings_bot.summary_policy import DEFAULT_COMPARISON, EMBER, HAIKU, POLICY_VERSION
from earnings_bot.summary_request import referenced_request, structured_request


def prepare(
    tickers: list[str], docs: Path, include_haiku: bool = False, references: bool = False
) -> dict[str, Any]:
    if references and include_haiku:
        raise ValueError("Reference-schema preparation is Ember only")
    settings = (
        (EMBER,)
        if references
        else (*DEFAULT_COMPARISON, HAIKU)
        if include_haiku
        else DEFAULT_COMPARISON
    )
    requests = []
    for ticker in tickers:
        raw = (docs / f"{ticker}.json").read_bytes()
        doc = json.loads(raw)
        document = None
        if references:
            html = docs / f"{ticker}.html"
            document = (
                document_from_html(html.read_text(encoding="utf-8"))
                if html.exists()
                else document_from_text(doc["release"])
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
        for setting in settings:
            request = (
                referenced_request(ticker, document, context)
                if document is not None
                else structured_request(ticker, doc["release"], context)
            )
            request["model"] = setting.model
            request["output_config"]["effort"] = setting.effort
            requests.append(
                {
                    "ticker": ticker,
                    "setting": setting.name,
                    "document_sha256": document.sha256
                    if document
                    else hashlib.sha256(raw).hexdigest(),
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
    parser.add_argument(
        "--references", action="store_true", help="Prepare unverified item 6 schema"
    )
    args = parser.parse_args()
    args.out.write_text(
        json.dumps(
            prepare(args.tickers, args.docs, args.include_haiku, args.references),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("Prepared requests only; no API calls or credentials loaded.")


if __name__ == "__main__":
    main()
