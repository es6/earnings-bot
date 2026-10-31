import io
import json
from datetime import UTC, datetime

import pytest

from earnings_bot import pipeline
from earnings_bot.estimates import Estimates
from earnings_bot.pipeline import Poster, Release, describe_source
from earnings_bot.recorder import ET, Journal, Target
from earnings_bot.sources import (
    PR_NEWSWIRE_EARNINGS,
    EdgarFilings,
    GuessedFile,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
)
from earnings_bot.summary import Summarizer, Summary
from tests.fakes import RoutedFetcher

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
    for step in ("posted", "attached", "summary"):
        assert 0 <= events[step]["live_seconds"] < 5


async def test_only_one_summary_when_several_sources_have_the_text(
    release_texts: dict[str, str],
) -> None:
    release_texts[IR_PAGE] = release_texts[FILING] = "the release"
    poster, summarizer = FakePoster(), FakeSummarizer()
    release = Release(uber(), poster, fetcher(), Journal(io.StringIO()), summarizer, None)

    release.add(Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", IR_PAGE))
    release.add(EdgarFilings(1543151), Sighting("acc", "8-K 2.02", FILING, earnings=True))
    await release.finish()

    assert len(summarizer.calls) == 1
    assert summarizer.calls[0][1] is None  # no FMP key, no estimates
