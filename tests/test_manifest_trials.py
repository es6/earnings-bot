import copy
import json
import re
from pathlib import Path

from first_print.trial_matching import candidates

API = "https://www.jpmorganchase.com/services/json/v1/investor-relations/quarterly-earnings.json"
FIXTURE = Path(__file__).parent / "fixtures/issuer-history/jpm-quarterly.json"
PATTERNS = [re.compile(p, re.IGNORECASE) for p in ("jpmorganchase", r"Q3\s+2026", "Press Release")]


def test_trial_manifest_identity_is_the_exact_press_release_document() -> None:
    [entry] = candidates(FIXTURE.read_bytes(), API, PATTERNS)
    assert entry["id"].endswith("/2026/3rd-quarter/6c1f8114-1be4-4174-8292-f75bbb59bc21.pdf")
    old_patterns = [
        re.compile(p, re.IGNORECASE) for p in ("jpmorganchase", r"Q3\s+2025", "Press Release")
    ]
    assert candidates(FIXTURE.read_bytes(), API, old_patterns) == []


def test_trial_manifest_rejects_a_presentation_role_or_conflicting_quarter_documents() -> None:
    data = json.loads(FIXTURE.read_bytes())
    data["items"][0]["docs"]["pressRelease"]["title"] = "3Q26 Earnings Presentation"
    assert candidates(json.dumps(data).encode(), API, PATTERNS) == []
    data = json.loads(FIXTURE.read_bytes())
    other = copy.deepcopy(data["items"][0])
    other["docs"]["pressRelease"]["link"] = "/different-release.pdf"
    data["items"].append(other)
    assert candidates(json.dumps(data).encode(), API, PATTERNS) == []
