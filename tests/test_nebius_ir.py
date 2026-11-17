"""Replay the issuer layout and 6-K report that NBIS exposed in /track."""

import io
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from first_print import date_discovery, ir_research
from first_print.browser_sources import BrowserRelease
from first_print.calendar import Calendar, Tracked
from first_print.date_discovery import DateDiscovery
from first_print.delivery import DeliveryStore
from first_print.issuers import profile
from first_print.known import KnownCompany
from first_print.pipeline import Releases
from first_print.recorder import ET, Journal, Target, poll
from first_print.source_audit import audit_sources
from tests.builders import sec_filing_detail
from tests.fakes import BrowserFake, FakePoster, RoutedFetcher

CIK = 1513845
NOW = datetime(2026, 11, 10, 2, tzinfo=ET)
HOME = "https://nebius.com/"
NEWSROOM = HOME + "newsroom"
PRIOR_URL = NEWSROOM + "/second-quarter-results"
NOTICE_URL = NEWSROOM + "/announces-date"
TITLE = "Nebius reports second quarter 2026 financial results"
NOTICE_TITLE = "Nebius announces date of third quarter 2026 results and conference call"
PDF = "https://assets.nebius.com/files/PR.pdf?cache-buster=123"
RESULTS = (
    f"<h1>{TITLE}</h1><p>Read the disclosure for the key financials.</p>"
    "<p>Nebius Group today announced its unaudited financial results for the quarter.</p>"
    + "<p>Cloud platform operating information and company background.</p>" * 70
    + f'<a href="{PDF}">{TITLE} (opens in a new tab)</a>'
)
NOTICE = (
    f"<h1>{NOTICE_TITLE}</h1>"
    "<p>Nebius will release its third quarter 2026 financial results on Thursday, "
    "November 12, 2026, before market open.</p>"
    "<p>It will also hold a conference call at 8:00 a.m. Eastern Time on the same day.</p>"
)


def card(url: str, title: str) -> str:
    return (
        f'<a href="{url}"><div>Press release</div><h2><span>{title}</span></h2>'
        "<div>October 30, 2026</div></a>"
    )


@dataclass
class Nebius:
    fetcher: RoutedFetcher
    company: KnownCompany
    tmp_path: Path

    async def research(self, plan: Target | None = None, evidence: str = "") -> Tracked:
        """Researches NBIS once from a fresh calendar, starting from `plan` if given."""
        with DeliveryStore(self.tmp_path / "state.db", 7) as store:
            calendar = Calendar(store)
            row, _ = calendar.add("NBIS", self.company)
            if plan:
                calendar.set_target(plan, evidence)
                [row] = calendar.entries()
            dates = DateDiscovery(
                calendar, self.fetcher, lambda: NOW, lambda: False, lambda _: None
            )
            await dates.run_once(row)
            [saved] = calendar.entries()
        return saved


@pytest.fixture
def nebius(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Nebius:
    accession = "0001104659-26-094568"
    folder = f"/Archives/edgar/data/{CIK}/{accession.replace('-', '')}"
    submissions = {
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["6-K"],
                "items": [""],
                "acceptanceDateTime": ["2026-08-12T12:55:00Z"],
                "primaryDocument": ["cover.htm"],
            }
        },
    }
    exhibit = (
        f"<h1>{TITLE}</h1><p>See https://nebius.com/investor-hub.</p>"
        "<table><tr><td>In USD millions</td><td>2025</td><td>2026</td></tr>"
        "<tr><td>Revenues</td><td>105.1</td><td>582.3</td></tr>"
        "<tr><td>Net loss</td><td>21.0</td><td>236.2</td></tr></table>"
        + "<p>Operating notes and consolidated financial statements.</p>"
        * 80
    )
    archive = json.dumps(
        {"@type": "ItemList", "itemListElement": [{"name": TITLE, "url": PRIOR_URL}]}
    )
    newsroom = (
        card(NOTICE_URL, NOTICE_TITLE)
        + "".join(card(f"{NEWSROOM}/other-{n}", "Product benchmark results") for n in range(3))
        + f'<script type="application/ld+json">{archive}</script>'
    )
    fetcher = RoutedFetcher(
        {
            f"CIK{CIK:010d}.json": (200, json.dumps(submissions).encode()),
            f"{folder}/{accession}-index.htm": (
                200,
                sec_filing_detail(f"{folder}/cover.htm", "ex99.htm"),
            ),
            f"{folder}/ex99.htm": (200, exhibit.encode()),
            NOTICE_URL: (200, NOTICE.encode()),
            PRIOR_URL: (200, RESULTS.encode()),
            PDF: (200, b"%PDF-1.7"),
            NEWSROOM: (200, newsroom.encode()),
            "/rss": (404, b""),
            "/feed/PressRelease.svc": (404, b""),
            HOME: (200, b'<a href="/newsroom">Newsroom</a>'),
        }
    )
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    monkeypatch.setattr(ir_research, "chrome_available", lambda: True)
    company = profile("NBIS", CIK, "Nebius Group N.V.", submissions, NOW.date())
    return Nebius(fetcher, company, tmp_path)


async def test_nebius_tracks_from_the_issuer_notice_and_verifies_the_previous_pdf(
    nebius: Nebius,
) -> None:
    saved = await nebius.research()

    assert saved.company.ir_release == PRIOR_URL
    assert saved.company.sixk == ["08:55"]
    assert saved.target and saved.target.ir
    assert saved.target.ir.listing == NEWSROOM
    assert saved.target.ir.http_first
    assert saved.target.period == "FY2026Q3"
    assert saved.target.window[0] == datetime(2026, 11, 12, 6, 0, tzinfo=ET)
    assert saved.evidence and NOTICE_URL in saved.evidence
    assert PDF in nebius.fetcher.no_redirect


async def test_a_saved_plan_without_direct_http_is_verified_again_and_gains_it(
    nebius: Nebius,
) -> None:
    first = await nebius.research()
    assert first.target and first.target.ir and first.evidence
    old_source = first.target.ir.model_copy(update={"http_first": False})

    updated = await nebius.research(
        first.target.model_copy(update={"ir": old_source}), first.evidence
    )

    assert updated.target and updated.target.ir and updated.target.ir.http_first


async def test_a_plan_already_polled_by_direct_http_is_not_verified_again(
    nebius: Nebius,
) -> None:
    first = await nebius.research()
    assert first.target and first.target.ir and first.target.ir.http_first and first.evidence
    nebius.fetcher.requests.clear()

    with DeliveryStore(nebius.tmp_path / "state2.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("NBIS", nebius.company)
        dates = DateDiscovery(calendar, nebius.fetcher, lambda: NOW, lambda: False, lambda _: None)
        kept = await dates._issuer_routes(row, first.target, first.evidence)

    assert kept == first.target
    assert nebius.fetcher.requests == []


async def test_the_speed_audit_finds_the_previous_6k_release_day(
    nebius: Nebius, tmp_path: Path
) -> None:
    speed = await audit_sources(nebius.fetcher, tmp_path, "NBIS", nebius.company, NOW, None)
    assert speed.day == datetime(2026, 8, 12).date()


async def test_issuer_pdf_wrapper_alerts_and_attaches_without_using_boilerplate_as_results() -> (
    None
):
    title = TITLE.replace("second", "third")
    url = PRIOR_URL.replace("second", "third")
    listing = card(NOTICE_URL, NOTICE_TITLE) + card(url, title)
    article = RESULTS.replace(TITLE, title)
    fake = BrowserFake([(200, listing.encode()), (200, article.encode())])
    fake.routes[PDF] = (200, b"%PDF-1.7")
    target = Target(
        ticker="NBIS",
        cik=CIK,
        period="FY2026Q3",
        expected=NOW,
    )
    source = BrowserRelease(NEWSROOM, ir_research.quarter_headline(2026, 3), verify_text=True)
    _, items = await poll(source, fake)
    assert [item.url for item in items] == [url]
    assert fake.browser_calls == [(NEWSROOM, NEWSROOM, False), (url, NEWSROOM, True)]

    poster, output = FakePoster(), io.StringIO()
    releases = Releases(lambda: poster, fake, Journal(output), None)
    releases.on_new(target, source, items[0], measure=True)
    await releases.finish()

    assert [kind for kind, _ in poster.calls] == ["send", "edit", "attach"]
    assert poster.calls[-1] == ("attach", "PR.pdf")
    assert fake.urgent == [PDF]
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert not any(event["event"] == "body" for event in events)
    assert [event["url"] for event in events if event["event"] == "source_body"] == [PDF]


@pytest.mark.parametrize(
    "article",
    [
        RESULTS.replace("assets.nebius.com", "assets.nebius.com.example.org"),
        RESULTS.replace("today announced its unaudited financial results", "will report results"),
    ],
    ids=["unrelated-pdf-host", "no-results-announcement"],
)
async def test_a_pdf_link_alone_does_not_publish_an_earnings_alert(article: str) -> None:
    fake = BrowserFake([(200, card(PRIOR_URL, TITLE).encode()), (200, article.encode())])
    source = BrowserRelease(NEWSROOM, ir_research.quarter_headline(2026, 2), verify_text=True)
    assert (await poll(source, fake))[1] == []
