"""Company facts from SEC for tickers outside the catalog."""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from earnings_bot import issuers
from earnings_bot.calendar import Calendar
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.issuers import Issuers, profile, release_time, symbol, wire_names
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET
from earnings_bot.sources import PR_NEWSWIRE_EARNINGS, RssFeed
from tests.fakes import RoutedFetcher
from tests.sec_data import (
    LCID_CIK,
    LCID_ROUTE,
    LCID_SUBMISSIONS,
    SEC_TICKERS_ROUTE,
    sec_routes,
)

TODAY = date(2026, 11, 9)


def exactly(message: str) -> str:
    return f"^{re.escape(message)}$"


@pytest.mark.parametrize(
    ("typed", "sec"), [(" lcid ", "LCID"), ("brk.b", "BRK-B"), ("BF/B", "BF-B"), ("", "")]
)
def test_symbols_are_spelled_as_sec_lists_them(typed: str, sec: str) -> None:
    assert symbol(typed) == sec


@pytest.mark.parametrize(
    ("title", "ticker", "names"),
    [
        ("Lucid Group, Inc.", "LCID", ["Lucid Group", "NASDAQ: LCID", "NYSE: LCID"]),
        (
            "Rivian Automotive, Inc. / DE",
            "RIVN",
            ["Rivian Automotive", "NASDAQ: RIVN", "NYSE: RIVN"],
        ),
        ("The Coca-Cola Company", "KO", ["The Coca-Cola", "NASDAQ: KO", "NYSE: KO"]),
        ("Ford Motor Co", "F", ["Ford Motor", "NASDAQ: F", "NYSE: F"]),
        # One word could be anyone's news, so only the exchange and ticker count
        ("Block, Inc.", "XYZ", ["NASDAQ: XYZ", "NYSE: XYZ"]),
        (
            "BERKSHIRE HATHAWAY INC",
            "BRK-B",
            ["BERKSHIRE HATHAWAY", "NASDAQ: BRK-B", "NYSE: BRK-B", "NASDAQ: BRK.B", "NYSE: BRK.B"],
        ),
    ],
)
def test_wire_names_drop_the_legal_ending_and_add_the_exchange_tag(
    title: str, ticker: str, names: list[str]
) -> None:
    assert wire_names(title, ticker) == names


def _item(title: str, lead: str, guid: str) -> str:
    return (
        f"<item><title>{title}</title><link>https://www.prnewswire.com/{guid}</link>"
        f"<guid>{guid}</guid><description>{lead}</description></item>"
    )


def test_the_names_find_the_companys_release_and_not_a_lookalike() -> None:
    feed = (
        '<rss version="2.0"><channel>'
        + _item(
            "Lucid Announces Third Quarter 2026 Results",
            "NEWARK, Calif., Nov. 9, 2026 /PRNewswire/ -- Lucid (Nasdaq: LCID), maker of",
            "lucid",
        )
        + _item(
            "Acme Reports Third Quarter 2026 Results",
            "AUSTIN, Texas /PRNewswire/ -- Acme Corp (NYSE: ACME), the block storage leader",
            "acme",
        )
        + "</channel></rss>"
    )
    response = Response(PR_NEWSWIRE_EARNINGS, 200, {}, feed.encode(), 0.01)

    def seen(title: str, ticker: str) -> list[str]:
        source = RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, wire_names(title, ticker))
        return [sighting.key for sighting in source.parse(response)]

    # The headline says only "Lucid", so it's the ticker that finds it
    assert seen("Lucid Group, Inc.", "LCID") == ["lucid"]
    assert seen("Block, Inc.", "XYZ") == []


def _accepted(*stamps: str) -> list[datetime]:
    return [datetime.fromisoformat(stamp) for stamp in stamps]


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [
        # Lucid: three after the close and a morning item 2.02 that wasn't results
        (
            _accepted(
                "2026-10-05T20:06:06Z",
                "2026-08-04T20:07:05Z",
                "2026-07-02T13:01:58Z",
                "2026-05-05T20:07:16Z",
            ),
            "16:05",
        ),
        # Before the open, rounded down to the five minutes the release went out on
        (
            _accepted("2026-10-28T11:01:00Z", "2026-07-29T11:02:00Z", "2026-04-29T20:05:00Z"),
            "07:00",
        ),
        # A tie goes to the newest filing's session
        (_accepted("2026-10-28T20:10:00Z", "2026-07-29T11:02:00Z"), "16:10"),
        # Eastern time, after the clocks went back
        (_accepted("2026-11-05T21:06:00Z"), "16:05"),
        ([], None),
    ],
    ids=["lucid", "before-open", "tie", "standard-time", "none"],
)
def test_release_time_comes_from_when_results_8ks_were_accepted(
    accepted: list[datetime], expected: str | None
) -> None:
    assert release_time(accepted) == expected


def test_a_profile_watches_edgar_and_all_three_wires() -> None:
    lucid = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, TODAY)

    assert lucid.cik == LCID_CIK
    assert lucid.names == ["Lucid Group", "NASDAQ: LCID", "NYSE: LCID"]
    # Only results 8-Ks, not amendments or other items, and the newest four
    assert lucid.eightk == ["16:06", "16:07", "09:01", "16:07"]
    assert lucid.release_time == "16:05"
    assert lucid.fiscal_year_end == "1231"
    assert lucid.checked == TODAY

    target = to_target("LCID", datetime(2026, 11, 9, 16, 5, tzinfo=ET), 2026, 3, lucid)

    assert target.q4_host is None
    assert target.newswire == target.prnewswire == lucid.names
    assert [s.name for s in target.sources()] == [
        "edgar",
        "edgar_latest",
        "businesswire_busted",
        "prnewswire_busted",
        "prnewswire",
        "globenewswire",
        "globenewswire_busted",
    ]


@pytest.mark.parametrize("year_end", [None, "", "12-31"])
def test_an_unusable_fiscal_year_end_is_left_out(year_end: str | None) -> None:
    submissions = {**LCID_SUBMISSIONS, "fiscalYearEnd": year_end}
    assert profile("LCID", LCID_CIK, "Lucid", submissions, TODAY).fiscal_year_end is None


async def test_a_catalog_ticker_needs_no_lookup() -> None:
    fetcher = RoutedFetcher(sec_routes())

    assert await Issuers(fetcher).company("NVDA", TODAY) == known_companies()["NVDA"]
    assert fetcher.requests == []


async def test_other_tickers_come_from_sec_and_the_list_is_kept_for_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RoutedFetcher(sec_routes())
    lookup = Issuers(fetcher)

    lucid = await lookup.company("LCID", TODAY)

    assert lucid == profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, TODAY)
    assert [url for _, url, _ in fetcher.requests] == [
        issuers.SEC_TICKERS,
        f"https://{LCID_ROUTE}",
    ]
    with pytest.raises(ValueError, match=exactly("ZZZZ isn't on SEC's list of company tickers.")):
        await lookup.company("ZZZZ", TODAY)
    # Both answered from the list already loaded
    assert len(fetcher.requests) == 2

    monkeypatch.setattr(issuers, "TICKERS_REFRESH", 0)
    lookup = Issuers(fetcher)
    await lookup.company("LCID", TODAY)
    await lookup.company("LCID", TODAY)
    assert [url for _, url, _ in fetcher.requests].count(issuers.SEC_TICKERS) == 3


@pytest.mark.parametrize("failing", [SEC_TICKERS_ROUTE, LCID_ROUTE])
@pytest.mark.parametrize("answer", [(503, b"busy"), (503, b"{}"), (200, b"<html>not json</html>")])
async def test_a_failed_lookup_asks_again_next_time(
    failing: str, answer: tuple[int, bytes]
) -> None:
    routes = sec_routes()
    working = routes[failing]
    routes[failing] = answer
    lookup = Issuers(RoutedFetcher(routes))

    with pytest.raises(
        ValueError, match=exactly("SEC's company data didn't load. Try again in a minute.")
    ):
        await lookup.company("LCID", TODAY)

    routes[failing] = working
    assert (await lookup.company("LCID", TODAY)).cik == LCID_CIK


@pytest.mark.parametrize("typed", ["", "$$$", "TOOLONGTICKER", "-ABC"])
async def test_something_that_isnt_a_symbol_isnt_looked_up(typed: str) -> None:
    fetcher = RoutedFetcher(sec_routes())

    with pytest.raises(ValueError, match=exactly("Enter a ticker symbol, such as NVDA.")):
        await Issuers(fetcher).company(typed, TODAY)
    assert fetcher.requests == []


def test_single_word_publisher_matches_azentas_own_results(tmp_path: Path) -> None:
    company = profile("AZTA", 933974, "Azenta, Inc.", LCID_SUBMISSIONS, date(2026, 11, 10))
    target = to_target("AZTA", datetime(2026, 11, 10, 16, 30, tzinfo=ET), 2026, 4, company)
    title = "Azenta Reports Fourth Quarter and Full Year Results for Fiscal 2026"
    featured = ("Year-end revenue grew as announced in the release. " * 5) + "(Nasdaq: AZTA)"
    with_publisher = _item(title, featured, "azenta").replace(
        "</item>", "<dc:contributor>Azenta, Inc.</dc:contributor></item>"
    )
    mismatched_publisher = _item(title, "No exchange tag", "wrong-publisher").replace(
        "</item>", "<dc:contributor>Another Company</dc:contributor></item>"
    )
    feed = (
        '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        f"<link>{PR_NEWSWIRE_EARNINGS}</link>"
        + with_publisher
        + _item(title, "No exchange tag", "headline")
        + mismatched_publisher
        + _item("Other Company Reports Fourth Quarter Results", "Its customer Azenta", "other")
        + "</channel></rss>"
    )
    response = Response(PR_NEWSWIRE_EARNINGS, 200, {}, feed.encode(), 0.01)
    assert target.prnewswire == ["NASDAQ: AZTA", "NYSE: AZTA"]
    # Existing /track rows and matching old TOML files pick up the identity on the next scan.
    old = target.model_copy(update={"wire_publisher": None})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("AZTA", company)
        calendar.set_target(old, "Issuer confirmed release time")
        planned = calendar.plan({old.release_key: old})[old.release_key]
    for source in planned.sources():
        if source.name in ("prnewswire", "prnewswire_busted"):
            assert [item.key for item in source.parse(response)] == ["azenta", "headline"]
