"""Replay saved JSON without API calls; optionally prepare a blind comparison.

Pass --results saved.json --gold gold.json --docs docs --out replay.json.
"""

import argparse
import hashlib
import json
import random
from pathlib import Path

from earnings_bot.summary_evaluation import (
    EVALUATOR_VERSION,
    common_cases,
    display_lines,
    replay_row,
)
from earnings_bot.summary_policy import DEFAULT_COMPARISON, POLICY_VERSION, SELECTION_RULES


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--docs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--include-haiku", action="store_true")
    parser.add_argument(
        "--blind", type=Path, help="Write anonymous briefs, with a separate key file"
    )
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    models = [s.name for s in DEFAULT_COMPARISON]
    if args.include_haiku:
        models.append("Haiku low")
    models.append("Code only")
    missing = set(models) - results.keys()
    if missing:
        parser.error(f"Saved results missing requested settings: {sorted(missing)}")
    cases = common_cases(results, models)
    if not cases:
        parser.error("No shared issuer/run cases to compare")
    report = {
        "evaluator_version": EVALUATOR_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_sha256": hashlib.sha256(SELECTION_RULES.encode()).hexdigest(),
        "note": (
            "Historical cached outputs, not generated with this policy. Coverage is not accuracy."
        ),
        "results_sha256": digest(args.results),
        "gold_sha256": digest(args.gold),
        "documents": {},
        "settings": {},
        "excluded": {},
    }
    rng = random.Random(args.seed)
    briefs, key = [], {}
    for ticker, run in sorted(cases):
        path = args.docs / f"{ticker}.json"
        release = json.loads(path.read_text(encoding="utf-8"))["release"]
        report["documents"][ticker] = digest(path)
        shuffled = models.copy()
        rng.shuffle(shuffled)
        mapping = {}
        briefs.append(f"## {ticker} / run {run}")
        for letter, model in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", shuffled, strict=False):
            rows = [r for r in results[model] if (r["ticker"], r.get("run", 1)) == (ticker, run)]
            if len(rows) != 1:
                parser.error(f"Duplicate cached result: {model} {ticker} run {run}")
            row = rows[0]
            report["settings"].setdefault(model, []).append(replay_row(row, gold[ticker], release))
            mapping[letter] = model
            briefs.append(f"\n{letter}\n```\n{display_lines(ticker, row['lines'])}\n```\n")
        key[f"{ticker}/{run}"] = mapping
    for model in models:
        report["excluded"][model] = sorted(
            {(r["ticker"], r.get("run", 1)) for r in results[model]} - cases
        )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.blind:
        args.blind.write_text("\n".join(briefs), encoding="utf-8")
        args.blind.with_suffix(".key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"Replayed {len(cases)} shared cases x {len(models)} settings; no API calls.")


if __name__ == "__main__":
    main()
