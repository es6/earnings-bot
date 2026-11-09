import io
from datetime import datetime

import pytest
from pydantic import ValidationError

from earnings_bot.browser import BrowserUnavailable
from earnings_bot.browser_sources import BrowserPDF, BrowserRelease, IRTrial, TeslaDeck
from earnings_bot.fetch import Response
from earnings_bot.ir_research import quarter_headline
from earnings_bot.recorder import ET, Journal, PollError, Target, Watch, poll, take
from earnings_bot.sources import Sighting
from earnings_bot.text import release_text
from tests.fakes import RoutedFetcher

FICO = "https://investors.fico.com/news-releases/"
ARTICLE = FICO + "news-release-details/earnings"
TITLE = "FICO Announces Earnings of $8.12 per Share for Fourth Quarter Fiscal 2026"
PATTERN = r"FICO Announces Earnings of .+ for Fourth Quarter Fiscal 2026"
MC = "https://corporate.mcdonalds.com/"
PDF = MC + "content/dam/quarterly-release.pdf"
TESLA = "https://assets-ir.tesla.com/tesla-contents/IR/TSLA-Q3-2026-Update.pdf"
LUCID = "https://ir.lucidmotors.com/press-releases"
# Lucid's results headlines end in "Results", not "Financial Results". This is the pattern in
# its Q3 2026 schedule.
LUCID_Q3 = (
    r"^(?!.*\b(?:webcast|conference call|to host|to announce|will report|date|schedule)\b)"
    r"(?=.*\b(?:Q3|third quarter)\b)(?=.*\b2026\b)(?=.*\bresults\b).*"
)


class BrowserFake(RoutedFetcher):
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        super().__init__({})
        self._responses = iter(responses)
        self.browser_calls: list[tuple[str, str, bool]] = []

    async def browser_request(
        self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
    ) -> Response:
        self.browser_calls.append((url, warmup, urgent))
        status, body = next(self._responses)
        return Response(url, status, {}, body, 0.015)


@pytest.mark.parametrize(
    ("headline", "verify_text"),
    [(PATTERN, False), (quarter_headline(2026, 4), True)],
    ids=["configured", "automatic"],
)
async def test_only_scheduled_issuer_article_is_fetched_and_reused(
    headline: str, verify_text: bool
) -> None:
    listing = (
        '<a href="https://example.org/not-issuer">' + TITLE + "</a>"
        '<a href="/announced-date">FICO Announces Date for Fourth Quarter 2026 Results</a>'
        '<a href="/prior">FICO Announces Earnings of $1.01 for Third Quarter Fiscal 2026</a>'
        f'<a href="{ARTICLE}#results"><span>{TITLE}</span></a>'
    ).encode()
    body = f"<h1>{TITLE}</h1><p>{'Revenue and earnings per share. ' * 120}</p>".encode()
    fetcher = BrowserFake([(200, listing), (200, body)])
    source = BrowserRelease(FICO, headline, verify_text=verify_text)

    _, items = await poll(source, fetcher)

    assert [item.url for item in items] == [ARTICLE]
    assert fetcher.browser_calls == [(FICO, FICO, False), (ARTICLE, FICO, True)]
    assert "Revenue and earnings" in (await release_text(fetcher, source, items[0]) or "")
    assert fetcher.requests == []


async def test_listing_only_or_stub_does_not_signal_a_complete_release() -> None:
    listing = f'<a href="{ARTICLE}">{TITLE}</a>'.encode()
    fetcher = BrowserFake([(200, listing), (200, b"<html>Try again later</html>")])
    source = BrowserRelease(FICO, PATTERN)

    _, items = await poll(source, fetcher)

    assert items == []


async def test_pdf_ignores_missing_files_and_html_and_reuses_valid_bytes() -> None:
    valid = b"%PDF-1.7\n"
    fetcher = BrowserFake([(404, b""), (200, b"<html>Missing</html>"), (200, valid)])
    source = BrowserPDF(PDF, MC)

    assert (await poll(source, fetcher))[1] == []
    assert (await poll(source, fetcher))[1] == []
    _, items = await poll(source, fetcher)

    assert len(items) == 1
    assert items[0].url == PDF
    assert items[0].body == valid
    assert all(url.startswith(PDF + "?_=") for url, _, _ in fetcher.browser_calls)
    assert fetcher.requests == []


def test_existing_trial_pdf_is_caught_up_without_claiming_a_live_detection() -> None:
    source = BrowserPDF(PDF, MC)
    watch = Watch("MCD", source)
    journal = Journal(io.StringIO())
    reported: list[Sighting] = []
    response = Response(PDF, 200, {}, b"%PDF-1.7\n", 0.04)

    take(watch, journal.now(), response, journal, lambda _, item: reported.append(item))

    assert len(reported) == 1 and reported[0].caught_up
    assert watch.done
    assert watch.first is None


async def test_tesla_scope_preserves_a_named_identity_and_rejects_other_urls() -> None:
    fetcher = RoutedFetcher({TESLA: (200, b"%PDF-1.7\n")})
    _, items = await poll(TeslaDeck(TESLA), fetcher)

    assert len(items) == 1
    method, _, headers = fetcher.requests[0]
    assert method == "GET"
    assert fetcher.no_redirect == [fetcher.requests[0][1]]
    assert headers == {"User-Agent": "earnings-bot/0.1 (public earnings release monitor)"}
    with pytest.raises(ValueError, match="official quarterly update"):
        TeslaDeck("https://other.example/deck.pdf")


async def test_browser_start_failure_stops_only_the_trial_source() -> None:
    class Unavailable(BrowserFake):
        async def browser_request(
            self, url: str, warmup: str, give_up_after: float, *, urgent: bool = False
        ) -> Response:
            raise BrowserUnavailable("desktop not available")

    with pytest.raises(PollError, match="desktop Chrome unavailable") as error:
        await poll(BrowserRelease(FICO, PATTERN), Unavailable([]))

    assert error.value.fatal


def test_schedule_requires_specific_trial_configuration() -> None:
    values = {"ticker": "FICO", "cik": 814547, "expected": datetime(2026, 11, 4, 16, 15, tzinfo=ET)}
    target = Target.model_validate(
        {**values, "trial": {"ir": {"listing": FICO, "headline": PATTERN}}}
    )

    assert not target.trial.post
    assert [source.name for source in target.sources() if source.trial] == ["browser-ir"]
    assert all(not source.trial for source in Target.model_validate(values).sources())
    with pytest.raises(ValidationError, match="scheduled quarter"):
        IRTrial(listing=FICO)
    with pytest.raises(ValidationError, match="warmup page's host"):
        IRTrial(pdfs=[PDF], warmup=FICO)


async def test_lucids_listing_trial_picks_only_the_quarters_results() -> None:
    details = "https://ir.lucidmotors.com/news-releases/news-release-details/"
    listing = (
        f'<a href="{details}q3-deliveries">Lucid Announces Q3 Production and Deliveries, '
        "Sets Date for Third Quarter 2026 Earnings</a>"
        f'<a href="{details}q2">Lucid Announces Operational Reset and Second Quarter 2026 '
        "Results</a>"
        f'<a href="{details}q3-webcast">Lucid to Host Third Quarter 2026 Results Webcast</a>'
        f'<a href="{details}q3">Lucid Announces Third Quarter 2026 Results</a>'
    ).encode()
    article = (
        "<h1>Lucid Announces Third Quarter 2026 Results</h1>"
        f"<p>{'Revenue of $395 million and deliveries of 3,806 vehicles. ' * 40}</p>"
    ).encode()
    fetcher = BrowserFake([(200, listing), (200, article)])
    # The host has to be on the browser list for the schedule to load at all
    [source] = IRTrial(listing=LUCID, headline=LUCID_Q3).sources()

    _, items = await poll(source, fetcher)

    assert [item.url for item in items] == [details + "q3"]
    # Only the matching article is opened, ahead of other requests to the host
    assert fetcher.browser_calls == [(LUCID, LUCID, False), (details + "q3", LUCID, True)]
