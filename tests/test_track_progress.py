import asyncio
import io
import itertools
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from earnings_bot import track_progress
from earnings_bot.bot import EarningsBot
from earnings_bot.browser_sources import IRSource
from earnings_bot.cache_research import CHECKING_CACHE
from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.date_discovery import VERIFYING_IR
from earnings_bot.delivery import DeliveryStore
from earnings_bot.edit_priority import EditPriority
from earnings_bot.formatting import GREEN
from earnings_bot.investigate import INVESTIGATION, Report
from earnings_bot.issuers import profile
from earnings_bot.known import KnownCompany, SourceCheck, to_target
from earnings_bot.pipeline import Release
from earnings_bot.recorder import ET, Journal, Target
from earnings_bot.release_cards import release_content
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.sources import RssFeed, Sighting
from earnings_bot.track_progress import REPLACE_AFTER, CardPoster, TrackProgress
from earnings_bot.version import release_version
from tests.fakes import RoutedFetcher, embed_text
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS
from tests.test_pipeline import uber
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
        "card_spacing": 0.0,
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


async def test_a_restart_leaves_a_card_alone_if_it_still_reads_the_same(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await tracker(store, channel).publish(1045810, card("NVDA · NVIDIA", "Same"))
        [message] = channel.messages.values()
        edited = message.edited_at
        # A new process has no memory of what it showed, only the message itself
        await tracker(store, channel).publish(1045810, card("NVDA · NVIDIA", "Same"))
        assert message.edited_at == edited
        await tracker(store, channel).publish(1045810, card("NVDA · NVIDIA", "Changed"))
        assert message.edited_at != edited
        assert message.embed is not None and message.embed.description == "Changed"
    assert channel.attempts == 1


async def test_card_edits_are_spaced_to_leave_room_for_release_edits(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        progress = tracker(store, channel, card_spacing=0.2)
        started = time.monotonic()
        for n in range(3):
            await progress.publish(1000 + n, card(f"T{n} · Company", "New"))
        # Three sends: the first goes at once, then two waits
        assert time.monotonic() - started >= 0.35
        started = time.monotonic()
        await progress.publish(1000, card("T0 · Company", "New"))  # unchanged: no wait
        assert time.monotonic() - started < 0.1


class WatchedDiscord(FakeDiscord):
    """Sets `sent` when a message goes out."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()

    async def send(
        self,
        content: str = "",
        *,
        nonce: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        allowed_mentions: object = None,
    ) -> FakeMessage:
        message = await super().send(content, nonce=nonce, embed=embed, embeds=embeds)
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


def test_a_complete_card_is_green_only_when_its_routes_are_verified_fresh(
    tmp_path: Path,
) -> None:
    target = lucid_target(ir=True)
    caveats: dict[int, list[str]] = {}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("LCID", INVESTIGATED)
        calendar.set_target(target, "Issuer confirmed release time <https://ir.example/notice>")
        [row] = calendar.entries()
        progress = tracker(
            store,
            FakeDiscord(),
            planned=lambda: {target.release_key: target},
            cache_notes=lambda _: ["IR press release listing · Query bypass verified"],
            cache_caveats=caveats.get,
        )
        calendar.save_review(row.cik, NOW, SPEED.to_json())
        verified = progress.render(row, SPEED.to_json(), False)
        caveats[row.cik] = ["IR press release listing"]
        unverified = progress.render(row, SPEED.to_json(), False)
        dot = progress.readiness(row.cik)

    assert verified.color is not None and verified.color.value == GREEN
    assert unverified.color is not None and unverified.color.value != GREEN
    assert unverified.description is not None
    assert "Research complete · watch scheduled" in unverified.description
    assert "**Not verified fresh:** IR press release listing." in unverified.description
    assert "SEC filings and newswires are watched too" in unverified.description
    assert dot == "🟡"


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
    # The bot now posts to channel 6, and the update script's window check moved the calendar
    # before the bot started
    with DeliveryStore(state, 6) as store:
        Calendar(store)
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


# Scorecards leave Discord's edit limits to release alerts


class OldEditCap:
    """Discord's cap on edits to messages over an hour old, which a channel shares: `tokens`
    edits, then one more each `refill` seconds, in turn."""

    def __init__(self, tokens: int, refill: float) -> None:
        self.tokens, self.refill = tokens, refill
        self.turn = asyncio.Lock()

    async def take(self) -> None:
        async with self.turn:
            if self.tokens == 0:
                await asyncio.sleep(self.refill)
                self.tokens = 1
            self.tokens -= 1


def refused_edit(retry_after: str) -> discord.HTTPException:
    response = SimpleNamespace(
        status=429, reason="Too Many Requests", headers={"Retry-After": retry_after}
    )
    return discord.HTTPException(
        cast(Any, response),
        {
            "message": "Maximum number of edits to messages older than 1 hour reached.",
            "code": 30046,
        },
    )


async def test_a_new_version_alone_doesnt_edit_a_card(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        shown = card("NVDA · NVIDIA", "Same")
        shown.set_footer(text="Earnings Bot v0.14.2 · Updated")
        await tracker(store, channel).publish(1045810, shown)
        deployed = card("NVDA · NVIDIA", "Same")
        deployed.set_footer(text="Earnings Bot v0.15.0 · Updated")
        await tracker(store, channel).publish(1045810, deployed)

    [message] = channel.messages.values()
    assert message.edited_at is None


async def test_a_restart_edits_no_card_whose_content_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the v0.15.0 restart on Nov 12, scorecards were edited three or four times each
    though none had changed: the footer named the new version, and until research ran again
    each card showed its cache preflight as queued, then being checked, then done."""
    channel = FakeDiscord()
    notes = {LCID_CIK: ["IR press release page · a new spelling reaches the server"]}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        row, _ = Calendar(store).add("LCID", INVESTIGATED)
        monkeypatch.setattr(track_progress, "release_version", lambda: "v0.14.2")
        before = tracker(store, channel, cache_notes=notes.get)
        before.offer(LCID_CIK, before.render(row, SPEED.to_json(), False))
        await before.flush()
        [message] = channel.messages.values()
        shown = embed_text(message.embed)

        # A new process, under a new version, whose research hasn't run yet
        monkeypatch.setattr(track_progress, "release_version", lambda: "v0.15.0")
        found: dict[int, list[str]] = {}
        looked: set[int] = set()
        after = tracker(store, channel, cache_notes=found.get, settled=looked.__contains__)
        queued = after.render(row, SPEED.to_json(), False)
        assert "Cache preflight (queued)" in embed_text(queued)
        after.offer(LCID_CIK, queued)
        await after.flush()
        # Research runs again and finds what it found before
        found.update(notes)
        looked.add(LCID_CIK)
        after.offer(LCID_CIK, after.render(row, SPEED.to_json(), False))
        await after.flush()

    assert message.edited_at is None
    assert embed_text(message.embed) == shown
    assert channel.attempts == 1


async def test_cards_wait_out_an_open_window_so_an_alert_edit_isnt_queued_behind_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NBIS's summary edit on its resumed alert failed with error 30046 on Nov 12 and landed
    72 s late, while scorecard edits spent the channel's cap on edits to old messages."""
    cap = OldEditCap(tokens=1, refill=0.5)
    edited: list[int] = []
    original = FakeMessage.edit

    async def capped(self: FakeMessage, **kwargs: Any) -> FakeMessage:
        await cap.take()
        edited.append(self.id)
        return await original(self, **kwargs)

    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        Calendar(store).add("LCID", LUCID)
        await tracker(store, channel).publish(LCID_CIK, card("LCID · Lucid Group, Inc.", "Old"))
        alert = await channel.send("🚨 **$NBIS Q3 FY26 earnings are out**", nonce="alert")
        monkeypatch.setattr(FakeMessage, "edit", capped)
        # The card's content changed, while a window is open
        progress = tracker(store, channel, busy=lambda: True)
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await alert.edit(content="🚨 **$NBIS Q3 FY26 earnings are out**\nWith its summary")
            took = time.monotonic() - started
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert edited == [alert.id]
    assert took < 0.25


async def test_a_release_edit_cancels_a_card_edit_already_waiting_on_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """discord.py waits out a 429 and sends the same edit again. An alert edit that comes up
    meanwhile goes first, and the card is edited after it."""
    channel = FakeDiscord()
    priority = EditPriority()
    target = uber()
    feed = RssFeed("issuer", "https://issuer.example/rss")
    one_at_a_time = asyncio.Lock()  # the channel's turn, which a request keeps while it waits
    original = FakeMessage.edit
    stall: int | None = None

    async def edit(self: FakeMessage, **kwargs: Any) -> FakeMessage:
        nonlocal stall
        async with one_at_a_time:
            if self.id == stall:
                stall = None
                await asyncio.sleep(30)  # Discord answered 429
            return await original(self, **kwargs)

    monkeypatch.setattr(FakeMessage, "edit", edit)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        progress = tracker(store, channel, priority=priority)
        await progress.publish(1045810, card("NVDA · NVIDIA", "Old"))
        [scorecard] = channel.messages.values()
        release = Release(
            target,
            channel.poster(store, target),
            RoutedFetcher({}),
            Journal(io.StringIO()),
            None,
            priority=priority,
        )
        release.add(feed, Sighting("1", "Results", "https://issuer.example/q3", earnings=True))
        await release.finish()
        alert = channel.messages[2]

        stall = scorecard.id
        progress.offer(1045810, card("NVDA · NVIDIA", "New"))
        flushing = asyncio.create_task(progress.flush())
        await asyncio.sleep(0.05)
        release.add(feed, Sighting("2", "Results", "https://wire.example/q3", earnings=True))
        await asyncio.wait_for(release.finish(), 1)
        await asyncio.wait_for(flushing, 1)

        assert "https://wire.example/q3" in release_content(alert)
        assert scorecard.embed is not None and scorecard.embed.description == "Old"
        await progress.flush()

    assert scorecard.embed is not None and scorecard.embed.description == "New"


async def test_old_cards_show_settled_results_one_at_a_time(tmp_path: Path) -> None:
    """Each hourly date check turned every card amber and back: two edits an hour on messages
    days old, which count against Discord's cap on edits to old messages."""
    channel = FakeDiscord()
    settled = {1000: False, 1001: True}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for cik in settled:
            await tracker(store, channel).publish(cik, card(f"T{cik} · Co", "Done"))
        progress = tracker(store, channel, settled=settled.__getitem__, old_card_spacing=0.3)

        def shown() -> list[str | None]:
            return [m.embed.description if m.embed else None for m in channel.messages.values()]

        progress.offer(1000, card("T1000 · Co", "Checking"))
        progress.offer(1001, card("T1001 · Co", "New date"))
        await progress.flush()
        assert shown() == ["Done", "New date"]

        settled[1000] = True
        progress.offer(1000, card("T1000 · Co", "Checked"))  # the latest content wins
        await progress.flush()
        assert shown() == ["Done", "New date"]  # within 0.3 s of the last old card's edit
        await asyncio.sleep(0.35)
        await progress.flush()
        assert shown() == ["Checked", "New date"]
        assert progress.pending == {}


async def test_a_fresh_card_shows_research_as_it_goes(tmp_path: Path) -> None:
    channel = FakeDiscord()
    channel.ids = itertools.count(discord.utils.time_snowflake(NOW - timedelta(minutes=10)))
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        progress = tracker(store, channel, settled=lambda _: False)
        await progress.publish(1000, card("T1000 · Co", "Queued"))
        progress.offer(1000, card("T1000 · Co", "Investigating"))
        await progress.flush()

    [message] = channel.messages.values()
    assert message.embed is not None and message.embed.description == "Investigating"


async def test_an_old_card_discord_keeps_refusing_is_posted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = FakeDiscord()
    tries = 0
    original = FakeMessage.edit

    async def refuse(self: FakeMessage, **kwargs: Any) -> FakeMessage:
        nonlocal tries
        tries += 1
        raise refused_edit("120")

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        progress = tracker(store, channel)
        await progress.publish(1045810, card("NVDA · NVIDIA", "Old"))
        [old] = channel.messages.values()
        monkeypatch.setattr(FakeMessage, "edit", refuse)

        progress.offer(1045810, card("NVDA · NVIDIA", "New"))
        await progress.flush()
        await progress.flush()  # Discord asked for two minutes
        assert tries == 1
        for _ in range(REPLACE_AFTER - 1):
            assert not await progress.publish(1045810, card("NVDA · NVIDIA", "New"), old=True)
        assert tries == REPLACE_AFTER
        assert list(channel.messages) == [old.id]

        # The next try posts the card as a new message and deletes the old one
        assert await progress.publish(1045810, card("NVDA · NVIDIA", "New"), old=True)
        new = channel.messages[2]
        assert old.deleted
        assert new.embed is not None and new.embed.description == "New"
        monkeypatch.setattr(FakeMessage, "edit", original)
        await progress.publish(1045810, card("NVDA · NVIDIA", "Newer"))

    assert new.embed is not None and new.embed.description == "Newer"
    assert tries == REPLACE_AFTER


async def test_the_audit_runs_in_the_background_during_a_window_but_not_its_own(
    tmp_path: Path,
) -> None:
    """Item 9: a window being open no longer holds every audit; the minutes around a release,
    and the company's own window, still do."""
    import contextlib
    import contextvars

    mode: contextvars.ContextVar[str] = contextvars.ContextVar("mode", default="normal")

    @contextlib.contextmanager
    def background() -> Iterator[None]:
        token = mode.set("background")
        try:
            yield
        finally:
            mode.reset(token)

    audited: list[tuple[str, str]] = []
    asked = asyncio.Event()

    async def audit(ticker: str, company: KnownCompany, when: datetime) -> Speed:
        audited.append((ticker, mode.get()))
        asked.set()
        return Speed(None)

    found = LUCID.model_copy(
        update={
            "investigated": date(2026, 11, 9),
            "evidence": "Investigated Nov 09: X.",
            "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
            "investigation": INVESTIGATION,
        }
    )
    own_window = {"open": True}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        Calendar(store).add("LCID", found)
        progress = tracker(
            store,
            WatchedDiscord(),
            audit=audit,
            busy=lambda: True,  # some window is open
            research_paused=lambda: False,
            in_window=lambda cik: own_window["open"],
            research_mode=background,
        )
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.sleep(0.3)
            assert audited == []  # Lucid's own window is open
            own_window["open"] = False
            progress.changed.set()
            await asyncio.wait_for(asked.wait(), 5)
            assert audited == [("LCID", "background")]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
