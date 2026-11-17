"""Bind the saved Ember-low reference answers again, offline and without model calls.

Three sets, each a saved run folder under --data: the six development answers of Nov 13
(the binder was tuned on them), sixteen held-out releases never used for tuning, and 60
answers for four issuers the binder had never seen. Prints how many selected values each
set keeps and writes every kept value with the passage that bound it, so a binder change
can be compared value by value: read each newly kept value against its passage before
counting it as right.

    uv run python scripts/binding_replay.py --data <folder of saved runs> --out kept.json
"""

import argparse
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from first_print.source_document import SourceDocument, SourceTable
from first_print.summary_binding import bind_answer
from first_print.summary_evidence import VALUE_FIELDS

DEVELOPMENT = "development-evaluation-2026-11-13"
HELD_OUT = "claude-continuation-evidence/item6-original/heldout/raw.json"
UNSEEN = "unseen-profile-evaluation-2026-11-13"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def answers(data: Path) -> Iterator[tuple[str, str, str, str, str]]:
    """(set, answer id, ticker, raw answer, document snapshot JSON) for each saved answer."""
    entries = {row["id"]: row for row in _load(data / DEVELOPMENT / "requests.json")["entries"]}
    for row in _load(data / DEVELOPMENT / "raw-results.json")["calls"]:
        if row["status"] == "complete":
            snapshot = entries[row["id"]]["snapshot"]
            yield "development", row["id"], row["ticker"], row["raw"], snapshot
    for row in _load(data / HELD_OUT)["calls"]:
        yield "held_out", row["ticker"], row["ticker"], row["raw"], row["snapshot"]
    for row in _load(data / UNSEEN / "raw-results.json")["calls"]:
        if row["variant"] != "build_profile" and row["status"] == "complete":
            document = data / UNSEEN / "docs" / f"{row['ticker']}-current.document.json"
            yield "unseen", row["id"], row["ticker"], row["raw"], document.read_text("utf-8")


def passage(document: SourceDocument, block_id: str, cell_ids: tuple[str, ...]) -> str:
    block = document.resolve(document.sha256, block_id)
    if isinstance(block, SourceTable) and cell_ids:
        cell = next(c for c in block.cells if c.id == cell_ids[0])
        return " | ".join(c.text for c in block.cells if c.row == cell.row)[:220]
    return " ".join(block.text.split())[:260]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--data", type=Path, required=True, help="folder of saved runs")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    selected: Counter[str] = Counter()
    kept: list[dict[str, object]] = []
    for group, ident, ticker, raw, snapshot in answers(args.data):
        try:
            facts = json.loads(raw)["facts"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        document = SourceDocument.from_json(snapshot)
        selected[group] += sum(
            sum(bool(f.get(field)) for field in VALUE_FIELDS) for f in facts if isinstance(f, dict)
        )
        for entry in bind_answer(raw, document).facts:
            fact = entry.fact
            for field, value in zip(VALUE_FIELDS, fact.values(), strict=True):
                if value is None:
                    continue
                ref = value.evidence[0]
                kept.append(
                    {
                        "set": group,
                        "id": ident,
                        "ticker": ticker,
                        "metric": fact.metric,
                        "group": fact.group,
                        "field": field,
                        "amount": value.amount,
                        "upper": value.upper,
                        "unit": value.unit,
                        "scale": value.scale,
                        "period": value.period,
                        "qualifier": value.comparison_qualifier,
                        "passage": passage(document, ref.block_id, ref.cell_ids),
                    }
                )
    counts = Counter(str(row["set"]) for row in kept)
    summary = {group: f"{counts[group]}/{selected[group]}" for group in selected}
    args.out.write_text(
        json.dumps({"kept_of_selected": summary, "kept": kept}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("Values kept of those selected:", summary)


if __name__ == "__main__":
    main()
