"""Replays 20 earnings releases landing at the same moment, entirely on this computer, to see what
SEC's request limit costs and whether anything else waits behind it.

    uv run python -m tests.burst_replay --sec per-host --out before.json
    uv run python -m tests.burst_replay --out after.json
    uv run python -m tests.burst_replay --no-sec --out no-sec.json
    uv run python -m tests.burst_replay --compare before.json after.json no-sec.json

`--sec per-host` paces SEC the way main did before one queue was shared by all of sec.gov:
www.sec.gov and data.sec.gov each at 8 requests a second. `--no-sec` drops both SEC polls, as a
control for the IR and newswire timings.

What's real: the recorder (every watch window, shared newswire polls), the Fetcher (its queues,
gaps and urgent turns), the alert pipeline (links, release text, attachments) and asyncio's
scheduling on this PC. What's simulated: every server. Each request gets its answer from local
fixtures after a made-up response time (LATENCY, times 0.8 to 1.6 at random), no CDN caches
anything, and Discord is a fake poster that answers at once. Every release becomes available
at one moment, R: the IR site's list and files, the newswire item, EDGAR's latest-filings feed
and the filing's documents. The submissions JSON lists the 8-K SUBMISSIONS_LAG seconds later,
as it did live. All times are seconds after R.

The companies come in three kinds, like the real schedule's:
- "ir": a Q4 IR site (press releases, and files that list two PDFs) and Business Wire
- "rss": the company's own feed (its release page has the text) and PR Newswire
- "sec": Business Wire only, whose articles block us, so the text comes from the 8-K's exhibit
Every company also has both SEC polls. Business Wire and PR Newswire are each one poll shared by
every company that uses them.
"""

import argparse
import asyncio
import contextlib
import io
import json
import math
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from earnings_bot import fetch
from earnings_bot.clock import Sample, TrustedClock
from earnings_bot.fetch import Response
from earnings_bot.pipeline import Poster, Releases
from earnings_bot.recorder import ET, Journal, Plan, Target, record
from earnings_bot.sources import PR_NEWSWIRE_EARNINGS, EdgarFilings, Source
from tests.fakes import FakeSession, SessionFetcher, sec_filing_detail

# Simulated response times in seconds, before the random factor
LATENCY = {
    "sec_submissions": 0.12,
    "sec_latest": 0.25,
    "sec_archive": 0.10,
    "q4_list": 0.15,
    "q4_bodies": 0.30,
    "q4_files": 0.15,
    "pdf": 0.25,
    "businesswire": 0.20,
    "prnewswire": 0.15,
    "prnewswire_article": 0.30,
    "ir_feed": 0.15,
    "ir_page": 0.20,
    "unknown": 0.05,
}
# Seconds the submissions JSON lists an 8-K after EDGAR's latest-filings feed does. Measured
# live: 0 to 56 s for six 8-Ks on Nov 3, 2026 (48 s for AMD's), 8 and 25 s on Nov 4, 19 and 24 s
# on Nov 5. Everything else is out at R.
SUBMISSIONS_LAG = 20.0
PDF_BYTES = 300_000
WORDS = [
    "Aster",
    "Birch",
    "Cedar",
    "Dahlia",
    "Elm",
    "Fern",
    "Gorse",
    "Hazel",
    "Iris",
    "Juniper",
    "Kelp",
    "Laurel",
    "Maple",
    "Nettle",
    "Olive",
    "Poplar",
    "Quince",
    "Rowan",
    "Sorrel",
    "Tansy",
]
# Repeated, so 20 companies are 10, 6 and 4 of each, and any 3 include one of each
KINDS = ("ir", "rss", "sec", "ir", "rss", "ir", "sec", "ir", "rss", "ir")
OLD_FILING = "0000000001-26-000001"


@dataclass(frozen=True)
class Company:
    index: int

    @property
    def word(self) -> str:
        round_ = self.index // len(WORDS)
        return WORDS[self.index % len(WORDS)] + (str(round_ + 1) if round_ else "")

    @property
    def ticker(self) -> str:
        return self.word[:4].upper() + "".join(c for c in self.word if c.isdigit())

    @property
    def name(self) -> str:
        return f"{self.word} Inc"

    @property
    def kind(self) -> str:
        return KINDS[self.index % len(KINDS)]

    @property
    def cik(self) -> int:
        return 1_900_000 + self.index

    @property
    def accession(self) -> str:
        return f"{self.cik:010d}-26-{self.index + 1:06d}"

    @property
    def folder(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}"
        )

    @property
    def q4_host(self) -> str:
        return f"investors.{self.word.lower()}.example"

    @property
    def ir_host(self) -> str:
        return f"ir.{self.word.lower()}.example"

    def pdf(self, quarter: int, title: str) -> str:
        # Three companies to each of the file CDN's hosts
        return (
            f"https://s{self.index % 3 + 1}.q4cdn.com/{100_000_000 + self.index}/files/"
            f"doc_financials/2026/q{quarter}/{self.ticker}-Q{quarter}-2026-{title}.pdf"
        )

    @property
    def prnewswire_article(self) -> str:
        return (
            f"https://www.prnewswire.com/news-releases/{self.word.lower()}-reports-third-quarter"
            f"-2026-results-30{2_600_000 + self.index}.html"
        )

    @property
    def businesswire_article(self) -> str:
        return f"https://www.businesswire.com/news/home/20261106{5000 + self.index}/en/"


class WithoutSec(Target):
    """A target whose SEC polls are left out, for the control run."""

    def sources(self) -> list[Source]:
        return [s for s in super().sources() if not isinstance(s, EdgarFilings)]


def plan_for(
    companies: Sequence[Company], start: datetime, released: datetime, end: datetime, sec: bool
) -> Plan:
    targets: list[Target] = []
    for c in companies:
        fields: dict[str, Any] = {
            "ticker": c.ticker,
            "expected": released,
            "cik": c.cik,
            "before_minutes": (released - start).total_seconds() / 60,
            "after_minutes": (end - released).total_seconds() / 60,
        }
        if c.kind == "ir":
            fields |= {"q4_host": c.q4_host, "newswire": [c.name]}
        elif c.kind == "rss":
            fields |= {"prnewswire": [c.name], "rss": [f"https://{c.ir_host}/rss/news.xml"]}
        else:
            fields |= {"newswire": [c.name]}
        targets.append((Target if sec else WithoutSec).model_validate(fields))
    return Plan(target=targets)


def release_html(c: Company) -> str:
    paragraphs = [
        (
            f"{c.name} today reported results for its third quarter of fiscal 2026. Revenue was "
            f"${1_000 + 37 * c.index:,} million, up {8 + c.index % 9} percent from a year earlier."
        )
    ] + [
        f"Paragraph {n}: management discussed demand, margins and the outlook for the fourth "
        "quarter, and repeated that these figures are made up for a local replay."
        for n in range(16)
    ]
    rows = "".join(
        f"<tr><td>{label}</td><td>{value:,}</td><td>{value * 9 // 10:,}</td></tr>"
        for label, value in (("Revenue", 1_000 + c.index), ("Net income", 200 + c.index))
    )
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><head><title>{c.name}</title></head><body>{body}<table>{rows}</table></body>"


@dataclass(frozen=True)
class Route:
    name: str  # which LATENCY applies
    kind: str  # sec_poll, sec_body, ir_poll, ir_body, pdf, wire_poll, wire_body or other
    owner: str | None  # the ticker it belongs to, unless it's a shared feed
    respond: Callable[[bool], tuple[int, bytes]]  # given whether the releases are out


class World:
    """Every simulated server. The releases all become available at `released_at`, a
    time.monotonic() value; each answer reflects the moment its request arrives, halfway
    through its response time."""

    def __init__(
        self, companies: Sequence[Company], released_at: float, released: datetime, seed: int
    ) -> None:
        self.companies = companies
        self.released_at = released_at
        self.released = released
        self.rng = random.Random(seed)
        self.by_cik = {c.cik: c for c in companies}
        self.by_host = {h: c for c in companies for h in (c.q4_host, c.ir_host)}
        self.by_pdf = {
            c.pdf(q, t): c for c in companies for q in (2, 3) for t in ("Release", "Slides")
        }
        self.by_article = {c.prnewswire_article: c for c in companies}

    async def answer(self, method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        route = self.route(url)
        delay = LATENCY[route.name] * self.rng.uniform(0.8, 1.6)
        await asyncio.sleep(delay / 2)
        lag = SUBMISSIONS_LAG if route.name == "sec_submissions" else 0.0
        status, body = route.respond(time.monotonic() >= self.released_at + lag)
        await asyncio.sleep(delay / 2)
        return status, {"content-type": "text/html" if status == 200 else "text/plain"}, body

    def route(self, url: str) -> Route:
        parts = urlsplit(url)
        host, path, query = parts.hostname or "", parts.path, parse_qs(parts.query)
        if host == "data.sec.gov":
            c = self.by_cik[int(path.removesuffix(".json").rsplit("CIK", 1)[1])]
            return Route(
                "sec_submissions", "sec_poll", c.ticker, lambda out: self.submissions(c, out)
            )
        if host == "www.sec.gov" and path == "/cgi-bin/browse-edgar":
            c = self.by_cik[int(query["CIK"][0])]
            return Route("sec_latest", "sec_poll", c.ticker, lambda out: self.latest(c, out))
        if host == "www.sec.gov" and path.startswith("/Archives/edgar/data/"):
            c = self.by_cik[int(path.split("/")[4])]
            name = path.rsplit("/", 1)[1]
            return Route(
                "sec_archive", "sec_body", c.ticker, lambda out: self.archive(c, name, out)
            )
        if host in self.by_host and host.startswith("investors."):
            c = self.by_host[host]
            if "PressRelease.svc" in path:
                bodies = query.get("bodyType") == ["1"]
                size = int(query["pageSize"][0])
                return Route(
                    "q4_bodies" if bodies else "q4_list",
                    "ir_body" if bodies else "ir_poll",
                    c.ticker,
                    lambda out: self.q4_releases(c, size, bodies, out),
                )
            return Route("q4_files", "ir_poll", c.ticker, lambda out: self.q4_files(c, out))
        if host.endswith(".q4cdn.com") and url in self.by_pdf:
            c = self.by_pdf[url]
            fresh = "/q3/" in url
            pdf = b"%PDF-1.7\n" + bytes(PDF_BYTES)
            return Route(
                "pdf", "pdf", c.ticker, lambda out: (200, pdf) if out or not fresh else (404, b"")
            )
        if host == "feed.businesswire.com":
            return Route("businesswire", "wire_poll", None, lambda out: self.wire(False, out))
        if host == "www.prnewswire.com" and path.startswith("/rss/"):
            return Route("prnewswire", "wire_poll", None, lambda out: self.wire(True, out))
        if host == "www.prnewswire.com" and url in self.by_article:
            c = self.by_article[url]
            return Route("prnewswire_article", "wire_body", c.ticker, lambda out: self.page(c, out))
        if host in self.by_host:
            c = self.by_host[host]
            if path.startswith("/rss/"):
                return Route("ir_feed", "ir_poll", c.ticker, lambda out: self.ir_feed(c, out))
            return Route("ir_page", "ir_body", c.ticker, lambda out: self.page(c, out))
        return Route("unknown", "other", None, lambda out: (404, b""))

    # The fixtures, each given whether the releases are out yet

    def submissions(self, c: Company, out: bool) -> tuple[int, bytes]:
        filings = [(OLD_FILING, "10-Q", "", "2026-08-05T20:05:00.000Z", "q2.htm")]
        if out:
            accepted = self.released.astimezone(UTC).isoformat(timespec="milliseconds")
            filings.insert(0, (c.accession, "8-K", "2.02,9.01", accepted, "form8-k.htm"))
        columns = ("accessionNumber", "form", "items", "acceptanceDateTime", "primaryDocument")
        recent = {name: [f[i] for f in filings] for i, name in enumerate(columns)}
        return 200, json.dumps({"cik": str(c.cik), "filings": {"recent": recent}}).encode()

    def latest(self, c: Company, out: bool) -> tuple[int, bytes]:
        def entry(accession: str, items: str, updated: datetime) -> str:
            folder = f"https://www.sec.gov/Archives/edgar/data/{c.cik}/{accession.replace('-', '')}"
            return (
                f"<entry><title>8-K - {c.name.upper()} ({c.cik:010d}) (Filer)</title>"
                f'<link rel="alternate" type="text/html" href="{folder}/{accession}-index.htm"/>'
                f'<summary type="html">&lt;b&gt;AccNo:&lt;/b&gt; {accession} {items}</summary>'
                f"<updated>{updated.astimezone(ET).isoformat(timespec='seconds')}</updated>"
                f"<id>urn:tag:sec.gov,2008:accession-number={accession}</id></entry>"
            )

        entries = [entry(OLD_FILING, "Item 5.07: Matters", self.released - timedelta(days=30))]
        if out:
            items = "Item 2.02: Results of Operations Item 9.01: Exhibits"
            entries.insert(0, entry(c.accession, items, self.released))
        feed = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest</title>'
        return 200, (feed + "".join(entries) + "</feed>").encode()

    def archive(self, c: Company, name: str, out: bool) -> tuple[int, bytes]:
        if not out:
            return 404, b""
        if name == "index.json":
            files = ["form8-k.htm", "ex99-1.htm", f"{c.accession}-index.htm"]
            items = [{"name": f, "size": "90000" if "99" in f else "9000"} for f in files]
            return 200, json.dumps({"directory": {"item": items}}).encode()
        if name == f"{c.accession}-index.htm":
            return 200, sec_filing_detail("form8-k.htm", "ex99-1.htm")
        if name == "ex99-1.htm":
            return 200, release_html(c).encode()
        return 200, b"<html><body>Form 8-K</body></html>"

    def q4_releases(self, c: Company, size: int, bodies: bool, out: bool) -> tuple[int, bytes]:
        releases: list[dict[str, Any]] = [
            {
                "PressReleaseId": 80_000 + c.index * 100 + n,
                "Headline": f"{c.name} to Host Third Quarter 2026 Conference Call"
                if n == 0
                else f"{c.name} Announces Product Update {n}",
                "LinkToDetailPage": f"/news/old-{n}",
                "PressReleaseDate": "10/01/2026 08:00:00",
            }
            for n in range(12)
        ]
        if out:
            release = {
                "PressReleaseId": 90_000 + c.index,
                "Headline": f"{c.name} Reports Third Quarter 2026 Results",
                "LinkToDetailPage": "/news/q3-2026-results",
                "PressReleaseDate": f"{self.released.astimezone(ET):%m/%d/%Y %H:%M:%S}",
            }
            releases.insert(0, release | ({"Body": release_html(c)} if bodies else {}))
        return 200, json.dumps({"GetPressReleaseListResult": releases[:size]}).encode()

    def q4_files(self, c: Company, out: bool) -> tuple[int, bytes]:
        def report(quarter: int) -> dict[str, Any]:
            documents = [
                {"DocumentPath": c.pdf(quarter, t), "DocumentTitle": t, "DocumentType": "File"}
                for t in ("Release", "Slides")
            ]
            return {"ReportTitle": f"Q{quarter} 2026 Earnings", "Documents": documents}

        reports = [report(3), report(2)] if out else [report(2)]
        return 200, json.dumps({"GetFinancialReportListResult": reports}).encode()

    def wire(self, prnewswire: bool, out: bool) -> tuple[int, bytes]:
        kinds = ("rss",) if prnewswire else ("ir", "sec")
        items = []
        if out:
            for c in self.companies:
                if c.kind in kinds:
                    link = c.prnewswire_article if prnewswire else c.businesswire_article
                    items.append((f"{c.name} Reports Third Quarter 2026 Results", link, c.name))
        items += [
            (f"Zenith Widgets {n} Reports Third Quarter 2026 Results", f"https://wire/{n}", "")
            for n in range(20)
        ]
        stamp = format_datetime(self.released)
        xml = "".join(
            f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
            f"<pubDate>{stamp}</pubDate><dc:contributor>{who}</dc:contributor>"
            f"<description>CITY--(WIRE)--{who or title}</description></item>"
            for title, link, who in items[: 20 + len(self.companies)]
        )
        channel_link = f"<link>{PR_NEWSWIRE_EARNINGS}</link>" if prnewswire else ""
        return 200, (
            '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            f"<title>Earnings</title>{channel_link}<lastBuildDate>{stamp}</lastBuildDate>"
            f"{xml}</channel></rss>"
        ).encode()

    def ir_feed(self, c: Company, out: bool) -> tuple[int, bytes]:
        items = [(f"{c.name} to Host Third Quarter 2026 Conference Call", "call")]
        if out:
            items.insert(0, (f"{c.name} Reports Third Quarter 2026 Results", "q3-2026-results"))
        xml = "".join(
            f"<item><title>{title}</title><link>https://{c.ir_host}/news/{slug}</link>"
            f"<pubDate>{format_datetime(self.released)}</pubDate></item>"
            for title, slug in items
        )
        return 200, f'<rss version="2.0"><channel><title>News</title>{xml}</channel></rss>'.encode()

    def page(self, c: Company, out: bool) -> tuple[int, bytes]:
        return (200, release_html(c).encode()) if out else (404, b"Not found")


@dataclass(frozen=True)
class Exchange:
    """One request the pipeline or recorder made, as the real Fetcher reported it."""

    url: str
    kind: str
    owner: str | None
    urgent: bool
    queued: float  # waiting for the host's (or SEC's) turn
    elapsed: float  # the simulated response time, plus scheduling
    done: float  # time.monotonic() when the answer was read
    status: int


class ReplayFetcher(SessionFetcher):
    def __init__(self, world: World) -> None:
        super().__init__(FakeSession(world.answer))
        self.world = world
        self.exchanges: list[Exchange] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
        encoded_url: bool = False,
    ) -> Response:
        response = await super().request(
            method,
            url,
            headers,
            give_up_after,
            urgent,
            allow_redirects=allow_redirects,
            encoded_url=encoded_url,
        )
        route = self.world.route(url)
        self.exchanges.append(
            Exchange(
                url,
                route.kind,
                route.owner,
                urgent,
                response.queued,
                response.elapsed,
                time.monotonic(),
                response.status,
            )
        )
        return response


class FakePoster(Poster):
    """Discord, answering at once with the time of each change."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.sends = 0
        self.files: list[str] = []

    async def send(self, content: str) -> datetime | None:
        self.sends += 1
        return self.journal.now()

    async def edit(self, content: str) -> datetime | None:
        return self.journal.now()

    async def attach(self, name: str, data: bytes) -> datetime | None:
        self.files.append(name)
        return self.journal.now()

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        return self.journal.now()


class LocalClock(TrustedClock):
    """This PC's clock, taken as synced without asking a time server."""

    def __init__(self) -> None:
        super().__init__(servers=())

    def sync(self) -> list[Sample]:
        sample = Sample("this PC (replay)", time.time() - time.monotonic(), 0.0)
        self.synced, self.synced_at = sample, time.time()
        return [sample]


class Usage(threading.Thread):
    """Samples this process's CPU use every half second."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stopping = threading.Event()
        self.samples: list[float] = []  # fraction of one core, per half second
        self.cpu_started, self.wall_started = time.process_time(), time.perf_counter()

    def run(self) -> None:
        cpu, wall = self.cpu_started, self.wall_started
        while not self.stopping.wait(0.5):
            now_cpu, now_wall = time.process_time(), time.perf_counter()
            self.samples.append((now_cpu - cpu) / (now_wall - wall))
            cpu, wall = now_cpu, now_wall

    def stop(self) -> dict[str, float]:
        self.stopping.set()
        self.join()
        cpu = time.process_time() - self.cpu_started
        wall = time.perf_counter() - self.wall_started
        return {
            "cpu_seconds": round(cpu, 2),
            "mean_cpu_percent": round(100 * cpu / wall, 1),
            "peak_cpu_percent": round(100 * max(self.samples, default=0.0), 1),
        }


def memory_mb() -> tuple[float, float] | None:
    """This process's working set now and at its peak, in MB, where the OS says."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                *((name, ctypes.c_size_t) for name in "abcdef"),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = kernel32.K32GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB on Linux
    return peak, peak


async def loop_lag(samples: list[float]) -> None:
    """How late asyncio wakes a 50 ms sleep: a busy event loop delays everything."""
    while True:
        asked = time.monotonic()
        await asyncio.sleep(0.05)
        samples.append(time.monotonic() - asked - 0.05)


@dataclass
class Settings:
    tickers: int = 20
    warmup: float = 8.0  # seconds from the windows opening to R, for every first poll to finish
    tail: float = 30.0  # seconds the windows stay open after R
    seed: int = 1
    sec: str = "shared"  # "per-host": how main paced SEC before the fix
    with_sec: bool = True
    log_dir: Path | None = None


@dataclass
class Run:
    settings: Settings
    companies: list[Company]
    released: datetime
    released_at: float
    events: list[dict[str, Any]]
    exchanges: list[Exchange]
    sent: list[tuple[float, str, str]]
    posters: list[FakePoster]
    usage: dict[str, float]
    memory: tuple[float, float] | None
    memory_before: tuple[float, float] | None
    lag: list[float] = field(default_factory=list)


async def replay(settings: Settings) -> dict[str, Any]:
    """Runs the scenario and returns its measurements."""
    shared = fetch._SHARED_QUEUES
    # main kept one queue per host, so www.sec.gov and data.sec.gov each got 8 a second
    fetch._SHARED_QUEUES = () if settings.sec == "per-host" else ("sec.gov",)
    try:
        return await _replay(settings)
    finally:
        fetch._SHARED_QUEUES = shared


async def _replay(settings: Settings) -> dict[str, Any]:
    companies = [Company(i) for i in range(settings.tickers)]
    clock = LocalClock()
    offset = clock.sync()[0].offset
    released_at = time.monotonic() + settings.warmup
    released = datetime.fromtimestamp(released_at + offset, UTC)
    plan = plan_for(
        companies,
        clock.now() - timedelta(seconds=1),
        released,
        released + timedelta(seconds=settings.tail),
        settings.with_sec,
    )
    world = World(companies, released_at, released, settings.seed)
    log, console = io.StringIO(), io.StringIO()
    posters: list[FakePoster] = []
    lag: list[float] = []
    memory_before = memory_mb()
    usage = Usage()
    usage.start()
    journal = Journal(log, clock)

    def new_poster() -> Poster:
        posters.append(FakePoster(journal))
        return posters[-1]

    lag_task = asyncio.create_task(loop_lag(lag))
    with contextlib.redirect_stdout(console):
        async with ReplayFetcher(world) as fetcher:
            releases = Releases(new_poster, fetcher, journal, None, None)
            await record(plan, fetcher, journal, releases.on_new)
            await releases.finish()
    lag_task.cancel()
    run = Run(
        settings,
        companies,
        released,
        released_at,
        [json.loads(line) for line in log.getvalue().splitlines()],
        fetcher.exchanges,
        fetcher.fake.sent,
        posters,
        usage.stop(),
        memory_mb(),
        memory_before,
        lag,
    )
    if settings.log_dir:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        (settings.log_dir / "journal.jsonl").write_text(log.getvalue(), encoding="utf-8")
        (settings.log_dir / "console.txt").write_text(console.getvalue(), encoding="utf-8")
    return measure(run)


def nearest_rank(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def spread(values: Sequence[float]) -> dict[str, float | int | None]:
    """Median, 95th percentile (nearest rank) and worst, in seconds."""
    if not values:
        return {"n": 0, "median": None, "p95": None, "max": None}
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "p95": round(nearest_rank(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def most_in_one_second(times: Sequence[float]) -> int:
    """The most of these moments that fall in any one-second span [t, t + 1)."""
    ordered = sorted(times)
    start, most = 0, 0
    for end, moment in enumerate(ordered):
        while moment - ordered[start] >= 1.0:
            start += 1
        most = max(most, end - start + 1)
    return most


CATEGORY = {"q4_press": "ir", "q4_files": "ir", "businesswire_busted": "wire", "prnewswire": "wire"}
CATEGORY |= {"edgar": "sec", "edgar_latest": "sec"}
BODY_KIND = {"SEC 8-K": "sec_body", "IR site": "ir_body", "PR Newswire": "wire_body"}


def measure(run: Run) -> dict[str, Any]:
    def after_r(stamp: str) -> float:
        return (datetime.fromisoformat(stamp) - run.released).total_seconds()

    events = run.events
    tickers: dict[str, dict[str, Any]] = {}
    for c in run.companies:
        mine = [e for e in events if e.get("ticker") == c.ticker]
        sightings = [e for e in mine if e["event"] == "new" and e["earnings"]]
        firsts: dict[str, float] = {}
        for e in sightings:
            category = CATEGORY.get(e["source"], "ir")  # rss:<host> is the company's own feed
            firsts[category] = min(firsts.get(category, math.inf), after_r(e["seen"]))
        first = min(sightings, key=lambda e: e["seen"], default=None)
        body = next((e for e in mine if e["event"] == "body" and "chars" in e), None)
        posted = [e for e in mine if e["event"] == "posted"]
        row: dict[str, Any] = {"kind": c.kind, "first_by_category": firsts, "posts": len(posted)}
        if first:
            # The poll that saw it: how long after R it waited for its next poll, then in the
            # queue (only the wait after R counts), then for the answer
            seen = after_r(first["seen"])
            fetched = min(seen, first["response_ms"] / 1000)
            queued = min(first["queued_ms"] / 1000, seen - fetched)
            row |= {
                "detect": seen,
                "detect_source": first["source"],
                "detect_queue": queued,
                "detect_fetch": fetched,
                "detect_poll_wait": seen - queued - fetched,
            }
        if body:
            row |= {"body": after_r(body["at"]), "body_source": body["source"]}
            kind = BODY_KIND.get(body["source"], "ir_body")
            done = run.released_at + row["body"] + 0.05
            used = [
                x
                for x in run.exchanges
                if x.owner == c.ticker and x.kind == kind and x.urgent and x.done <= done
            ]
            row |= {
                "body_queue": sum(x.queued for x in used),
                "body_fetch": sum(x.elapsed for x in used),
            }
        if posted:
            row |= {"alert": after_r(posted[0]["visible"]), "alert_source": posted[0]["source"]}
        tickers[c.ticker] = row

    def column(name: str, rows: Sequence[dict[str, Any]] | None = None) -> list[float]:
        return [r[name] for r in (rows or tickers.values()) if name in r]

    sec_sent = [t for t, _, url in run.sent if (urlsplit(url).hostname or "").endswith("sec.gov")]
    sec_after = [t for t in sec_sent if t >= run.released_at]
    by_host = Counter(urlsplit(url).hostname for t, _, url in run.sent if t >= run.released_at)
    queues: dict[str, Any] = {}
    for kind in sorted({x.kind for x in run.exchanges}):
        after = [x for x in run.exchanges if x.kind == kind and x.done >= run.released_at]
        queues[kind] = {
            "requests_after_r": len(after),
            "queue_wait": spread([x.queued for x in after]),
            "fetch_time": spread([x.elapsed for x in after]),
        }
    new_keys = Counter((e["ticker"], e["source"], e["key"]) for e in events if e["event"] == "new")
    asked: set[str] = set()
    repeats: list[Exchange] = []
    for x in sorted((x for x in run.exchanges if x.urgent), key=lambda x: x.done):
        url = re.sub(r"[?&]_=\d+", "", x.url)  # the cache-buster differs every time
        if url in asked:
            repeats.append(x)
        asked.add(url)
    errors = [e for e in events if e["event"] == "error"]
    late_starts = sum(e.get("caught_up", 0) for e in events if e["event"] == "ready")
    first_poll_done = max(
        (after_r(e["at"]) for e in events if e["event"] == "ready"), default=math.nan
    )
    by_category = {
        category: spread(
            [
                r["first_by_category"][category]
                for r in tickers.values()
                if category in r["first_by_category"]
            ]
        )
        for category in ("ir", "wire", "sec")
    }
    by_kind = {
        kind: {
            name: spread(column(name, [r for r in tickers.values() if r["kind"] == kind]))
            for name in ("detect", "body", "alert")
        }
        for kind in sorted({c.kind for c in run.companies})
    }
    s = run.settings
    return {
        "label": ("no SEC polls" if not s.with_sec else f"SEC queues: {s.sec}")
        + (f", seed {s.seed}" if s.seed != 1 else ""),
        "settings": {
            "tickers": s.tickers,
            "warmup_s": s.warmup,
            "tail_s": s.tail,
            "seed": s.seed,
            "sec": s.sec,
            "with_sec": s.with_sec,
        },
        "simulated": "server response times are simulated (LATENCY in tests/burst_replay.py)",
        "sec": {
            "requests": len(sec_sent),
            "most_in_any_second": most_in_one_second(sec_sent),
            "most_in_any_second_after_r": most_in_one_second(sec_after),
            "most_in_first_second": sum(1 for t in sec_sent if t < min(sec_sent) + 1)
            if sec_sent
            else 0,
        },
        "detect": spread(column("detect")),
        "detect_queue": spread(column("detect_queue")),
        "detect_fetch": spread(column("detect_fetch")),
        "detect_poll_wait": spread(column("detect_poll_wait")),
        "body": spread(column("body")),
        "body_queue": spread(column("body_queue")),
        "body_fetch": spread(column("body_fetch")),
        "alert": spread(column("alert")),
        "first_seen_by_category": by_category,
        "by_kind": by_kind,
        "alerts_from": dict(Counter(column_str(tickers, "alert_source"))),
        "bodies_from": dict(Counter(column_str(tickers, "body_source"))),
        "queues": queues,
        # Urgent downloads of a URL already asked for: both SEC sources reading one 8-K's
        # exhibit, say, or a release whose text another source already delivered
        "urgent_repeats": dict(Counter(x.kind for x in repeats)),
        "urgent_requests": dict(Counter(x.kind for x in run.exchanges if x.urgent)),
        "requests_after_r_by_host": dict(by_host.most_common()),
        "missed_alerts": [t for t, r in tickers.items() if not r["posts"]],
        "duplicate_alerts": [t for t, r in tickers.items() if r["posts"] > 1]
        + [f"{p.sends} sends" for p in run.posters if p.sends > 1],
        "duplicate_sightings": [list(key) for key, n in new_keys.items() if n > 1],
        "no_body": [t for t, r in tickers.items() if "body" not in r],
        "attachments": sum(len(p.files) for p in run.posters),
        "errors": len(errors),
        "timeouts": sum(1 for e in errors if e.get("timeout")),
        "first_polls_done_s": round(first_poll_done, 3),
        "caught_up_at_first_poll": late_starts,
        "loop_lag": spread(run.lag),
        "usage": run.usage,
        "memory_mb": None
        if run.memory is None
        else {
            "before": round(run.memory_before[0], 1) if run.memory_before else None,
            "after": round(run.memory[0], 1),
            "peak": round(run.memory[1], 1),
        },
        "tickers": tickers,
    }


def column_str(tickers: dict[str, dict[str, Any]], name: str) -> list[str]:
    return [r[name] for r in tickers.values() if name in r]


def show(result: dict[str, Any]) -> str:
    """The headline numbers, for a terminal."""
    lines = [f"{result['label']}  (simulated server times; seconds after R)"]
    sec = result["sec"]
    lines.append(
        f"  SEC: {sec['requests']} requests, at most {sec['most_in_any_second']} in any one"
        f" second ({sec['most_in_first_second']} in the first)"
    )
    for name in ("detect", "detect_queue", "detect_fetch", "body", "body_queue", "alert"):
        d = result[name]
        lines.append(f"  {name:<16} median {d['median']}  p95 {d['p95']}  max {d['max']}")
    for category, d in result["first_seen_by_category"].items():
        lines.append(
            f"  first seen, {category:<5} median {d['median']}  p95 {d['p95']}  max {d['max']}"
        )
    lines.append(
        f"  missed {result['missed_alerts']}  duplicates {result['duplicate_alerts']}"
        f"  no text {result['no_body']}  errors {result['errors']}"
    )
    usage, memory = result["usage"], result["memory_mb"]
    lines.append(
        f"  CPU {usage['cpu_seconds']}s, peak {usage['peak_cpu_percent']}% of a core;"
        f" memory peak {memory and memory['peak']} MB; loop lag max {result['loop_lag']['max']}s"
    )
    return "\n".join(lines)


def compare(paths: Sequence[Path]) -> str:
    """A Markdown table of several runs side by side."""
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]

    def spread_cell(d: dict[str, Any]) -> str:
        return (
            "-" if d["median"] is None else f"{d['median']:.2f} / {d['p95']:.2f} / {d['max']:.2f}"
        )

    rows: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("SEC requests in the busiest second", lambda r: str(r["sec"]["most_in_any_second"])),
        ("SEC requests in the first second", lambda r: str(r["sec"]["most_in_first_second"])),
        ("SEC requests in all", lambda r: str(r["sec"]["requests"])),
    ]
    for name, title in (
        ("detect", "First detection"),
        ("detect_poll_wait", "  waiting for a poll"),
        ("detect_queue", "  queue wait"),
        ("detect_fetch", "  fetch time"),
        ("body", "Usable text"),
        ("body_queue", "  queue wait"),
        ("body_fetch", "  fetch time"),
        ("alert", "First alert"),
    ):
        rows.append((f"{title} (median / p95 / max s)", lambda r, n=name: spread_cell(r[n])))
    for category, title in (("ir", "IR site"), ("wire", "Newswire"), ("sec", "SEC")):
        rows.append(
            (
                f"First seen on the {title} (median / p95 / max s)",
                lambda r, c=category: spread_cell(r["first_seen_by_category"][c]),
            )
        )
    for kind, title in (("ir", "IR"), ("rss", "own-feed"), ("sec", "SEC-only")):
        rows.append(
            (
                f"Usable text, {title} companies (median / p95 / max s)",
                lambda r, k=kind: spread_cell(r["by_kind"][k]["body"]),
            )
        )
    for kind, title in (
        ("ir_poll", "IR polls"),
        ("wire_poll", "Newswire polls"),
        ("sec_poll", "SEC polls"),
    ):
        rows.append(
            (
                f"Queue wait, {title} after R (median / p95 / max s)",
                lambda r, k=kind: (
                    spread_cell(r["queues"][k]["queue_wait"]) if k in r["queues"] else "-"
                ),
            )
        )
    rows += [
        (
            "Urgent SEC downloads (of them repeats)",
            lambda r: (
                f"{r['urgent_requests'].get('sec_body', 0)}"
                f" ({r['urgent_repeats'].get('sec_body', 0)})"
            ),
        ),
        (
            "Alerts by first source",
            lambda r: ", ".join(f"{k} {v}" for k, v in r["alerts_from"].items()),
        ),
        ("Missed alerts", lambda r: str(len(r["missed_alerts"]))),
        ("Duplicate alerts", lambda r: str(len(r["duplicate_alerts"]))),
        ("Tickers without text", lambda r: str(len(r["no_body"]))),
        ("Poll errors / timeouts", lambda r: f"{r['errors']} / {r['timeouts']}"),
        (
            "CPU seconds (peak % of a core)",
            lambda r: f"{r['usage']['cpu_seconds']} ({r['usage']['peak_cpu_percent']}%)",
        ),
        ("Peak memory (MB)", lambda r: str(r["memory_mb"]["peak"]) if r["memory_mb"] else "-"),
        ("Event loop lag, worst (s)", lambda r: str(r["loop_lag"]["max"])),
    ]
    header = "| | " + " | ".join(r["label"] for r in runs) + " |"
    lines = [header, "|---" * (len(runs) + 1) + "|"]
    lines += [
        "| " + title + " | " + " | ".join(cell(r) for r in runs) + " |" for title, cell in rows
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tests.burst_replay", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("--tickers", type=int, default=20)
    parser.add_argument("--warmup", type=float, default=8.0)
    parser.add_argument("--tail", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sec", choices=("shared", "per-host"), default="shared")
    parser.add_argument("--no-sec", action="store_true", help="leave out both SEC polls")
    parser.add_argument("--out", type=Path, help="write the measurements here as JSON")
    parser.add_argument("--log-dir", type=Path, help="keep the journal and console output here")
    parser.add_argument("--compare", type=Path, nargs="+", help="tabulate earlier --out files")
    args = parser.parse_args()
    if args.compare:
        print(compare(args.compare))
        return
    settings = Settings(
        args.tickers, args.warmup, args.tail, args.seed, args.sec, not args.no_sec, args.log_dir
    )
    result = asyncio.run(replay(settings))
    print(show(result))
    if args.out:
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
