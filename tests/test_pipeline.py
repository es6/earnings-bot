import io
import json
from datetime import UTC, datetime

import pytest

from earnings_bot import pipeline
from earnings_bot.browser_sources import BrowserPDF
from earnings_bot.estimates import Estimates
from earnings_bot.fetch import Response
from earnings_bot.pipeline import Poster, Release, Releases, describe_source
from earnings_bot.recorder import ET, Journal, Target, Trial
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
from earnings_bot.summary import Summarizer, Summary
from tests.fakes import RoutedFetcher, sec_filing_detail

PDF = "https://s23.q4cdn.com/1/files/Uber-Q3-26-Earnings-Press-Release.pdf"
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
        self.calls: list[tuple[str, Estimates | None]] = []

    async def write(
        self, ticker: str, release: str, estimates: Estimates | None, seconds: float = 0.0
    ) -> Summary:
        self.calls.append((release, estimates))
        return Summary("UBER $UBER Q3'26 EARNINGS HIGHLIGHTS", ["12%"], 0.0, 1000, 200)


def uber() -> Target:
    return Target(ticker="UBER", expected=datetime(2026, 11, 4, 6, 55, tzinfo=ET), cik=1543151)


def fetcher() -> RoutedFetcher:
    calendar = [{"symbol": "UBER", "date": "2026-11-04", "epsEstimated": 0.86}]
    return RoutedFetcher(
        {".pdf": (200, b"%PDF-1.7"), "earnings-calendar": (200, json.dumps(calendar).encode())}
    )


@pytest.fixture
def release_texts(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The release text each source gives, by sighting URL."""
    texts: dict[str, str] = {}

    async def release_text(fetcher: object, source: Source, sighting: Sighting) -> str | None:
        return texts.get(sighting.url)

    monkeypatch.setattr(pipeline, "release_text", release_text)
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
    release = Release(uber(), poster, fetcher(), Journal(log), summarizer, "key")
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
    assert f"IR site: <{IR_PAGE}>" in first_post
    last_edit = [text for kind, text in poster.calls if kind == "edit"][-1]
    assert f"SEC 8-K: <{FILING}>" in last_edit
    assert f"Uber-Q3-26-Earnings-Press-Release.pdf: <{PDF}>" in last_edit
    summary = next(text for kind, text in poster.calls if kind == "summary")
    assert "from the IR site text" in summary
    assert "12%" in summary
    assert summarizer.calls == [(release_texts[IR_PAGE], Estimates(eps=0.86, revenue=None))]
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
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), None, None)
    index = FILING.rsplit("/", 1)[0] + "/0001543151-26-000040-index.htm"

    release.add(
        EdgarLatest(1543151), Sighting("0001543151-26-000040", "8-K 2.02", index, earnings=True)
    )
    release.add(
        EdgarFilings(1543151), Sighting("0001543151-26-000040", "8-K 2.02", FILING, earnings=True)
    )
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"SEC 8-K: <{index}>" in poster.calls[0][1]


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
    release = Release(uber(), FakePoster(), sec, Journal(log), summarizer, None)

    both_sec_sightings(release)  # before any text has arrived
    await release.finish()

    assert sec.urgent == [f"{FOLDER}/{FILING_INDEX}", f"{FOLDER}/{exhibit}"]
    [(text, _)] = summarizer.calls
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
    ) -> Response:
        response = await super().request(
            method, url, headers, give_up_after, urgent, allow_redirects=allow_redirects
        )
        if url.endswith(f"/{FILING_INDEX}"):
            return Response(url, 404, {}, b"", 0.01)
        return response


async def test_the_other_sec_source_tries_again_when_the_first_read_gets_nothing() -> None:
    sec, log = ListedLate(filing_routes()), io.StringIO()
    release = Release(uber(), FakePoster(), sec, Journal(log), None, None)

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
    release = Release(uber(), poster, fetcher(), Journal(log), summarizer, None)

    release.add(Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", IR_PAGE))
    release.add(EdgarFilings(1543151), Sighting("acc", "8-K 2.02", FILING, earnings=True))
    await release.finish()

    assert len(summarizer.calls) == 1
    assert summarizer.calls[0][1] is None  # no FMP key, no estimates
    # The text's arrival is logged once, for whichever source delivered it first
    assert [e["event"] for e in map(json.loads, log.getvalue().splitlines())].count("body") == 1


def broadcom_release() -> tuple[BroadcomNewsroom, Sighting]:
    newsroom = BroadcomNewsroom()
    newsroom.trial = True
    path = "/company/news/financial-releases/64900"
    title = "Broadcom Inc. Announces Fourth Quarter and Fiscal Year 2026 Financial Results"
    url = f"https://www.broadcom.com{path}"
    return newsroom, Sighting("64900", title, url, earnings=True, details={"path": path})


async def test_trial_sources_dont_post_unless_the_schedule_asks() -> None:
    posters: list[FakePoster] = []

    def new_poster() -> FakePoster:
        posters.append(FakePoster())
        return posters[-1]

    releases = Releases(new_poster, fetcher(), Journal(io.StringIO()), None, None)
    newsroom, results = broadcom_release()
    expected = datetime(2026, 12, 10, 16, 15, tzinfo=ET)
    on_trial = Target(ticker="AVGO", expected=expected, cik=1730168, trial=Trial())

    releases.on_new(on_trial, newsroom, results)
    await releases.finish()
    assert posters == []

    proven = on_trial.model_copy(update={"trial": Trial(post=True, broadcom_newsroom=True)})
    releases.on_new(proven, newsroom, results)
    await releases.finish()
    [poster] = posters
    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"Broadcom newsroom: <{results.url}>" in poster.calls[0][1]


async def test_the_same_release_from_both_copies_of_a_feed_posts_once(
    release_texts: dict[str, str],
) -> None:
    url = "https://www.globenewswire.com/news-release/2026/11/18/3400000/0/en/nvidia-results.html"
    release_texts[url] = "the release"
    poster, log = FakePoster(), io.StringIO()
    nvda = Target(ticker="NVDA", expected=datetime(2026, 11, 18, 16, 20, tzinfo=ET), cik=1045810)
    release = Release(nvda, poster, fetcher(), Journal(log), None, None)
    results = Sighting(url, "NVIDIA Announces Financial Results", url, earnings=True)

    release.add(RssFeed("globenewswire", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"]), results)
    release.add(RssFeed("globenewswire_busted", GLOBENEWSWIRE_EARNINGS, ["NVIDIA"], True), results)
    await release.finish()

    assert [kind for kind, _ in poster.calls] == ["send"]
    assert f"GlobeNewswire: <{url}>" in poster.calls[0][1]
    # The text is still fetched without a summarizer, to log when it arrives
    events = [e["event"] for e in map(json.loads, log.getvalue().splitlines())]
    assert events.count("body") == 1


async def test_an_alert_for_a_release_already_out_says_so() -> None:
    poster = FakePoster()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), None, None)
    index = FILING.rsplit("/", 1)[0] + "/0001543151-26-000040-index.htm"

    release.add(
        EdgarLatest(1543151),
        Sighting("0001543151-26-000040", "8-K 2.02", index, earnings=True, caught_up=True),
    )
    await release.finish()

    [(_, alert)] = poster.calls
    assert "-# Already out when the bot started watching. First seen" in alert


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
    release = Release(tonight, poster, files, Journal(io.StringIO()), None, None)
    release.attach_retry = 0.01

    release.add(Q4FinancialReports("investor.uber.com"), listed_pdf())
    await release.finish()

    assert files.pdf_requests == 3
    assert [kind for kind, _ in poster.calls] == ["send", "attach"]


async def test_a_missing_file_isnt_retried_after_the_window_closes() -> None:
    poster, files = FakePoster(), UploadedLater()
    release = Release(uber(), poster, files, Journal(io.StringIO()), None, None)  # Nov 4
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
    release = Release(uber(), poster, raw, Journal(io.StringIO()), None, None)

    release.add(source, sighting)
    release.add(source, sighting)
    await release.finish()

    assert raw.requests == []
    assert [kind for kind, _ in poster.calls] == ["send", "attach"]
