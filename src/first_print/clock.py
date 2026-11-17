"""A clock that takes its time from NTP servers instead of this machine's system clock.

Each sync asks a few public time servers for the time and keeps the answer that came back
fastest. Between syncs the clock counts forward on time.monotonic(), which nothing can set, so
changes to the system clock (Windows time sync, someone fixing the time by hand) don't move it.
"""

import socket
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime

# Cloudflare and Google answer from nearby data centers. NIST keeps official US time.
TIME_SERVERS = ("time.cloudflare.com", "time.google.com", "time.nist.gov")

_NTP_TO_UNIX = 2_208_988_800  # seconds from 1900, where NTP counts from, to 1970


@dataclass(frozen=True)
class Sample:
    server: str
    offset: float  # real time minus time.monotonic(), in seconds
    round_trip: float  # network delay; the offset is off by at most half of it


def parse_reply(server: str, reply: bytes, sent: float, received: float) -> Sample | None:
    """Reads an SNTP reply. `sent` and `received` are time.monotonic() around the exchange."""
    if len(reply) < 48:
        return None
    leap, mode, stratum = reply[0] >> 6, reply[0] & 0b111, reply[1]
    # Leap indicator 3 means the server lost its own time source. Stratum 0 is a refusal.
    if leap == 3 or mode != 4 or stratum == 0:
        return None
    # When the server got our request and sent its reply, as 32.32 fixed point
    server_received, server_sent = (
        seconds - _NTP_TO_UNIX + fraction / 2**32
        for seconds, fraction in struct.iter_unpack("!II", reply[32:48])
    )
    return Sample(
        server,
        offset=((server_received - sent) + (server_sent - received)) / 2,
        round_trip=(received - sent) - (server_sent - server_received),
    )


def ask(server: str, timeout: float = 2.0) -> Sample | None:
    try:
        # Look up the address first, so DNS time doesn't count as network delay
        address = socket.getaddrinfo(server, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sent = time.monotonic()
            sock.sendto(b"\x23" + bytes(47), address)  # SNTP version 4, client mode
            reply = sock.recv(48)
            received = time.monotonic()
    except OSError:
        return None
    return parse_reply(server, reply, sent, received)


class TrustedClock:
    def __init__(self, servers: tuple[str, ...] = TIME_SERVERS) -> None:
        self.servers = servers
        self.synced: Sample | None = None  # the answer in use; None means the system clock
        self.synced_at = 0.0  # time.time() of the last sync

    def sync(self) -> list[Sample]:
        """Asks each server once, blocking up to 2 seconds per server, and returns the answers
        fastest first. When no server answers, the previous sync stays in use."""
        samples = sorted(
            (sample for server in self.servers if (sample := ask(server))),
            key=lambda sample: sample.round_trip,
        )
        if samples:
            self.synced, self.synced_at = samples[0], time.time()
        return samples

    def forget(self) -> None:
        """Go back to the system clock."""
        self.synced = None

    def now(self) -> datetime:
        if self.synced is None:
            return datetime.now(UTC)
        return datetime.fromtimestamp(time.monotonic() + self.synced.offset, UTC)

    def system_clock_behind(self) -> float:
        """Seconds the system clock is behind this one."""
        return self.now().timestamp() - time.time()
