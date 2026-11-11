import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from earnings_bot.summary_evaluation import common_cases, coverage, metric_match, replay_row


@pytest.mark.parametrize(
    ("alias", "metric"),
    [
        ("Adjusted EBITDA", "Adjusted EBITDA margin"),
        ("Adjusted EBITDA margin", "Adjusted EBITDA"),
        ("EPS", "Adjusted EPS"),
        ("Adjusted EPS", "EPS"),
        ("revenue", "Cloud revenue"),
        ("RPO", "cRPO"),
        ("", "Revenue"),
    ],
)
def test_coverage_does_not_reward_a_different_measure(alias: str, metric: str) -> None:
    assert not metric_match(alias, metric)


def test_total_revenue_alias_and_explicit_growth_engine_can_match() -> None:
    assert metric_match("revenue", "Q4 revenue outlook")
    assert metric_match("RPO", "Total RPO")
    assert metric_match("Cloud revenue", "Cloud revenue growth")


@pytest.mark.parametrize(
    ("alias", "metric"),
    [
        ("revenue", "Revenue, Q3 2026"),
        ("revenue", "Revenue guidance, Q4 2026"),
        ("revenue", "Total revenue (Q3 2026)"),
        ("revenue", "FY 2026 revenue guidance"),
        ("adjusted operating earnings", "Adjusted (non-GAAP) Operating Earnings per share"),
        ("non-GAAP EPS", "Adjusted EPS"),
        ("Adjusted (non-GAAP) EPS", "Adjusted EPS"),
        ("adjusted EBITDA", "Adj. EBITDA"),
    ],
)
def test_equivalent_measure_labels_are_not_marked_missing(alias: str, metric: str) -> None:
    assert metric_match(alias, metric)


@pytest.mark.parametrize(
    ("alias", "metric"),
    [
        ("adjusted EBITDA", "Adj. EBITDA margin"),
        ("EPS", "Non-GAAP EPS"),
        ("revenue", "Cloud revenue (Q3 2026)"),
        ("revenue", "Revenue less transaction costs (Q3 2026)"),
    ],
)
def test_label_normalization_preserves_meaningful_differences(alias: str, metric: str) -> None:
    assert not metric_match(alias, metric)


def test_actuals_do_not_cover_guidance_and_one_line_cannot_fill_two_items() -> None:
    gold = [["Outlook", ["revenue"], "guide"], ["Sales", ["revenue"], "actual"]]
    result = coverage([{"metric": "Revenue", "kind": "actual"}], gold)
    assert result.covered == ("Sales",)
    assert result.missing == ("Outlook",)


@pytest.mark.parametrize(("actual", "expected"), [("actual", "guide"), ("guide", "actual")])
def test_an_outlook_is_never_coverage_of_a_reported_result(actual: str, expected: str) -> None:
    result = coverage([{"metric": "Revenue", "kind": actual}], [["Revenue", ["revenue"], expected]])
    assert not result.covered


def test_a_segment_total_is_not_company_revenue() -> None:
    result = coverage(
        [{"metric": "Revenue", "kind": "segment"}], [["Revenue", ["revenue"], "actual"]]
    )
    assert not result.covered


def test_replay_exposes_evidence_and_period_issues_without_calling_them_accuracy() -> None:
    row = {
        "ticker": "NEW",
        "lines": [
            {"metric": "Revenue outlook", "kind": "guide", "value": "$100M", "source": ""},
            {"metric": "Travel agent", "kind": "update", "value": "", "source": "Launched"},
        ],
    }
    gold = {"must": [["Revenue outlook", ["revenue"], "guide"]]}
    result = replay_row(row, gold, "Launched")
    assert result["editorial_coverage"] == 1
    assert {i["issue"] for i in result["issues"]} == {
        "missing_source_passage",
        "missing_guidance_period",
        "bare_update",
    }
    assert "accuracy" not in result


def test_only_shared_issuer_run_pairs_are_compared() -> None:
    results = {"A": [{"ticker": "NEW"}, {"ticker": "OLD"}], "B": [{"ticker": "NEW"}]}
    assert common_cases(results, ["A", "B"]) == {("NEW", 1)}


def test_offline_cli_uses_default_pair_and_code_baseline_and_writes_blind_key(
    tmp_path: Path,
) -> None:
    row = {
        "ticker": "NEW",
        "lines": [{"metric": "Revenue", "value": "$100M", "source": "Revenue was $100M."}],
    }
    # Including this fake Haiku output must not silently add it to the comparison.
    results = {name: [row] for name in ["Sonnet low", "Muse Ember low", "Haiku low", "Code only"]}
    files = {
        "results.json": results,
        "gold.json": {"NEW": {"must": [["Revenue", ["revenue"], "actual"]]}},
        "NEW.json": {"release": "Revenue was $100M."},
    }
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    out, blind = tmp_path / "report.json", tmp_path / "blind.md"
    command = [
        sys.executable,
        "scripts/summary_eval.py",
        "--results",
        str(tmp_path / "results.json"),
        "--gold",
        str(tmp_path / "gold.json"),
        "--docs",
        str(tmp_path),
        "--out",
        str(out),
        "--blind",
        str(blind),
    ]
    subprocess.run(command, check=True, capture_output=True, env={**os.environ, "PYTHONUTF8": "1"})
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["evaluator_version"]
    assert set(report["settings"]) == {"Sonnet low", "Muse Ember low", "Code only"}
    assert len(report["documents"]["NEW"]) == 64
    assert "Sonnet" not in blind.read_text(encoding="utf-8")
    key = json.loads(blind.with_suffix(".key.json").read_text(encoding="utf-8"))
    assert set(key["NEW/1"].values()) == set(report["settings"])


def test_future_requests_share_policy_schema_and_default_pair(tmp_path: Path) -> None:
    document = tmp_path / "NEW.json"
    document.write_text(
        json.dumps({"release": "Unseen issuer revenue was $123M."}), encoding="utf-8"
    )
    out = tmp_path / "requests.json"
    command = [
        sys.executable,
        "scripts/summary_prepare.py",
        "NEW",
        "--docs",
        str(tmp_path),
        "--out",
        str(out),
    ]
    subprocess.run(command, check=True, capture_output=True)
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [r["setting"] for r in plan["requests"]] == ["Sonnet low", "Muse Ember low"]
    first, second = (r["request"] for r in plan["requests"])
    assert first["system"] == second["system"]
    assert "EPS is optional" in first["system"]
    assert first["output_config"] == second["output_config"]
    assert (
        "period"
        in first["output_config"]["format"]["schema"]["properties"]["lines"]["items"]["required"]
    )
    assert "Unseen issuer revenue" in first["messages"][0]["content"]
    subprocess.run([*command, "--include-haiku"], check=True, capture_output=True)
    assert len(json.loads(out.read_text(encoding="utf-8"))["requests"]) == 3


def test_future_requests_never_silently_truncate_a_release(tmp_path: Path) -> None:
    (tmp_path / "LONG.json").write_text(json.dumps({"release": "x" * 200_001}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/summary_prepare.py",
            "LONG",
            "--docs",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no truncation" in result.stderr
