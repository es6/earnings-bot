import socket
import struct
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

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


def test_clock_ignores_the_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Frozen: this machine says 5000.0 s after the epoch at monotonic 1000.0
    monkeypatch.setattr(
        clock, "time", SimpleNamespace(time=lambda: 5000.0, monotonic=lambda: 1000.0)
    )
    trusted = TrustedClock()
    # Pretend the servers say real time is 10 seconds ahead of this machine
    trusted.synced = Sample("a", 5000.0 - 1000.0 + 10, 0.01)

    assert trusted.system_clock_behind() == 10
    assert trusted.now() == datetime.fromtimestamp(5010.0, UTC)

    trusted.forget()
    monkeypatch.undo()  # the real clocks again
    assert trusted.system_clock_behind() == pytest.approx(0, abs=1)


def test_ask_sends_one_sntp_request_and_reads_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[bytes] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        address = server.getsockname()

        def answer() -> None:
            request, client = server.recvfrom(100)
            requests.append(request)
            server.sendto(reply(1_800_000_000.05, 1_800_000_000.06), client)

        thread = threading.Thread(target=answer)
        thread.start()
        monkeypatch.setattr(clock.socket, "getaddrinfo", lambda *args: [(0, 0, 0, "", address)])

        sample = clock.ask("time.example", timeout=2)
        thread.join()

    assert requests == [b"#" + bytes(47)]  # SNTP version 4, client mode, 48 bytes
    assert sample is not None and sample.server == "time.example"
    assert sample.round_trip < 2  # the 10 ms the server held it comes off the wire time


def test_ask_gives_nothing_when_the_server_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        address = silent.getsockname()
        monkeypatch.setattr(clock.socket, "getaddrinfo", lambda *args: [(0, 0, 0, "", address)])

        assert clock.ask("time.example", timeout=0.05) is None
    monkeypatch.setattr(clock.socket, "getaddrinfo", lambda *args: (_ for _ in ()).throw(OSError))
    assert clock.ask("no-such-host.example") is None
