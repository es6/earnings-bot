import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from earnings_bot.browser_sources import IRSource, QuarterlyTable
from earnings_bot.calendar import Calendar, manual_target, upcoming
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.ir_research import IR_LISTINGS, quarter_headline
from earnings_bot.issuers import profile
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET, Target, Trial
from earnings_bot.sources import PR_NEWSWIRE_EARNINGS
from tests.builders import AMAT
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def test_subscription_and_override_survive_restart_and_preserve_file_trials(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        row, created = calendar.add("NVDA")
        assert created and row.target is None
        assert calendar.add("NVDA") == (row, False)
        target = manual_target("NVDA", "2026-11-12T16:20", "FY2027Q3", NOW)
        calendar.set_target(target, "Manual override")

    file = to_target("NVDA", datetime(2026, 11, 13, 16, 20, tzinfo=ET), 2027, 3)
    file.trial = Trial(globenewswire=["NVIDIA"])
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        targets = calendar.plan({file.release_key: file})
        [effective] = targets.values()
        assert effective.expected == target.expected
        assert effective.trial == file.trial
        assert "Manual override" in upcoming(calendar.entries(), targets, NOW)[0]
        assert calendar.remove(row.cik)
        assert calendar.plan({file.release_key: file}) == {file.release_key: file}


def test_tracking_and_untracking_can_be_repeated(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        for _ in range(6):
            row, added = calendar.add("NVDA")
            assert added
            calendar.save_review(row.cik, NOW, "{}")
            assert calendar.remove(row.cik)
            assert not calendar.remove(row.cik)
            assert calendar.entries() == []
            assert calendar.review(row.cik) == (None, None)


@pytest.mark.parametrize(
    ("when", "period", "message"),
    [
        (
            "2026-11-17T16:20",
            "FY2027Q0",
            "Use an ISO date and time like 2026-11-17T16:20 and a period like FY2027Q3.",
        ),
        ("2026-11-01T16:20", "FY2027Q3", "Choose a future earnings release time."),
    ],
    ids=["bad-period", "past-time"],
)
def test_override_requires_a_valid_fiscal_period_and_future_time(
    when: str, period: str, message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        manual_target("NVDA", when, period, NOW)


def test_auto_browser_source_preserves_existing_schedule_choices(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA")
        target = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3)
        target.ir = IRSource(
            listing="https://investor.nvidia.com/news/", headline="Q3", verify_text=True
        )
        calendar.set_target(target, "Issuer notice")
        file = target.model_copy(update={"ir": None, "trial": Trial(globenewswire=["NVIDIA"])})
        [effective] = calendar.plan({file.release_key: file}).values()
        assert effective.ir == target.ir
        assert effective.trial.globenewswire == ["NVIDIA"]
        file.trial = Trial.model_validate({"post": True, "globenewswire": ["NVIDIA"]})
        [effective] = calendar.plan({file.release_key: file}).values()
        assert effective.ir == target.ir
        assert effective.trial.globenewswire == ["NVIDIA"]
        assert "post" not in effective.trial.model_dump()


@pytest.mark.parametrize("kind", ["automatic", "manual-pattern", "custom-listing", "unverified"])
def test_only_saved_automatic_browser_headline_is_repaired_after_restart(
    tmp_path: Path, kind: str
) -> None:
    db = tmp_path / "state.db"
    target = to_target("CRWD", datetime(2026, 12, 1, 16, 5, tzinfo=ET), 2027, 3)
    headline = quarter_headline(2027, 3)
    target.ir = IRSource(
        listing=IR_LISTINGS["CRWD"], headline=headline.removesuffix(".*"), verify_text=True
    )
    if kind == "manual-pattern":
        target.ir.headline = "CrowdStrike custom results.*"
    elif kind == "custom-listing":
        target.ir.listing = "https://ir.example.com/news"
    elif kind == "unverified":
        target.ir.verify_text = False
    expected = headline if kind == "automatic" else target.ir.headline
    with DeliveryStore(db, 7) as store:
        calendar = Calendar(store)
        calendar.add("CRWD")
        calendar.set_target(target, "Issuer notice")
    with DeliveryStore(db, 7) as store:
        [effective] = Calendar(store).plan({}).values()
        assert effective.ir and effective.ir.headline == expected


def test_a_calendar_saved_before_profiles_is_upgraded_in_place(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.db.executescript("""
            CREATE TABLE tracked (
                channel INTEGER NOT NULL,
                cik INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                target TEXT,
                evidence TEXT,
                PRIMARY KEY (channel, cik)
            );
            INSERT INTO tracked (channel, cik, ticker) VALUES (7, 1045810, 'NVDA');
        """)

        calendar = Calendar(store)

        [nvda] = calendar.entries()
        assert nvda.company == known_companies()["NVDA"]
        lucid = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, date(2026, 11, 9))
        calendar.add("LCID", lucid)
        assert [row.company for row in Calendar(store).entries()] == [lucid, nvda.company]


def test_a_company_the_catalog_dropped_is_still_watched_on_edgar(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.db.execute(
            "CREATE TABLE tracked (channel INTEGER NOT NULL, cik INTEGER NOT NULL, "
            "ticker TEXT NOT NULL, target TEXT, evidence TEXT, profile TEXT, "
            "PRIMARY KEY (channel, cik))"
        )
        store.db.execute("INSERT INTO tracked (channel, cik, ticker) VALUES (7, 99, 'GONE')")

        [row] = Calendar(store).entries()

    target = to_target("GONE", datetime(2027, 1, 5, 16, tzinfo=ET), 2026, 4, row.company)
    assert [source.name for source in target.sources()] == ["edgar", "edgar_latest"]
    assert target.cik == 99


def test_the_calendar_follows_the_bot_to_a_new_channel(tmp_path: Path) -> None:
    # HQ Nest's #market-watch had the calendar; the bot now posts to #bot-testing
    db = tmp_path / "state.db"
    with DeliveryStore(db, 1374740009533440074) as store:
        calendar = Calendar(store)
        calendar.add("NVDA")
        calendar.add("AAPL")
        calendar.save_review(calendar.entries()[0].cik, NOW, "{}")
        # A smaller one left from an older server's channel stays put
        store.db.execute("INSERT INTO tracked (channel, cik, ticker) VALUES (99, 354950, 'HD')")
        store.db.commit()

    with DeliveryStore(db, 1489330380124328027) as store:
        calendar = Calendar(store)

        assert calendar.moved_from == 1374740009533440074
        assert [row.ticker for row in calendar.entries()] == ["AAPL", "NVDA"]
        assert calendar.review(calendar.entries()[0].cik)[1] == "{}"
    with DeliveryStore(db, 1489330380124328027) as store:
        assert Calendar(store).moved_from is None  # once
    with DeliveryStore(db, 99) as store:
        assert [row.ticker for row in Calendar(store).entries()] == ["HD"]


def test_legacy_schedule_keeps_its_key_and_watches_the_researched_table(tmp_path: Path) -> None:
    target = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3)
    results = "https://investor.nvidia.com/financial-results"
    target.ir = IRSource(results_table=results, table_rendered=True, cache_bust_urls=[results])
    manual = IRSource(listing="https://investor.nvidia.com/news/", headline="Q3")
    file = target.model_copy(update={"period": None, "ir": manual})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA")
        calendar.set_target(target, "Issuer notice")

        effective = calendar.plan({file.release_key: file})[file.release_key]

    assert effective.release_key == file.release_key
    assert effective.ir and effective.ir.listing == manual.listing
    tables = [source for source in effective.sources() if isinstance(source, QuarterlyTable)]
    assert len(tables) == 1
    table = tables[0]
    assert (table.listing, table.year, table.quarter) == (results, 2027, 3)
    assert table.rendered and table.cache_bust


def test_a_schedule_file_for_a_6k_filer_is_told_to_watch_6k(tmp_path: Path) -> None:
    company = AMAT.model_copy(update={"sec_form": "6-K"})
    expected = datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    file = Target.model_validate(
        to_target("AMAT", expected, 2026, 4, AMAT).model_dump(exclude={"sec_form"})
    )
    assert "sec_form" not in file.model_fields_set
    explicit = file.model_copy(update={"sec_form": "8-K"})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("AMAT", company)
        calendar.set_target(to_target("AMAT", expected, 2026, 4, company), "Issuer notice")

        [effective] = calendar.plan({file.release_key: file}).values()
        assert effective.sec_form == "6-K"

        # A file that names its form is left as written
        [kept] = calendar.plan({explicit.release_key: explicit}).values()
        assert kept.sec_form == "8-K"


def _wire_item(title: str, lead: str, guid: str, publisher: str | None = None) -> str:
    contributor = f"<dc:contributor>{publisher}</dc:contributor>" if publisher else ""
    return (
        f"<item><title>{title}</title><link>https://www.prnewswire.com/{guid}</link>"
        f"<guid>{guid}</guid><description>{lead}</description>{contributor}</item>"
    )


def test_a_saved_single_word_company_gets_its_wire_publisher_on_the_next_plan(
    tmp_path: Path,
) -> None:
    company = profile("AZTA", 933974, "Azenta, Inc.", LCID_SUBMISSIONS, date(2026, 11, 10))
    target = to_target("AZTA", datetime(2026, 11, 10, 16, 30, tzinfo=ET), 2026, 4, company)
    title = "Azenta Reports Fourth Quarter and Full Year Results for Fiscal 2026"
    featured = ("Year-end revenue grew as announced in the release. " * 5) + "(Nasdaq: AZTA)"
    feed = (
        '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        f"<link>{PR_NEWSWIRE_EARNINGS}</link>"
        + _wire_item(title, featured, "azenta", "Azenta, Inc.")
        + _wire_item(title, "No exchange tag", "headline")
        + _wire_item(title, "No exchange tag", "wrong-publisher", "Another Company")
        + _wire_item("Other Company Reports Fourth Quarter Results", "Its customer Azenta", "other")
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

    assert planned.wire_publisher == "Azenta"
    wire_sources = [s for s in planned.sources() if s.name in ("prnewswire", "prnewswire_busted")]
    assert len(wire_sources) == 2
    for source in wire_sources:
        assert [item.key for item in source.parse(response)] == ["azenta", "headline"]
