import io
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from earnings_bot.http_policy import retry_after
from earnings_bot.publication_trials import run_trials
from earnings_bot.recorder import Journal, PollError, fetch
from earnings_bot.trial_matching import candidates
from tests.test_publication_trials import QueuedFetcher, events, plan


@pytest.mark.parametrize("status", [429, 503])
async def test_recorder_honors_http_date_retry_after(status: int) -> None:
    from earnings_bot.fetch import Response

    class Source:
        timeout = 3

        async def fetch(self, fetcher: object) -> Response:
            return Response(
                "https://example.com",
                status,
                {
                    "retry-after": format_datetime(
                        datetime.now(UTC) + timedelta(hours=1), usegmt=True
                    )
                },
                b"",
                0,
            )

    with pytest.raises(PollError) as refused:
        await fetch(Source(), None)  # type: ignore[arg-type]
    assert 3595 <= refused.value.pause <= 3600


def test_retry_dates_are_utc_past_dates_are_zero_and_bad_values_fall_back() -> None:
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    assert retry_after("Sun, 15 Nov 2026 13:00:00 GMT", now) == 3600
    assert retry_after("Sun, 15 Nov 2026 11:00:00 GMT", now) == 0
    assert retry_after(" 120 ", now) == 120
    assert retry_after("nonsense", now) == 30


@pytest.mark.parametrize(
    "markup",
    [
        (
            "<rss><channel><item><title>Example Corp Q2 results</title><link>/old</link></item>"
            "<item><title>OtherCo third quarter results</title><link>/other</link></item>"
            "</channel></rss>"
        ),
        (
            '<a href="/old">Example Corp Q2 results</a>'
            '<a href="/other">OtherCo third quarter results</a>'
        ),
        json.dumps(
            {
                "items": [
                    {"title": "Example Corp Q2 results", "url": "/old"},
                    {"title": "OtherCo third quarter results", "url": "/other"},
                ]
            }
        ),
    ],
)
async def test_trial_never_stitches_a_candidate_from_different_entries(markup: str) -> None:
    fetcher = QueuedFetcher({"example.com": [markup.encode()]})
    log = io.StringIO()
    config = plan(max_requests=2)
    for route in config.routes:
        route.kind = "listing"
    with sqlite3.connect(":memory:") as db:
        await run_trials(config, fetcher, Journal(log), db)
    assert not any(e["candidate_visible"] for e in events(log, "publication_trial"))


def test_feed_and_html_candidates_keep_the_matching_entry_identity() -> None:
    patterns = [re.compile(p, re.IGNORECASE) for p in ("Example Corp", "third quarter", "results")]
    for markup in [
        (
            "<rss><channel><item><title>Example Corp third quarter results</title>"
            "<link>/q3</link></item></channel></rss>"
        ),
        '<a href="/q3">Example Corp <b>third quarter</b> results</a>',
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            '<title>Example Corp third quarter results</title><link href="/q3"/></entry></feed>'
        ),
    ]:
        entries = candidates(markup.encode(), "https://example.com/news", patterns)
        assert len(entries) == 1 and entries[0]["id"] == "https://example.com/q3"


def test_hidden_script_text_and_nonweb_links_cannot_identify_a_release() -> None:
    patterns = [re.compile(p, re.IGNORECASE) for p in ("Example Corp", "third quarter", "results")]
    body = (
        '<a href="/old"><script>Example Corp third quarter results</script>Older news</a>'
        '<a href="javascript:alert(1)">Example Corp third quarter results</a>'
    )
    assert candidates(body.encode(), "https://example.com/news", patterns) == []


async def test_trial_positive_records_the_matching_link_not_unrelated_page_links() -> None:
    page = (
        '<a href="/other">OtherCo third quarter results</a>'
        '<a href="/q3">Example Corp third quarter results</a>'
    )
    fetcher = QueuedFetcher({"example.com": [page.encode()]})
    log = io.StringIO()
    config = plan(max_requests=2)
    for route in config.routes:
        route.kind = "listing"
    with sqlite3.connect(":memory:") as db:
        await run_trials(config, fetcher, Journal(log), db)
    for event in events(log, "publication_trial"):
        assert event["candidate_scope"] == "entry"
        assert event["matched_entries"] == [
            {"id": "https://ir.example.com/q3", "title": "Example Corp third quarter results"}
        ]
