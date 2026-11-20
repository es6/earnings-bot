"""Independent newsroom signals must not replace the complete earnings report."""

import io
import tomllib
from pathlib import Path

import pytest

from first_print.known import target_toml
from first_print.pipeline import Release, describe_source
from first_print.recorder import Journal, Target, poll
from first_print.sources import Source
from tests.fakes import FakePoster, FakeSummarizer, RoutedFetcher
from tests.test_pipeline import IR_PAGE, ir_sighting, uber
from tests.test_pipeline import release_texts as release_texts

LISTING = "https://www.weride.ai/press"
ARTICLE = "https://www.weride.ai/posts/lndnhff3kzyl09yb2yzoapmf"
TITLE = (
    "WeRide's Q3 Revenue Surges 88% to a Record High as Overseas Revenue Jumps 172% "
    "and Robotaxi Revenue Tops RMB 100 Million"
)
BODY = (Path(__file__).parent / "fixtures/wrd-corporate-20261120.html").read_bytes()


def target(listing: str = LISTING, name: str = "WeRide") -> Target:
    return Target.model_validate(
        uber().model_dump()
        | {
            "period": "FY2026Q3",
            "newsrooms": [{"listing": listing, "names": [name]}],
        }
    )


def source(listing: str = LISTING, name: str = "WeRide") -> Source:
    sources = [s for s in target(listing, name).sources() if s.name.startswith("newsroom:")]
    assert len(sources) == 1, "The independent route must be in the actual watch plan"
    return sources[0]


def routes(body: bytes = BODY, title: str = TITLE) -> RoutedFetcher:
    return RoutedFetcher(
        {
            LISTING: (200, f'<a href="{ARTICLE}">{title}</a>'.encode()),
            ARTICLE: (200, body),
        }
    )


async def test_real_yearless_revenue_headline_is_verified_against_the_article() -> None:
    room, fetcher = source(), routes()
    _, [item] = await poll(room, fetcher)

    assert item.url == ARTICLE and item.earnings
    assert item.details["period"] == "FY2026Q3" and item.details["content_kind"] == "highlights"
    assert item.body == BODY
    assert "Official newsroom" in describe_source(room)
    assert [url for _, url, _ in fetcher.requests] == [LISTING, ARTICLE]


@pytest.mark.parametrize(
    "replacement",
    [
        (b"third quarter of 2026", b"third quarter of 2025"),
        (b"third quarter of 2026", b"second quarter of 2026"),
        (b"WeRide", b"OtherCo"),
        (b"today announced its financial results", b"will report its financial results"),
    ],
)
async def test_wrong_period_issuer_and_results_notices_cannot_trigger_a_signal(
    replacement: tuple[bytes, bytes],
) -> None:
    assert not (await poll(source(), routes(BODY.replace(*replacement))))[1]


async def test_an_older_article_cannot_hide_the_current_one_and_same_url_can_be_updated() -> None:
    room = source()
    old = ARTICLE + "-older"
    fetcher = RoutedFetcher(
        {
            LISTING: (200, f'<a href="{old}">{TITLE}</a><a href="{ARTICLE}">{TITLE}</a>'.encode()),
            old: (200, BODY.replace(b"third quarter of 2026", b"third quarter of 2025")),
            ARTICLE: (200, BODY),
        }
    )
    assert (await poll(room, fetcher))[1][0].url == ARTICLE
    assert (await poll(room, fetcher))[1][0].url == ARTICLE
    fetcher.routes[old] = (200, BODY)
    assert (await poll(room, fetcher))[1][0].url == old


@pytest.mark.parametrize("status", [403, 429])
async def test_article_refusal_stops_the_poll_before_any_other_candidate(status: int) -> None:
    room = source()
    fetcher = routes()
    fetcher.routes[ARTICLE] = status, b"refused"
    with pytest.raises(Exception, match=str(status)):
        await poll(room, fetcher)
    assert len(fetcher.requests) == 2 and not fetcher.chrome_calls


async def test_other_issuers_work_and_official_host_boundary_is_enforced() -> None:
    listing, article = "https://mobility.example/newsroom", "https://mobility.example/news/q3"
    title = TITLE.replace("WeRide", "Example Mobility")
    body = BODY.replace(b"WeRide", b"Example Mobility")
    fetcher = RoutedFetcher(
        {
            listing: (
                200,
                (
                    f'<a href="https://elsewhere.example/q3">{title}</a>'
                    f'<a href="{article}">{title}</a>'
                ).encode(),
            ),
            article: (200, body),
        }
    )
    _, [item] = await poll(source(listing, "Example Mobility"), fetcher)
    assert item.url == article and not any("elsewhere.example" in u for _, u, _ in fetcher.requests)


def test_independent_routes_survive_schedule_round_trip_and_keep_existing_routes() -> None:
    planned = target()
    decoded = Target.model_validate(tomllib.loads(target_toml(planned))["target"][0])
    assert any(s.url() == LISTING for s in decoded.sources())
    assert {s.url() for s in uber().sources()} <= {s.url() for s in decoded.sources()}


async def test_highlights_alert_immediately_but_only_full_report_feeds_the_summary(
    release_texts: dict[str, str],
) -> None:
    room = source()
    _, [item] = await poll(room, routes())
    poster, writer = FakePoster(), FakeSummarizer()
    release = Release(uber(), poster, routes(), Journal(io.StringIO()), writer)
    release.add(room, item)
    await release.finish()
    assert poster.calls and ARTICLE in poster.calls[0][1] and not writer.calls
    assert not release.has_text
    release_texts[IR_PAGE] = "The full financial results and tables"
    release.add(*ir_sighting())
    await release.finish()
    assert writer.calls == [release_texts[IR_PAGE]]
