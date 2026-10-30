import struct
import time
from datetime import UTC, datetime

import pytest

from earnings_bot import clock
from earnings_bot.clock import Sample, TrustedClock, parse_reply


def ntp(unix_seconds: float) -> bytes:
    seconds = int(unix_seconds)
    return struct.pack("!II", seconds + 2_208_988_800, int((unix_seconds - seconds) * 2**32))


def reply(received: float, sent: float, first_byte: int = 0x24, stratum: int = 2) -> bytes:
    # 0x24: no leap warning, version 4, server mode
    return bytes([first_byte, stratum]) + bytes(30) + ntp(received) + ntp(sent)


def test_parse_reply() -> None:
    # Sent at monotonic 100.0 and answered at 100.2. The server held it for 10 ms.
    sample = parse_reply("ntp", reply(1_800_000_000.05, 1_800_000_000.06), 100.0, 100.2)

    assert sample is not None
    assert sample.round_trip == pytest.approx(0.19, abs=1e-6)
    assert sample.offset == pytest.approx(1_800_000_000.055 - 100.1, abs=1e-6)


@pytest.mark.parametrize(
    "bad",
    [
        reply(1.0, 1.0, stratum=0),  # refusal
        reply(1.0, 1.0, first_byte=0xE4),  # leap indicator 3: server not synchronized
        reply(1.0, 1.0, first_byte=0x23),  # client mode, not a server reply
        b"\x24\x02",  # too short
    ],
)
def test_parse_reply_rejects_bad_answers(bad: bytes) -> None:
    assert parse_reply("ntp", bad, 100.0, 100.2) is None


def test_clock_uses_the_fastest_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"a": Sample("a", 5.0, 0.040), "b": Sample("b", 6.0, 0.010), "c": None}
    monkeypatch.setattr(clock, "ask", answers.get)
    trusted = TrustedClock(servers=("a", "b", "c"))

    samples = trusted.sync()

    assert [sample.server for sample in samples] == ["b", "a"]
    assert trusted.synced == answers["b"]


def test_clock_keeps_its_last_sync_when_no_server_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = TrustedClock(servers=("a",))
    trusted.synced = Sample("a", 5.0, 0.01)
    monkeypatch.setattr(clock, "ask", lambda server: None)

    assert trusted.sync() == []
    assert trusted.synced == Sample("a", 5.0, 0.01)


def test_clock_ignores_the_system_clock() -> None:
    trusted = TrustedClock()
    # Pretend the servers say real time is 10 seconds ahead of this machine
    trusted.synced = Sample("a", time.time() - time.monotonic() + 10, 0.01)

    assert trusted.system_clock_behind() == pytest.approx(10, abs=0.05)
    assert (trusted.now() - datetime.now(UTC)).total_seconds() == pytest.approx(10, abs=0.05)

    trusted.forget()
    assert trusted.system_clock_behind() == pytest.approx(0, abs=0.05)
