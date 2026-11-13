import re
from datetime import datetime
from pathlib import Path
from typing import cast

import discord
import pytest

from earnings_bot.delivery import DeliveryStore
from earnings_bot.formatting import GREEN
from earnings_bot.pipeline import DurableDiscordPoster
from earnings_bot.recorder import ET
from earnings_bot.release_cards import HIGHLIGHTS, RELEASE, release_cards, release_content
from tests.test_unattended import FakeDiscord, FakeMessage

SEEN = datetime(2026, 11, 9, 16, 5, 2, 708000, tzinfo=ET)
HEADER = "🚨 **$LCID Q3 FY26 earnings are out**"
WHEN = "-# First seen 4:05:02.708 PM ET via PR Newswire"
WIRE = "📰 [PR Newswire](<https://wire.example/results>)"
IR = "🌐 [IR site](<https://issuer.example/results>)"
CONTENT = f"{HEADER}\n{WHEN}\n{WIRE}"
SUMMARY = "🔹 Revenue: $200M\n\nGuidance:\n🔹 Deliveries: 20,000"
FOOTER = "Summary written by Claude in 1s from the IR site text"  # a pre-0.10 card


def poster(channel: FakeDiscord, store: DeliveryStore) -> DurableDiscordPoster:
    result = DurableDiscordPoster(cast(discord.abc.Messageable, channel), store, "LCID:Q3", 42)
    result.first_seen = SEEN
    return result


async def test_the_first_alert_is_one_green_card_with_a_ticker_notification(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await poster(channel, store).send(CONTENT)

    [message] = channel.messages.values()
    assert channel.attempts == 1
    assert message.content == HEADER
    [card] = message.embeds
    assert (card.title, card.author.name, card.color) == (
        "$LCID · Q3 FY26",
        RELEASE,
        discord.Color(GREEN),
    )
    assert card.description and "Sources & documents" in card.description
    assert card.timestamp == SEEN
    assert release_content(message) == CONTENT


async def test_late_links_and_files_keep_the_summary_and_first_seen_after_restart(
    tmp_path: Path,
) -> None:
    channel = FakeDiscord()
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        first = poster(channel, store)
        await first.send(CONTENT)
        await first.attach("release.pdf", b"%PDF-1.7")
        await first.set_summary(SUMMARY, FOOTER)
    with DeliveryStore(db, 7) as store:
        restarted = poster(channel, store)
        restarted.first_seen = SEEN.replace(hour=17)
        await restarted.send(f"{HEADER}\n-# A later sighting\n{IR}")

    [message] = channel.messages.values()
    assert channel.attempts == 1
    summary, sources = message.embeds
    assert summary.author.name == HIGHLIGHTS and summary.description == SUMMARY
    assert summary.footer.text == FOOTER
    assert sources.title == "Sources & documents" and sources.timestamp == SEEN
    assert release_content(message) == f"{CONTENT}\n{IR}"
    assert [a.filename for a in message.attachments] == ["release.pdf"]


async def test_a_card_accepted_before_a_crash_is_recovered_without_another_send(
    tmp_path: Path,
) -> None:
    channel = FakeDiscord()
    channel.lose_response = True
    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store, pytest.raises(TimeoutError):
        await poster(channel, store).send(CONTENT)
    with DeliveryStore(db, 7) as store:
        restarted = poster(channel, store)
        await restarted.send(CONTENT)
        assert restarted.resumed and not store.pending()
    assert channel.attempts == 1


async def test_a_legacy_alert_keeps_its_summary_when_the_next_link_upgrades_it(
    tmp_path: Path,
) -> None:
    channel = FakeDiscord()
    old_summary = discord.Embed(description=SUMMARY)
    old_summary.set_footer(text=FOOTER)
    message = await channel.send(CONTENT, nonce="legacy", embed=old_summary)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.prepare("LCID:Q3", CONTENT)
        store.delivered("LCID:Q3", message.id)
        await poster(channel, store).send(f"{HEADER}\n{WHEN}\n{IR}")

    assert channel.attempts == 1
    assert message.embeds[0].description == SUMMARY
    assert release_content(message) == f"{CONTENT}\n{IR}"


async def test_replacing_a_deleted_card_keeps_its_edited_links_and_summary(tmp_path: Path) -> None:
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        active = poster(channel, store)
        await active.send(CONTENT)
        await active.edit(f"{CONTENT}\n{IR}")
        await active.set_summary(SUMMARY, FOOTER)
        channel.delete(1)
        later = "🏛️ [SEC 8-K](<https://www.sec.gov/filing>)"
        await active.edit(f"{CONTENT}\n{later}")

    [message] = channel.messages.values()
    assert channel.attempts == 2
    assert message.embeds[0].description == SUMMARY
    assert release_content(message) == f"{CONTENT}\n{IR}\n{later}"


def test_long_links_and_summaries_fit_discord_without_losing_recovery_data() -> None:
    link = "📄 [Results PDF](<https://issuer.example/" + "a" * 1400 + ".pdf>)"
    content = f"{CONTENT}\n{link}"
    cards = release_cards(content, first_seen=SEEN, summary=(SUMMARY * 200, FOOTER * 50))
    assert cards is not None
    assert sum(map(len, cards)) <= 6000
    assert all(len(c.description or "") <= 4096 for c in cards)
    assert len(cards) <= 10
    # What still doesn't fit is counted where people can see it, not cut silently
    shown = [c for c in cards if c.author.name == HIGHLIGHTS]
    assert re.search(r"\d+ more lines didn't fit", shown[-1].description or "")
    message = FakeMessage(1, HEADER, extra_embeds=cards)
    assert release_content(message) == content


def bn_like_summary(lines: int = 60) -> str:
    """A long brief: BN's ran 25 lines on Nov 12, and briefs ran 13 to 33."""
    return "\n".join(
        f"🔹 **Segment {n}**: distributable earnings ${n}.{n:02d}B; +{n}% YoY" for n in range(lines)
    )


def test_a_summary_longer_than_one_embed_shows_every_line() -> None:
    text = bn_like_summary(80)
    assert len(text) > 4096
    cards = release_cards(CONTENT, first_seen=SEEN, summary=(text, FOOTER))
    assert cards is not None
    shown = [c for c in cards if c.author.name == HIGHLIGHTS]
    assert len(shown) >= 2 and shown[1].title == "Highlights, continued"
    assert "\n".join(c.description or "" for c in shown) == text
    assert sum(map(len, cards)) <= 6000
    assert cards[-1].author.name == RELEASE


def test_a_later_link_keeps_a_continued_summary_whole() -> None:
    text = bn_like_summary(80)
    cards = release_cards(CONTENT, first_seen=SEEN, summary=(text, FOOTER))
    assert cards is not None
    posted = FakeMessage(1, HEADER, extra_embeds=cards)

    again = release_cards(f"{CONTENT}\n{IR}", previous=posted)

    assert again is not None
    shown = [c for c in again if c.author.name == HIGHLIGHTS]
    assert "\n".join(c.description or "" for c in shown) == text
    assert shown[0].footer.text == FOOTER


def test_catchup_and_replay_labels_stay_visible_and_legacy_content_is_untouched() -> None:
    content = CONTENT.replace(HEADER, HEADER + " (replay)").replace(
        "First seen", "Already out when the bot started watching · First seen"
    )
    cards = release_cards(content)
    assert cards and "Already out" in (cards[0].description or "")
    assert release_content(FakeMessage(1, HEADER + " (replay)", extra_embeds=cards)) == content
    assert release_cards("Old alert\nTimestamp\nIssuer: <https://issuer.example/>") is None
