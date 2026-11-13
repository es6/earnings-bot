import asyncio
import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from earnings_bot import fetch, pipeline
from earnings_bot.browser_sources import BrowserPDF, IRSource
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.ir_research import quarter_headline
from earnings_bot.pipeline import Poster, Release, Releases, describe_source
from earnings_bot.recorder import ET, Journal, Target, Trial, Watch, watch
from earnings_bot.source_document import SourceDocument, document_from_text
from earnings_bot.sources import (
    GLOBENEWSWIRE_EARNINGS,
    PR_NEWSWIRE_EARNINGS,
    PR_NEWSWIRE_SEMICONDUCTORS,
    BroadcomNewsroom,
    EdgarFilings,
    EdgarLatest,
    GuessedFile,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
)
from earnings_bot.summary import Summarizer, Summary, SummaryError
from tests.fakes import RoutedFetcher, sec_filing_detail, simple_pdf

PDF = "https://s23.q4cdn.com/1/files/Uber-Q3-26-Earnings-Press-Release.pdf"
MC = "https://corporate.mcdonalds.com/"
PDF_ONLY = MC + "release.pdf"
IR_PAGE = "https://investor.uber.com/news/uber-announces-results/default.aspx"
FILING = "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040/uber-20261104.htm"


class FakePoster(Poster):
    """Records each step, and stamps it with the current time the way Discord would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send(self, content: str) -> datetime | None:
        self.calls.append(("send", content))
        return datetime.now(UTC)

    async def edit(self, content: str) -> datetime | None:
        self.calls.append(("edit", content))
        return datetime.now(UTC)

    async def attach(self, name: str, data: bytes) -> datetime | None:
        self.calls.append(("attach", name))
        return datetime.now(UTC)

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        self.calls.append(("summary", f"{text} | {footer}"))
        return datetime.now(UTC)


class FakeSummarizer(Summarizer):
    def __init__(self) -> None:
        self.calls: list[str] = []  # the release text of each request
        self.contexts: list[dict | None] = []

    async def write(
        self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
    ) -> Summary:
        self.calls.append(release)
        self.contexts.append(context)
        return Summary("UBER $UBER Q3'26 EARNINGS HIGHLIGHTS", ["12%"], 0.0, 1000, 200)


def uber() -> Target:
    return Target(ticker="UBER", expected=datetime(2026, 11, 4, 6, 55, tzinfo=ET), cik=1543151)


def fetcher() -> RoutedFetcher:
    return RoutedFetcher({".pdf": (200, b"%PDF-1.7")})


@pytest.fixture
def release_texts(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The release text each source gives, by sighting URL."""
    texts: dict[str, str] = {}

    async def release_document(
        fetcher: object, source: Source, sighting: Sighting
    ) -> SourceDocument | None:
        text = texts.get(sighting.url)
        return document_from_text(text) if text else None

    monkeypatch.setattr(pipeline, "release_document", release_document)
    return texts


def test_newswires_are_named_in_the_alert() -> None:
    assert describe_source(RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, ["Lilly"])) == "PR Newswire"
    assert describe_source(RssFeed("businesswire_busted", "https://bw.example/rss")) == (
        "Business Wire"
    )
    semis = RssFeed("prnewswire_semiconductors", PR_NEWSWIRE_SEMICONDUCTORS, ["Broadcom Inc"])
    assert describe_source(semis) == "PR Newswire"
    globe = RssFeed("globenewswire_busted", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"], True)
    assert describe_source(globe) == "GlobeNewswire"
    assert describe_source(BroadcomNewsroom()) == "Broadcom newsroom"
    page = GuessedPage("https://mcdonalds.mediaroom.com/2026-11-04-McDONALDS-REPORTS")
    assert describe_source(page) == "mcdonalds.mediaroom.com"


async def test_release_posts_links_files_and_summary(release_texts: dict[str, str]) -> None:
    release_texts[IR_PAGE] = "Uber Announces Results for Third Quarter 2026 ..."
    poster, summarizer, log = FakePoster(), FakeSummarizer(), io.StringIO()
    release = Release(uber(), poster, fetcher(), Journal(log), summarizer)
    press = Sighting("1700", "Uber Announces Results", IR_PAGE, earnings=True)
    filing = Sighting("0001543151-26-000040", "8-K 2.02,9.01", FILING, earnings=True)

    release.add(Q4PressReleases("investor.uber.com"), press)
    release.add(GuessedFile(PDF), Sighting(PDF, "Uber-Q3-26-Earnings-Press-Release.pdf", PDF))
    release.add(EdgarFilings(1543151), filing)
    await release.finish()

    kinds = [kind for kind, _ in poster.calls]
    assert kinds[0] == "send"
    assert sorted(kinds) == ["attach", "edit", "edit", "send", "summary"]
    first_post = poster.calls[0][1]
    assert first_post.startswith("🚨 **$UBER earnings are out**")
    assert f"[IR site](<{IR_PAGE}>)" in first_post
    last_edit = [text for kind, text in poster.calls if kind == "edit"][-1]
    assert f"[SEC 8-K](<{FILING}>)" in last_edit
    assert f"[Uber-Q3-26-Earnings-Press-Release.pdf](<{PDF}>)" in last_edit
    summary = next(text for kind, text in poster.calls if kind == "summary")
    assert "from the IR site text" in summary
    assert "Summary by Muse Ember" in summary and "1 figure held back" in summary
    assert summarizer.calls == [release_texts[IR_PAGE]]
    # Each step logs when Discord stamped it, counted from the first sighting
    events = {e["event"]: e for e in map(json.loads, log.getvalue().splitlines())}
    for step in ("posted", "linked", "attached", "summary"):
        assert 0 <= events[step]["live_seconds"] < 5
        # Discord's own timestamp, when people could see the change
        visible = datetime.fromisoformat(events[step]["visible"])
        assert visible.tzinfo is not None
    assert events["posted"]["source"] == "IR site"
    body = events["body"]
    assert (body["source"], body["via"]) == ("IR site", "fetch")
    assert body["chars"] == len(release_texts[IR_PAGE])


async def test_both_sec_sources_give_the_filing_one_link() -> None:
    poster = FakePoster()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), None)
    index = FILING.rsplit("/", 1)[0] + "/0001543151-26-000040-index.htm"

    release.add(
        EdgarLatest(1543151), Sighting("0001543151-26-000040", "8-K 2.02", index, earnings=True)
    )
    release.add(
        EdgarFilings(1543151), Sighting("0001543151-26-000040", "8-K 2.02", FILING, earnings=True)
    )
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"[SEC 8-K](<{index}>)" in poster.calls[0][1]


FOLDER, PRIMARY = FILING.rsplit("/", 1)
ACCESSION = "0001543151-26-000040"
FILING_INDEX = f"{ACCESSION}-index.htm"


def filing_routes(exhibit: str = "ex99-1.htm") -> dict[str, tuple[int, bytes]]:
    """The filing's file lists, primary document and press release exhibit."""
    items = [{"name": PRIMARY, "size": "90000"}, {"name": exhibit, "size": "50000"}]
    paragraph = "<p>Uber Announces Results for Third Quarter 2026. Gross bookings grew 21%.</p>"
    return {
        FILING_INDEX: (
            200,
            sec_filing_detail(PRIMARY, exhibit).replace(
                b"</table>",
                b"<tr><td>3</td><td>Press release announcing cash dividend</td>"
                b"<td>dividend-release.htm</td><td>EX-99.2</td><td>100000</td></tr></table>",
            ),
        ),
        "index.json": (200, json.dumps({"directory": {"item": items}}).encode()),
        PRIMARY: (200, ("<p>" + "Form 8-K cover text. " * 120 + "</p>").encode()),
        exhibit: (200, f"<html><body>{paragraph * 40}</body></html>".encode()),
    }


def both_sec_sightings(release: Release) -> None:
    index = f"{FOLDER}/{FILING_INDEX}"
    release.add(EdgarLatest(1543151), Sighting(ACCESSION, "8-K 2.02", index, earnings=True))
    release.add(EdgarFilings(1543151), Sighting(ACCESSION, "8-K 2.02", FILING, earnings=True))


@pytest.mark.parametrize("exhibit", ["ex99-1.htm", "report.htm"])
async def test_both_sec_sources_download_the_filings_exhibit_once(exhibit: str) -> None:
    sec, summarizer, log = RoutedFetcher(filing_routes(exhibit)), FakeSummarizer(), io.StringIO()
    release = Release(uber(), FakePoster(), sec, Journal(log), summarizer)

    both_sec_sightings(release)  # before any text has arrived
    await release.finish()

    assert sec.urgent == [f"{FOLDER}/{FILING_INDEX}", f"{FOLDER}/{exhibit}"]
    [text] = summarizer.calls
    assert "Gross bookings grew 21%" in text
    events = [e for e in map(json.loads, log.getvalue().splitlines()) if e["event"] == "body"]
    assert [e["source"] for e in events] == ["SEC 8-K"]


class ListedLate(RoutedFetcher):
    """The filing's detail page is still missing when the latest-filings source reads it."""

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
        if url.endswith(f"/{FILING_INDEX}"):
            return Response(url, 404, {}, b"", 0.01)
        return response


async def test_the_other_sec_source_tries_again_when_the_first_read_gets_nothing() -> None:
    sec, log = ListedLate(filing_routes()), io.StringIO()
    release = Release(uber(), FakePoster(), sec, Journal(log), None)

    both_sec_sightings(release)
    await release.finish()

    assert sec.urgent == [
        f"{FOLDER}/{FILING_INDEX}",
        f"{FOLDER}/index.json",
        f"{FOLDER}/ex99-1.htm",
    ]
    events = [e for e in map(json.loads, log.getvalue().splitlines()) if e["event"] == "body"]
    assert [e["source"] for e in events] == ["SEC 8-K"]


async def test_only_one_summary_when_several_sources_have_the_text(
    release_texts: dict[str, str],
) -> None:
    release_texts[IR_PAGE] = release_texts[FILING] = "the release"
    poster, summarizer, log = FakePoster(), FakeSummarizer(), io.StringIO()
    release = Release(uber(), poster, fetcher(), Journal(log), summarizer)

    release.add(Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", IR_PAGE))
    release.add(EdgarFilings(1543151), Sighting("acc", "8-K 2.02", FILING, earnings=True))
    await release.finish()

    assert len(summarizer.calls) == 1
    # The text's arrival is logged once, for whichever source delivered it first
    assert [e["event"] for e in map(json.loads, log.getvalue().splitlines())].count("body") == 1


def broadcom_release() -> tuple[BroadcomNewsroom, Sighting]:
    newsroom = BroadcomNewsroom()
    path = "/company/news/financial-releases/64900"
    title = "Broadcom Inc. Announces Fourth Quarter and Fiscal Year 2026 Financial Results"
    url = f"https://www.broadcom.com{path}"
    return newsroom, Sighting("64900", title, url, earnings=True, details={"path": path})


async def test_a_newly_added_source_posts_like_any_other() -> None:
    # No source waits on a trial: a release day is the only way to time a new one
    posters: list[FakePoster] = []

    def new_poster() -> FakePoster:
        posters.append(FakePoster())
        return posters[-1]

    releases = Releases(new_poster, fetcher(), Journal(io.StringIO()), None)
    newsroom, results = broadcom_release()
    expected = datetime(2026, 12, 10, 16, 15, tzinfo=ET)
    added = Target(ticker="AVGO", expected=expected, cik=1730168, trial=Trial())

    releases.on_new(added, newsroom, results)
    await releases.finish()
    [poster] = posters
    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"[Broadcom newsroom](<{results.url}>)" in poster.calls[0][1]


async def test_the_same_release_from_both_copies_of_a_feed_posts_and_downloads_once() -> None:
    url = "https://www.globenewswire.com/news-release/2026/11/18/3400000/0/en/nvidia-results.html"
    article = url.replace("www.", "rss.", 1)
    fetch = RoutedFetcher({article: (200, b"<p>Revenue and earnings per share. </p>" * 100)})
    poster, log = FakePoster(), io.StringIO()
    nvda = Target(ticker="NVDA", expected=datetime(2026, 11, 18, 16, 20, tzinfo=ET), cik=1045810)
    release = Release(nvda, poster, fetch, Journal(log), None)
    results = Sighting(url, "NVIDIA Announces Financial Results", url, earnings=True)

    release.add(RssFeed("globenewswire", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"]), results)
    release.add(RssFeed("globenewswire_busted", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"], True), results)
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"[GlobeNewswire](<{url}>)" in poster.calls[0][1]
    assert fetch.requests == [("GET", article, None)]
    # The text is still fetched without a summarizer, to log when it arrives
    events = [e["event"] for e in map(json.loads, log.getvalue().splitlines())]
    assert events.count("body") == 1


async def test_an_alert_for_a_release_already_out_says_so() -> None:
    poster = FakePoster()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), None)
    index = FILING.rsplit("/", 1)[0] + "/0001543151-26-000040-index.htm"

    release.add(
        EdgarLatest(1543151),
        Sighting("0001543151-26-000040", "8-K 2.02", index, earnings=True, caught_up=True),
    )
    await release.finish()

    [(_, alert)] = poster.calls
    assert "-# Already out when the bot started watching · First seen" in alert


class UploadedLater(RoutedFetcher):
    """The PDF answers 404 until its third request."""

    def __init__(self) -> None:
        super().__init__({})
        self.pdf_requests = 0

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
        if url != PDF:
            return await super().request(method, url, headers, give_up_after)
        self.pdf_requests += 1
        if self.pdf_requests < 3:
            return Response(url, 404, {}, b"Not found", 0.01)
        return Response(url, 200, {}, b"%PDF-1.7", 0.01)


def listed_pdf() -> Sighting:
    return Sighting(PDF, "Third Quarter 2026: Earnings release", PDF, earnings=True)


async def test_a_file_listed_before_its_upload_is_attached_once_it_goes_up() -> None:
    poster, files = FakePoster(), UploadedLater()
    tonight = Target(ticker="UBER", expected=datetime.now(ET), cik=1543151)
    release = Release(tonight, poster, files, Journal(io.StringIO()), None)
    release.attach_retry = 0.01

    release.add(Q4FinancialReports("investor.uber.com"), listed_pdf())
    await release.finish()

    assert files.pdf_requests == 3
    assert [kind for kind, _ in poster.calls] == ["send", "attach"]


async def test_a_missing_file_isnt_retried_after_the_window_closes() -> None:
    poster, files = FakePoster(), UploadedLater()
    release = Release(uber(), poster, files, Journal(io.StringIO()), None)  # Nov 4
    release.attach_retry = 0.01

    release.add(Q4FinancialReports("investor.uber.com"), listed_pdf())
    await release.finish()

    assert files.pdf_requests == 1
    assert [kind for kind, _ in poster.calls] == ["send"]


async def test_browser_pdf_attaches_already_verified_bytes_without_another_get() -> None:
    warmup = "https://corporate.mcdonalds.com/"
    url = warmup + "release.pdf"
    source = BrowserPDF(url, warmup)
    payload = Response(url, 200, {}, b"%PDF-1.7\n", 0.04)
    sighting = source.parse(payload)[0]
    raw = RoutedFetcher({})
    poster = FakePoster()
    release = Release(uber(), poster, raw, Journal(io.StringIO()), None)

    release.add(source, sighting)
    release.add(source, sighting)
    await release.finish()

    assert raw.requests == []
    assert [kind for kind, _ in poster.calls] == ["send", "attach"]


async def test_keep_warm_pings_discord_only_while_a_window_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "KEEP_WARM_SECONDS", 0.01)
    journal = Journal(io.StringIO())
    now = journal.now()
    windows = [(now - timedelta(minutes=1), now + timedelta(minutes=1))]
    pings = 0

    async def ping() -> None:
        nonlocal pings
        pings += 1
        if pings == 2:
            raise aiohttp.ClientConnectionError("connection dropped")

    warm = asyncio.create_task(pipeline.keep_warm(ping, lambda: windows, journal))
    try:
        await asyncio.sleep(0.1)
        assert pings > 3  # it kept going after a failed ping
        windows.clear()
        await asyncio.sleep(0.02)
        quiet = pings
        await asyncio.sleep(0.1)
        assert pings == quiet
    finally:
        warm.cancel()


async def test_the_summary_starts_as_soon_as_the_text_arrives(
    release_texts: dict[str, str],
) -> None:
    """Each release looked up FMP's consensus estimates, and the summary waited for the answer,
    though the writer never sent estimates to the model."""
    release_texts[IR_PAGE] = "the release"
    started: list[float] = []

    class TimedSummarizer(FakeSummarizer):
        async def write(
            self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
        ) -> Summary:
            started.append(time.monotonic())
            return await super().write(ticker, release, seconds)

    class SlowCalendar(RoutedFetcher):
        async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Response:
            if "financialmodelingprep.com" in url:
                await asyncio.sleep(1)
            return await super().request(method, url, *args, **kwargs)

    fetch = SlowCalendar({})
    releases = Releases(FakePoster, fetch, Journal(io.StringIO()), TimedSummarizer())
    added = time.monotonic()

    press = Sighting("1700", "Uber Announces Results", IR_PAGE, earnings=True)
    releases.on_new(uber(), Q4PressReleases("investor.uber.com"), press)
    await releases.finish()

    assert started and started[0] - added < 0.5
    assert not [url for _, url, _ in fetch.requests if "financialmodelingprep.com" in url]


async def test_a_file_too_big_for_discord_stays_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "MAX_FILE_BYTES", 1000)
    poster = FakePoster()
    big = RoutedFetcher({".pdf": (200, b"%PDF-1.7" + bytes(1000))})
    release = Release(uber(), poster, big, Journal(io.StringIO()), None)

    release.add(GuessedFile(PDF), Sighting(PDF, "Uber-Q3-26-Earnings-Press-Release.pdf", PDF))
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send"]
    assert PDF in poster.calls[0][1]


class FailsOnceSummarizer(FakeSummarizer):
    async def write(
        self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
    ) -> Summary:
        if not self.calls:
            self.calls.append(release)
            raise SummaryError("Claude API error 529: Overloaded")
        return await super().write(ticker, release, seconds)


async def test_a_failed_summary_is_tried_again_from_the_next_source(
    release_texts: dict[str, str],
) -> None:
    release_texts[IR_PAGE], release_texts[FILING] = "from the IR site", "from the 8-K"
    poster, summarizer = FakePoster(), FailsOnceSummarizer()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), summarizer)

    release.add(Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", IR_PAGE))
    await release.finish()
    release.add(EdgarFilings(1543151), Sighting("acc", "8-K 2.02", FILING, earnings=True))
    await release.finish()

    assert summarizer.calls == ["from the IR site", "from the 8-K"]
    assert [kind for kind, _ in poster.calls].count("summary") == 1


async def test_an_alert_with_too_many_links_is_cut_to_discords_limit() -> None:
    poster = FakePoster()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), None)
    feed = RssFeed("issuer", "https://issuer.example/rss")

    for n in range(40):
        url = f"https://issuer.example/news/{n}/uber-announces-third-quarter-2026-results"
        release.add(feed, Sighting(str(n), "Results", url, earnings=True))
    await release.finish()

    longest = max(len(content) for _, content in poster.calls)
    assert longest == pipeline.MAX_CONTENT
    assert poster.calls[-1][1].endswith("…")


async def test_an_alert_names_the_quarter_and_masks_each_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Target(
        ticker="LCID",
        expected=datetime(2026, 11, 9, 16, 5, tzinfo=ET),
        cik=1811210,
        period="FY2026Q3",
    )
    poster = FakePoster()
    journal = Journal(io.StringIO())
    monkeypatch.setattr(journal, "now", lambda: target.expected)
    release = Release(target, poster, RoutedFetcher({}), journal, None)
    wire = RssFeed("prnewswire_busted", "https://www.prnewswire.com/rss/x.rss", ["Lucid Group"])
    article = "https://www.prnewswire.com/news-releases/lucid-q3.html"
    filing = "https://www.sec.gov/Archives/edgar/data/1811210/000162828026072158/index.htm"
    deck = "https://ir.lucidmotors.com/files/Q3_2026_Results.pdf"

    release.add(
        wire, Sighting("q3", "Lucid Announces Third Quarter 2026 Results", article, earnings=True)
    )
    await release.finish()
    release.add(EdgarFilings(1811210), Sighting("8k", "8-K 2.02", filing, earnings=True))
    release.add(GuessedFile(deck), Sighting(deck, "deck", deck, earnings=True, body=b"%PDF-1.7"))
    await release.finish()

    first = poster.calls[0][1].splitlines()
    assert first[0] == "🚨 **$LCID Q3 FY26 earnings are out**"
    assert first[1].startswith("-# First seen ") and first[1].endswith(" PM ET via PR Newswire")
    assert first[2:] == [f"📰 [PR Newswire](<{article}>)"]
    last = [content for kind, content in poster.calls if kind == "edit"][-1].splitlines()
    assert last[2:] == [
        f"📰 [PR Newswire](<{article}>)",
        f"🏛️ [SEC 8-K](<{filing}>)",
        # Markdown in a file's name is escaped, so it shows as written
        f"📄 [Q3\\_2026\\_Results.pdf](<{deck}>)",
    ]


RELEASE_LINES = [
    "Uber Announces Results for Third Quarter 2026",
    *["Revenue grew 20% and net income was $1.2 billion, or $0.56 per share." for _ in range(40)],
]


async def test_a_release_pdf_is_summarized_when_no_page_has_the_text() -> None:
    """Brookfield's Q3 2026 PDF was attached at 7:06:36 and the summary waited for the 6-K,
    4 minutes later."""
    source = BrowserPDF(PDF_ONLY, MC)
    sighting = source.parse(Response(PDF_ONLY, 200, {}, simple_pdf(RELEASE_LINES), 0.04))[0]
    poster, summarizer, log = FakePoster(), FakeSummarizer(), io.StringIO()
    release = Release(uber(), poster, RoutedFetcher({}), Journal(log), summarizer)

    release.add(source, sighting)
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send", "attach", "summary"]
    assert "Uber Announces Results for Third Quarter 2026" in summarizer.calls[0]
    assert "from the release.pdf text" in poster.calls[-1][1]


def pdf_sighting() -> tuple[BrowserPDF, Sighting]:
    source = BrowserPDF(PDF_ONLY, MC)
    return source, source.parse(Response(PDF_ONLY, 200, {}, simple_pdf(RELEASE_LINES), 0.04))[0]


async def test_a_pdf_is_summarized_at_once_and_a_page_replaces_it_once(
    release_texts: dict[str, str], tmp_path: Path
) -> None:
    """BN's summary waited 4 minutes for a page with the PDF already in hand, and every
    PDF-first release waited 10 s more. A page reads better, though: the checker kept 13 of
    BN's 21 figures from the PDF and 20 of 25 from the page."""
    release_texts[PDF_ONLY] = "the PDF's text"
    release_texts[IR_PAGE] = "the page's text"
    release_texts[IR_PAGE + "?copy"] = "another copy of the page"
    poster, summarizer, log = FakePoster(), FakeSummarizer(), io.StringIO()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        release = Release(uber(), poster, RoutedFetcher({}), Journal(log), summarizer, jobs=store)
        release.add(*pdf_sighting())
        await release.finish()
        assert summarizer.calls == ["the PDF's text"]

        release.add(*ir_sighting())
        await release.finish()
        release.add(*ir_sighting(IR_PAGE + "?copy"))  # one replacement only
        await release.finish()
        job = store.summary(uber().release_key)
        pdf = document_from_text("the PDF's text")
        assert store.summary_document(uber().release_key, pdf.sha256) == pdf.to_json()

    assert summarizer.calls == ["the PDF's text", "the page's text"]
    assert [kind for kind, _ in poster.calls].count("summary") == 2
    posted = [r for r in map(json.loads, log.getvalue().splitlines()) if r["event"] == "summary"]
    assert "replaces" not in posted[0] and posted[1]["replaces"] == "release.pdf"
    assert job is not None and (job.source, job.replaced, job.attempts) == (
        "IR site",
        "release.pdf",
        2,
    )
    assert job.source_document == document_from_text("the page's text").to_json()


async def test_a_page_that_comes_during_the_pdf_summary_replaces_it_after(
    release_texts: dict[str, str],
) -> None:
    release_texts[PDF_ONLY] = "the PDF's text"
    release_texts[IR_PAGE] = "the page's text"

    class Slow(FakeSummarizer):
        async def write(
            self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
        ) -> Summary:
            await asyncio.sleep(0.1)
            return await super().write(ticker, release, seconds)

    poster, summarizer = FakePoster(), Slow()
    release = Release(uber(), poster, RoutedFetcher({}), Journal(io.StringIO()), summarizer)
    release.add(*pdf_sighting())
    await asyncio.sleep(0.03)  # the PDF's summary is being written
    release.add(*ir_sighting())
    await release.finish()

    assert summarizer.calls == ["the PDF's text", "the page's text"]
    summaries = [text for kind, text in poster.calls if kind == "summary"]
    assert len(summaries) == 2  # the page's summary went up after, and over, the PDF's


async def test_a_pdf_never_replaces_a_page_based_summary(release_texts: dict[str, str]) -> None:
    release_texts[PDF_ONLY] = "the PDF's text"
    release_texts[IR_PAGE] = "the page's text"
    summarizer = FakeSummarizer()
    release = Release(uber(), FakePoster(), RoutedFetcher({}), Journal(io.StringIO()), summarizer)

    release.add(*ir_sighting())
    await release.finish()
    release.add(*pdf_sighting())
    await release.finish()

    assert summarizer.calls == ["the page's text"]


async def test_a_deck_or_unreadable_pdf_is_not_summarized() -> None:
    source = BrowserPDF(PDF_ONLY, MC)
    deck = simple_pdf(["Q3 2026 Investor Presentation", "Safe harbor statement"] * 100)
    sighting = source.parse(Response(PDF_ONLY, 200, {}, deck, 0.04))[0]
    poster, summarizer = FakePoster(), FakeSummarizer()
    release = Release(uber(), poster, RoutedFetcher({}), Journal(io.StringIO()), summarizer)

    release.add(source, sighting)
    await release.finish()

    assert summarizer.calls == []
    assert [kind for kind, _ in poster.calls] == ["send", "attach"]


async def test_a_wire_alert_without_text_reads_the_release_from_the_issuers_newsroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business Wire's article pages are closed to us. Nebius's Q3 2026 alert came from its feed
    while the newsroom watcher was stopped, so the summary waited 2h 7m for the 6-K."""
    monkeypatch.setattr(pipeline, "ISSUER_TEXT_EVERY", 0.01)
    listing = "https://nebius.com/newsroom"
    article = listing + "/nebius-reports-third-quarter-2026-financial-results"
    title = "Nebius reports third quarter 2026 financial results"
    target = Target(
        ticker="NBIS",
        cik=1513845,
        period="FY2026Q3",
        expected=datetime(2026, 11, 12, 8, 0, tzinfo=ET),
        ir=IRSource(
            listing=listing, headline=quarter_headline(2026, 3), verify_text=True, http_first=True
        ),
    )
    page = f"<h1>{title}</h1><p>{'Revenues and net income per share grew. ' * 120}</p>".encode()
    # The newsroom lists the release only from the second look
    listings = iter([b"<a href='/newsroom/other'>Nebius opens a data center</a>"])
    link = f'<a href="{article}">{title}</a>'.encode()

    class Newsroom(RoutedFetcher):
        async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Response:
            self.requests.append((method, url, None))
            if url.lower() == listing:
                return Response(url, 200, {}, next(listings, link), 0.01)
            if url == article:
                return Response(url, 200, {}, page, 0.01)
            return Response(url, 404, {}, b"", 0.01)

    fetcher = Newsroom({})
    poster, summarizer, log = FakePoster(), FakeSummarizer(), io.StringIO()
    release = Release(target, poster, fetcher, Journal(log), summarizer)
    wire = RssFeed("businesswire_busted", "https://feed.businesswire.com/rss/home/?rss=x")
    alert = Sighting(
        "20261112208769en",
        title,
        "http://www.businesswire.com/news/home/20261112208769/en/Nebius",
        earnings=True,
    )

    release.add(wire, alert)
    release.add(wire, alert)  # a second copy of the feed starts no second search
    await release.finish()

    assert len(summarizer.calls) == 1
    assert "Revenues and net income per share grew." in summarizer.calls[0]
    assert [url for _, url, _ in fetcher.requests if url == article] == [article]
    assert f"](<{article}>)" in next(
        text for kind, text in reversed(poster.calls) if kind == "edit"
    )
    events = [json.loads(line)["event"] for line in log.getvalue().splitlines()]
    assert events.count("issuer_text_search") == 1


NBIS_LISTING = "https://nebius.com/newsroom"
NBIS_TITLE = "Nebius reports third quarter 2026 financial results"
NBIS_ARTICLE = NBIS_LISTING + "/nebius-reports-third-quarter-2026-financial-results"
NBIS_WIRE = RssFeed("businesswire_busted", "https://feed.businesswire.com/rss/home/?rss=x")
NBIS_ALERT = Sighting(
    "20261112208769en",
    NBIS_TITLE,
    "http://www.businesswire.com/news/home/20261112208769/en/Nebius",
    earnings=True,
)


def nebius() -> Target:
    return Target(
        ticker="NBIS",
        cik=1513845,
        period="FY2026Q3",
        expected=datetime(2026, 11, 12, 8, 0, tzinfo=ET),
        ir=IRSource(
            listing=NBIS_LISTING,
            headline=quarter_headline(2026, 3),
            verify_text=True,
            http_first=True,
        ),
    )


class NebiusNewsroom(RoutedFetcher):
    """Refuses the listing for `refuse_for` seconds, over HTTP and in Chrome alike, then lists
    the release."""

    def __init__(self, refuse_for: float = 0.0) -> None:
        super().__init__({})
        self.refused_until = time.monotonic() + refuse_for
        # When each request for the listing went out, and the status it got
        self.listing_asked: list[tuple[float, int]] = []

    def _answer(self, url: str) -> Response:
        if url.lower() == NBIS_LISTING:
            status = 403 if time.monotonic() < self.refused_until else 200
            self.listing_asked.append((time.monotonic(), status))
            link = f'<a href="{NBIS_ARTICLE}">{NBIS_TITLE}</a>'.encode()
            return Response(url, status, {}, link if status == 200 else b"", 0.01)
        if url == NBIS_ARTICLE:
            body = f"<h1>{NBIS_TITLE}</h1><p>{'Revenues and net income grew. ' * 120}</p>"
            return Response(url, 200, {}, body.encode(), 0.01)
        return Response(url, 404, {}, b"", 0.01)

    async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Response:
        self.requests.append((method, url, None))
        return self._answer(url)

    async def browser_request(
        self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
    ) -> Response:
        return self._answer(url)


async def test_the_newsroom_read_waits_out_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read that stands in for a closed wire article asked again 5 s after a 403, though
    Nebius's newsroom had refused us for polling it too often."""
    monkeypatch.setattr(fetch, "BLOCK_PAUSE", 0.3)
    monkeypatch.setattr(pipeline, "ISSUER_TEXT_EVERY", 0.01)
    fetcher, log = NebiusNewsroom(refuse_for=0.1), io.StringIO()
    summarizer = FakeSummarizer()
    release = Release(nebius(), FakePoster(), fetcher, Journal(log), summarizer)

    release.add(NBIS_WIRE, NBIS_ALERT)
    await release.finish()

    refused = [at for at, status in fetcher.listing_asked if status == 403]
    answered = [at for at, status in fetcher.listing_asked if status == 200]
    assert refused and answered
    assert answered[0] - refused[-1] >= 0.3
    assert len(summarizer.calls) == 1
    errors = [
        record
        for line in log.getvalue().splitlines()
        if (record := json.loads(line))["event"] == "issuer_text_error"
    ]
    assert [(error["message"][:8], error["retry_in"]) for error in errors] == [("HTTP 403", 0.3)]


async def test_the_newsroom_read_waits_while_the_newsrooms_watcher_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "BLOCK_PAUSE", 0.3)
    fetcher = NebiusNewsroom()
    # The window's own watcher of the newsroom was just refused
    fetcher.route_pause(NBIS_LISTING).refused()
    started = time.monotonic()
    release = Release(nebius(), FakePoster(), fetcher, Journal(io.StringIO()), None)

    release.add(NBIS_WIRE, NBIS_ALERT)
    await release.finish()

    assert fetcher.listing_asked and fetcher.listing_asked[0][0] - started >= 0.29


async def test_the_newsroom_watcher_waits_while_a_read_of_the_newsroom_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "BLOCK_PAUSE", 0.3)
    target = nebius()
    assert target.ir is not None
    day = target.expected.astimezone(ET).date()
    source = next(s for s in target.ir.sources(day, target.period) if s.pause_route)
    fetcher = NebiusNewsroom()
    # A release's read of the newsroom was just refused
    fetcher.route_pause(NBIS_LISTING).refused()
    journal = Journal(io.StringIO())
    started = time.monotonic()

    await watch(Watch("NBIS", source), fetcher, journal, journal.now() + timedelta(seconds=1))

    assert fetcher.listing_asked and fetcher.listing_asked[0][0] - started >= 0.29


def ir_sighting(url: str = IR_PAGE) -> tuple[Q4PressReleases, Sighting]:
    return Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", url, earnings=True)


async def test_a_restart_doesnt_summarize_a_delivered_release_again(
    release_texts: dict[str, str], tmp_path: Path
) -> None:
    """The Nov 12 restart re-read both delivered releases and summarized each again: two paid
    calls, and edits to alerts hours old."""
    release_texts[IR_PAGE] = "the release"
    summarizer, log = FakeSummarizer(), io.StringIO()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for _ in range(2):  # the bot, then the bot after a restart, reading the same release
            release = Release(uber(), FakePoster(), fetcher(), Journal(log), summarizer, jobs=store)
            release.add(*ir_sighting())
            await release.finish()
        job = store.summary(uber().release_key)

    assert summarizer.calls == ["the release"]
    assert job is not None and (job.status, job.attempts, job.source) == ("delivered", 1, "IR site")
    document = document_from_text("the release")
    assert job.document == document.sha256 and job.source_document == document.to_json()
    skipped = [
        r for r in map(json.loads, log.getvalue().splitlines()) if r["event"] == "summary_skipped"
    ]
    assert [r["reason"] for r in skipped] == ["already delivered"]


async def test_a_summary_written_before_a_crash_is_posted_with_no_new_call(
    release_texts: dict[str, str], tmp_path: Path
) -> None:
    release_texts[IR_PAGE] = "the release"
    key = uber().release_key
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.summary_started(key, "abc", "SEC 8-K", "2026-11-04")
        store.summary_written(key, "**Q3 2026**", "Summary by Muse Ember in 4s", [], 900, 70)
        summarizer, poster = FakeSummarizer(), FakePoster()
        release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), summarizer, jobs=store)
        release.add(*ir_sighting())
        await release.finish()
        job = store.summary(key)

    assert summarizer.calls == []
    assert ("summary", "**Q3 2026** | Summary by Muse Ember in 4s") in poster.calls
    assert job is not None and (job.status, job.attempts) == ("delivered", 1)


class AlwaysFails(FakeSummarizer):
    async def write(
        self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
    ) -> Summary:
        self.calls.append(release)
        raise SummaryError("Ember API error 529: Overloaded")


async def test_a_release_gets_at_most_three_model_calls_across_restarts(
    release_texts: dict[str, str], tmp_path: Path
) -> None:
    pages = [f"{IR_PAGE}?copy={n}" for n in range(5)]
    for page in pages:
        release_texts[page] = f"text from {page}"
    summarizer, log = AlwaysFails(), io.StringIO()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for page in pages:  # each a new source, after a restart
            release = Release(uber(), FakePoster(), fetcher(), Journal(log), summarizer, jobs=store)
            release.add(*ir_sighting(page))
            await release.finish()

    assert len(summarizer.calls) == 3
    skipped = [
        r for r in map(json.loads, log.getvalue().splitlines()) if r["event"] == "summary_skipped"
    ]
    assert [r["reason"] for r in skipped] == ["3 model calls already made for this release"] * 2


async def test_summary_calls_stop_at_the_days_cap(
    release_texts: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "DAILY_SUMMARY_CALLS", 1)
    release_texts[IR_PAGE] = "the release"
    lyft = Target(ticker="LYFT", expected=datetime(2026, 11, 4, 16, 5, tzinfo=ET), cik=1759509)
    summarizer = FakeSummarizer()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for target in (uber(), lyft):
            release = Release(
                target, FakePoster(), fetcher(), Journal(io.StringIO()), summarizer, jobs=store
            )
            release.add(*ir_sighting())
            await release.finish()

    assert summarizer.calls == ["the release"]


async def test_at_most_two_model_calls_run_at_once(
    release_texts: dict[str, str], tmp_path: Path
) -> None:
    release_texts[IR_PAGE] = "the release"
    running = most = 0

    class Slow(FakeSummarizer):
        async def write(
            self, ticker: str, release: str, seconds: float = 0.0, *, context: dict | None = None
        ) -> Summary:
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.05)
            running -= 1
            return await super().write(ticker, release, seconds)

    targets = [
        Target(ticker=f"T{n}", expected=datetime(2026, 11, 4, 16, 5, tzinfo=ET), cik=1000 + n)
        for n in range(3)
    ]
    summarizer = Slow()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        releases = Releases(FakePoster, fetcher(), Journal(io.StringIO()), summarizer, store=store)
        for target in targets:
            releases.on_new(target, *ir_sighting())
        await releases.finish()

    assert len(summarizer.calls) == 3
    assert most == 2


async def test_release_writer_reads_prepared_context_without_blocking_detection(
    release_texts: dict[str, str],
) -> None:
    release_texts[IR_PAGE] = "the current release"
    writer = FakeSummarizer()
    context = {"advisory_prior_release_profile": {"usual_kpi": "Gross Bookings"}}
    releases = Releases(FakePoster, fetcher(), Journal(io.StringIO()), writer)
    releases.issuer_context = lambda target: context if target.cik == uber().cik else {}
    releases.on_new(uber(), *ir_sighting())
    await releases.finish()
    assert writer.calls == ["the current release"] and writer.contexts == [context]


async def test_a_file_link_reads_without_url_escapes() -> None:
    """Nebius's Q3 2026 PDF showed as "Nebius%20Q3%202026%20Financial%20Results..."."""
    url = "https://nebius.com/files/Nebius%20Q3%202026%20Results.pdf"
    poster = FakePoster()
    release = Release(uber(), poster, RoutedFetcher({}), Journal(io.StringIO()), None)

    release.add(GuessedFile(url), Sighting(url, "Nebius Q3 2026 Results", url))
    await release.finish()

    assert f"[Nebius Q3 2026 Results.pdf](<{url}>)" in poster.calls[0][1]
