import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from earnings_bot.bot import EarningsBot
from earnings_bot.browser_sources import IRSource
from earnings_bot.cache_research import CHECKING_CACHE
from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.date_discovery import VERIFYING_IR
from earnings_bot.delivery import DeliveryStore
from earnings_bot.formatting import GREEN
from earnings_bot.investigate import INVESTIGATION, Report
from earnings_bot.issuers import profile
from earnings_bot.known import KnownCompany, SourceCheck, to_target
from earnings_bot.recorder import ET, Target
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.track_progress import CardPoster, TrackProgress
from earnings_bot.version import release_version
from tests.fakes import embed_text
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS
from tests.test_unattended import FakeDiscord, FakeMessage

NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)
LUCID = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, date(2026, 11, 9))
LISTING = "https://ir.lucidmotors.com/press-releases"
# What an investigation of Lucid finds
INVESTIGATED = LUCID.model_copy(
    update={
        "investigated": date(2026, 11, 9),
        "investigation": INVESTIGATION,
        "wire": "PR Newswire",
        "ir_listing": LISTING,
        "ir_http_status": 403,
        "platform": "own-site",
        "checks": [
            SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here"),
            SourceCheck(
                source="ir.lucidmotors.com without Chrome",
                watch=False,
                why="Blocks automated requests (HTTP 403)",
            ),
            SourceCheck(
                source=f"Press release page <{LISTING}>, read in Chrome",
                watch=True,
                why="Chrome read its latest results",
            ),
            SourceCheck(
                source="Business Wire earnings feed",
                watch=False,
                why="Its releases go out on PR Newswire",
            ),
        ],
    }
)


def test_directory_bot_registers_tracking_commands(tmp_path: Path) -> None:
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}), tmp_path)
    assert {"track", "upcoming", "untrack"} <= {command.name for command in bot.tree.get_commands()}


async def test_watch_status_shows_build_separately_from_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("earnings_bot.bot.build_revision", lambda: "0123456789")
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}))
    send = AsyncMock()
    interaction = cast(discord.Interaction, Mock(response=Mock(send_message=send)))

    await bot._status(interaction)

    send.assert_awaited_once_with(
        f"**Earnings Bot {release_version()}**\nBuild `0123456789`\n"
        "Earnings monitoring is not configured.",
        ephemeral=True,
    )


def lines_of(embed: discord.Embed) -> dict[str, list[str]]:
    return {field.name or "": (field.value or "").splitlines() for field in embed.fields}


def card(title: str, text: str) -> discord.Embed:
    return discord.Embed(title=title, description=text)


async def never(ticker: str, company: KnownCompany, when: datetime) -> Speed:
    raise AssertionError("The publisher should not start an audit")


def tracker(store: DeliveryStore, channel: FakeDiscord, **kwargs: object) -> TrackProgress:
    defaults: dict[str, object] = {
        "planned": dict,
        "now": lambda: NOW,
        "busy": lambda: False,
        "audit": never,
    }
    defaults.update(kwargs)
    return TrackProgress(
        Calendar(store),
        store,
        cast(discord.abc.Messageable, channel),
        42,
        **defaults,  # type: ignore[arg-type]
    )


async def test_a_card_resumes_after_discord_accepted_its_send(tmp_path: Path) -> None:
    channel = FakeDiscord()
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        channel.lose_response = True
        await tracker(store, channel).publish(1045810, card("NVDA · NVIDIA", "Queued"))
        assert store.pending()
    with DeliveryStore(db, 7) as store:
        await tracker(store, channel).publish(1045810, card("NVDA · NVIDIA", "Finished"))
        assert not store.pending()
    assert channel.attempts == 1
    [message] = channel.messages.values()
    assert message.embed is not None and message.embed.description == "Finished"


async def test_a_card_is_edited_in_place_and_sent_again_if_deleted(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        poster = CardPoster(cast(discord.abc.Messageable, channel), store, "track:7:1", 42)
        await poster.show(card("NVDA · NVIDIA", "One"))
        await poster.show(card("NVDA · NVIDIA", "Two"))
        assert list(channel.messages) == [1]
        assert channel.messages[1].embed and channel.messages[1].embed.description == "Two"

        channel.delete(1)
        await poster.show(card("NVDA · NVIDIA", "Three"))
        assert list(channel.messages) == [2]
        assert channel.messages[2].embed and channel.messages[2].embed.description == "Three"


async def test_an_unchanged_card_isnt_edited_again(tmp_path: Path) -> None:
    channel = FakeDiscord()
    edits = 0
    original = FakeMessage.edit

    async def counted(self: FakeMessage, **kwargs: object) -> FakeMessage:
        nonlocal edits
        edits += 1
        return await original(self, **kwargs)  # type: ignore[arg-type]

    FakeMessage.edit = counted  # type: ignore[method-assign]
    try:
        with DeliveryStore(tmp_path / "state.db", 7) as store:
            progress = tracker(store, channel)
            for text in ("Same", "Same", "Same", "Changed"):
                await progress.publish(1045810, card("NVDA · NVIDIA", text))
    finally:
        FakeMessage.edit = original  # type: ignore[method-assign]
    assert channel.attempts == 1
    assert edits == 1


class WatchedDiscord(FakeDiscord):
    """Sets `sent` when a message goes out."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()

    async def send(
        self, content: str = "", *, nonce: str, embed: discord.Embed | None = None
    ) -> FakeMessage:
        message = await super().send(content, nonce=nonce, embed=embed)
        self.sent.set()
        return message


async def test_the_audit_waits_for_the_investigation(tmp_path: Path) -> None:
    channel = WatchedDiscord()
    audited: list[str] = []
    asked = asyncio.Event()

    async def audit(ticker: str, company: KnownCompany, when: datetime) -> Speed:
        audited.append(company.evidence)
        asked.set()
        return Speed(None)

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", LUCID)
        progress = tracker(store, channel, audit=audit)
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.wait_for(channel.sent.wait(), 5)
            [message] = channel.messages.values()
            assert "▫️ Investigating sources (queued)" in embed_text(message.embed)
            assert audited == []

            found = LUCID.model_copy(
                update={
                    "investigated": date(2026, 11, 9),
                    "evidence": "Investigated Nov 09: X.",
                    "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
                    "investigation": INVESTIGATION,
                }
            )
            calendar.save_profile(LCID_CIK, found)
            progress.changed.set()
            await asyncio.wait_for(asked.wait(), 5)
            # The audit uses what the investigation found
            assert audited == ["Investigated Nov 09: X."]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_the_card_follows_an_investigation_as_it_goes(tmp_path: Path) -> None:
    report = Report()
    report.doing("Opening ir.lucidmotors.com in Chrome")
    report.reject("ir.lucidmotors.com without Chrome", "Blocks automated requests (HTTP 403)")
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", LUCID)
        progress = tracker(store, FakeDiscord(), research={LCID_CIK: report}.get)

        embed = progress.render(row, None, False)

    text = embed_text(embed)
    assert embed.title == "LCID · Lucid Group, Inc."
    assert "⏳ Investigating sources: Opening ir.lucidmotors.com in Chrome…" in text
    assert "Researching. This message updates as it goes." in text
    assert "• **ir.lucidmotors.com without Chrome**: Blocks automated requests (HTTP 403)" in text


def lucid_target(ir: bool) -> Target:
    target = to_target("LCID", datetime(2026, 11, 12, 16, 5, tzinfo=ET), 2026, 3, INVESTIGATED)
    if ir:
        target.ir = IRSource(listing=LISTING, headline="Third Quarter 2026 Results")
    return target


SPEED = Speed(
    date(2026, 8, 5),
    confident=True,
    measured_headlines=[
        Timing("edgar_latest", datetime(2026, 8, 5, 16, 8, 53, tzinfo=ET)),
        Timing("prnewswire_busted", datetime(2026, 8, 5, 16, 5, 17, tzinfo=ET)),
        Timing("prnewswire", datetime(2026, 8, 5, 16, 6, 2, tzinfo=ET)),
        Timing("browser-ir", datetime(2026, 8, 5, 16, 6, 59, tzinfo=ET)),
    ],
    benchmark="[@earnings_guy posted](https://x.com/earnings_guy/status/1) at 16:06:00 ET",
)


async def test_a_finished_card_says_what_was_fastest_watched_and_rejected(tmp_path: Path) -> None:
    target = lucid_target(ir=True)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", INVESTIGATED)
        evidence = "Issuer confirmed release time <https://ir.lucidmotors.com/notice>"
        calendar.set_target(target, evidence)
        [row] = calendar.entries()
        progress = tracker(store, FakeDiscord(), planned=lambda: {target.release_key: target})

        embed = progress.render(row, SPEED.to_json(), False)

    fields = lines_of(embed)
    assert embed.color is not None and embed.color.value == GREEN
    assert embed.description == (
        "**✅ Research complete · watch scheduled**\n"
        "**Next results:** <t:1794517500:F> (<t:1794517500:R>) · Q3 FY26\n"
        "-# Issuer confirmed release time · [source](<https://ir.lucidmotors.com/notice>) · "
        "watching from 3:55 PM ET"
    )
    assert embed.footer.text == f"Earnings Bot {release_version()} · Updated"
    assert fields["Progress"] == [
        f"✅ Found in SEC data: CIK {LCID_CIK}, results on Form 8-K",
        "✅ Sources investigated Nov 9",
        "✅ Release date found",
        "✅ Speed history checked",
    ]
    # Copies of one feed count once, at the earliest
    assert fields["⚡ Fastest last time"] == [
        "🥇 **PR Newswire earnings feed** at 4:05:17 PM ET",
        "IR press release page, read in Chrome +1m 42s",
        "SEC 8-K filings +3m 36s",
        "-# Aug 5, 2026 release · seen live by the bot",
        "-# [@earnings_guy posted](https://x.com/earnings_guy/status/1) at 16:06:00 ET",
    ]
    assert fields["👀 Watching for the next release"] == [
        "• SEC 8-K filings",
        "• PR Newswire earnings feed",
        "• IR press release page, read in Chrome",
    ]
    # The Chrome page was verified for this release, so only the others are rejected
    assert fields["🚫 Checked and rejected"] == [
        "• **ir.lucidmotors.com without Chrome**: Blocks automated requests (HTTP 403)",
        "• **Business Wire earnings feed**: Its releases go out on PR Newswire",
    ]


async def test_an_unverified_ir_page_says_why_it_isnt_watched(tmp_path: Path) -> None:
    target = lucid_target(ir=False)
    notes = {LCID_CIK: "No Q2 2026 results headline on the page to check the pattern against"}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", INVESTIGATED)
        calendar.set_target(target, "Manual override")
        [row] = calendar.entries()
        progress = tracker(
            store,
            FakeDiscord(),
            planned=lambda: {target.release_key: target},
            ir_note=notes.get,
        )

        fields = lines_of(progress.render(row, None, False))

    assert not any(
        "IR press release page" in line for line in fields["👀 Watching for the next release"]
    )
    assert fields["🚫 Checked and rejected"][-1] == (
        "• **IR press release page, read in Chrome**: No Q2 2026 results headline on the page "
        "to check the pattern against"
    )


async def test_without_a_date_the_card_says_what_it_would_watch(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        phase = "No release date found yet. Rechecking hourly."
        progress = tracker(store, FakeDiscord(), date_phase={LCID_CIK: phase}.get)

        embed = progress.render(row, None, False)
        complete = progress.render(row, SPEED.to_json(), False)

    fields = lines_of(embed)
    assert embed.description == "No release date yet."
    assert complete.description == (
        "**✅ Research complete · awaiting a confirmed date**\nNo release date yet."
    )
    assert f"⚠️ {phase}" in fields["Progress"]
    assert fields["👀 Will watch once a date is set"][-1] == (
        "• IR press release page *(verified after the date is set)*"
    )
    assert fields["IR page access"] == [
        f"[Press release listing](<{LISTING}>) · Direct HTTP returned 403"
    ]


def test_a_scheduled_card_waits_for_ir_verification_to_finish(tmp_path: Path) -> None:
    target = lucid_target(ir=False)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        progress = tracker(
            store,
            FakeDiscord(),
            planned=lambda: {target.release_key: target},
            date_phase=lambda _: VERIFYING_IR,
        )
        embed = progress.render(row, SPEED.to_json(), False)

    assert embed.description and "Research complete" not in embed.description
    assert f"⏳ {VERIFYING_IR}…" in lines_of(embed)["Progress"]


def test_the_card_shows_cache_preflight_before_research_completes(tmp_path: Path) -> None:
    target = lucid_target(ir=True)
    phases = {LCID_CIK: CHECKING_CACHE}
    notes: dict[int, list[str]] = {}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        progress = tracker(
            store,
            FakeDiscord(),
            planned=lambda: {target.release_key: target},
            date_phase=phases.get,
            cache_notes=notes.get,
        )
        working = progress.render(row, SPEED.to_json(), False)
        phases.clear()
        notes[row.cik] = ["IR press release listing · Query bypass verified"]
        complete = progress.render(row, SPEED.to_json(), False)

    assert working.description and "Research complete" not in working.description
    assert f"⏳ {CHECKING_CACHE}…" in lines_of(working)["Progress"]
    assert complete.description and "watch scheduled" in complete.description
    assert lines_of(complete)["Cache preflight"] == [
        "• IR press release listing · Query bypass verified"
    ]


async def test_a_check_saved_in_the_old_format_is_redone(tmp_path: Path) -> None:
    channel = WatchedDiscord()
    asked = asyncio.Event()

    async def audit(ticker: str, company: KnownCompany, when: datetime) -> Speed:
        asked.set()
        return SPEED

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", INVESTIGATED)
        calendar.save_review(LCID_CIK, NOW - timedelta(hours=1), "**Last release | Aug 05**")
        progress = tracker(store, channel, audit=audit)
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.wait_for(asked.wait(), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _, saved = calendar.review(LCID_CIK)
    assert Speed.from_json(saved) == SPEED


async def test_a_removed_ticker_gets_a_closing_card(tmp_path: Path) -> None:
    channel = WatchedDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", INVESTIGATED)
        calendar.save_review(LCID_CIK, NOW, SPEED.to_json())
        progress = tracker(store, channel)
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.wait_for(channel.sent.wait(), 5)
            calendar.remove(LCID_CIK)
            progress.removed[LCID_CIK] = "LCID"
            progress.changed.set()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if not progress.removed:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    [message] = channel.messages.values()
    assert message.embed is not None
    assert (message.embed.title, message.embed.description) == (
        "LCID · removed",
        "No longer on the earnings calendar.",
    )


async def test_a_page_rejected_at_first_but_verified_later_isnt_listed_as_rejected(
    tmp_path: Path,
) -> None:
    rejected = SourceCheck(
        source="ir.lucidmotors.com press release page",
        watch=False,
        why=f"<{LISTING}> shows no results headline",
    )
    company = INVESTIGATED.model_copy(update={"checks": [rejected]})
    target = lucid_target(ir=True)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", company)
        calendar.set_target(target, "Manual override")
        [row] = calendar.entries()
        progress = tracker(store, FakeDiscord(), planned=lambda: {target.release_key: target})

        fields = lines_of(progress.render(row, None, False))

    assert "🚫 Checked and rejected" not in fields
    assert "• IR press release page, read in Chrome" in fields["👀 Watching for the next release"]


def test_a_lone_timing_isnt_called_the_fastest(tmp_path: Path) -> None:
    accepted = datetime(2026, 8, 5, 16, 8, 17, tzinfo=ET)
    speed = Speed(date(2026, 8, 5), archive=[Timing("SEC 8-K acceptance", accepted)])
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        fields = lines_of(tracker(store, FakeDiscord()).render(row, speed.to_json(), False))
    # One time isn't a race, so nothing is called the fastest
    assert "⚡ Fastest last time" not in fields
    assert fields["⏱️ Last release"] == [
        "Only one time is known for it: **SEC 8-K filings** at 4:08:17 PM ET.",
        "Which source was first isn't known yet. The next release is timed live.",
        "-# Aug 5, 2026 release · publishers' timestamps",
    ]


def test_a_minute_resolution_timestamp_cannot_win_by_seconds(tmp_path: Path) -> None:
    posted = datetime(2026, 8, 5, 16, 1, tzinfo=ET)
    accepted = posted.replace(second=10)
    speed = Speed(
        posted.date(),
        archive=[
            Timing("browser-ir", posted, resolution_seconds=60),
            Timing("SEC 8-K acceptance", accepted),
        ],
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        fields = lines_of(tracker(store, FakeDiscord()).render(row, speed.to_json(), False))

    assert "⚡ Fastest last time" not in fields
    assert fields["⏱️ Last release"][:3] == [
        "The reported times overlap, so the order is unresolved.",
        "**IR press release page, read in Chrome** at 4:01 PM ET",
        "**SEC 8-K filings** at 4:01:10 PM ET",
    ]


async def test_a_moved_calendars_cards_are_edited_not_posted_again(tmp_path: Path) -> None:
    channel = FakeDiscord()
    state, cards = tmp_path / "state.db", tmp_path / "cards.db"
    with DeliveryStore(state, 5) as store, DeliveryStore(cards, 7) as card_store:
        calendar = Calendar(store)
        row, _ = calendar.add("NVDA")
        first = TrackProgress(
            calendar,
            card_store,
            cast(discord.abc.Messageable, channel),
            42,
            planned=dict,
            now=lambda: NOW,
            busy=lambda: False,
            audit=never,
        )
        await first.publish(row.cik, card("NVDA · NVIDIA", "Queued"))
    # The bot now posts to channel 6, and the calendar came along
    with DeliveryStore(state, 6) as store, DeliveryStore(cards, 7) as card_store:
        moved = TrackProgress(
            Calendar(store),
            card_store,
            cast(discord.abc.Messageable, channel),
            42,
            planned=dict,
            now=lambda: NOW,
            busy=lambda: False,
            audit=never,
        )
        moved.follow_calendar()
        await moved.publish(row.cik, card("NVDA · NVIDIA", "Finished"))

    [message] = channel.messages.values()
    assert "Finished" in embed_text(message.embeds[0])
