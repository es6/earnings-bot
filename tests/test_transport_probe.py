import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from first_print import transport_probe as probe


def plan(budget: int = 4) -> probe.Plan:
    return probe.Plan.model_validate(
        {
            "off_window_confirmed": True,
            "budget": budget,
            "cases": [
                {
                    "name": "issuer",
                    "url": "https://example.com/results",
                    "patterns": ["Issuer", "Q3 2026", "results"],
                }
            ],
        }
    )


def test_profile_comparison_honors_durable_reservations_and_refusal_cooldown(
    tmp_path: Path,
) -> None:
    clock = [1000.0]
    calls = []

    def transfer(case, profile):
        calls.append((clock[0], profile))
        return probe.Result(403, {}, b"no")

    def sleep(seconds):
        clock[0] += seconds

    ledger = probe.run(plan(), tmp_path, transfer, now=lambda: clock[0], sleep=sleep)
    assert [profile for _, profile in calls] == [None, "chrome", None, "chrome"]
    assert [calls[i + 1][0] - calls[i][0] for i in range(3)] == [30, 60, 120]
    assert len(ledger["attempts"]) == 4
    probe.run(plan(), tmp_path, transfer, now=lambda: clock[0], sleep=sleep)
    assert len(calls) == 4  # restarting cannot clear the aggregate allowance


@pytest.mark.parametrize("status", [429, 503])
def test_throttle_or_outage_stops_the_host_before_another_profile(
    tmp_path: Path, status: int
) -> None:
    calls = []

    def transfer(case, profile):
        calls.append(profile)
        return probe.Result(status, {"retry-after": "120", "Set-Cookie": "secret"}, b"wait")

    ledger = probe.run(plan(), tmp_path, transfer, sleep=lambda _: None)
    assert calls == [None]
    assert ledger["stopped"] == ["example.com"]
    assert "secret" not in json.dumps(ledger)


def test_transfer_error_reservation_survives_and_does_not_log_its_message(tmp_path: Path) -> None:
    def broken(case, profile):
        raise OSError("private credentials must not be logged")

    ledger = probe.run(plan(), tmp_path, broken, sleep=lambda _: None)
    assert len(ledger["attempts"]) == 1
    assert ledger["attempts"][0]["error"] == "OSError"
    assert "credentials" not in json.dumps(ledger)
    with pytest.raises(ValueError, match="different plan"):
        probe.run(plan(3), tmp_path, broken)


def test_dns_mixture_is_rejected_before_a_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    probe.public_ip.cache_clear()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443)), (0, 0, 0, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="nonpublic"):
        probe.public_ip("example.com")
    probe.public_ip.cache_clear()


def test_plan_rejects_secrets_and_requires_an_off_window_check() -> None:
    data = plan().model_dump()
    data["cases"][0]["url"] += "?access_token=secret"
    with pytest.raises(ValidationError):
        probe.Plan.model_validate(data)
    data = plan().model_dump()
    data["off_window_confirmed"] = False
    with pytest.raises(ValidationError):
        probe.Plan.model_validate(data)
