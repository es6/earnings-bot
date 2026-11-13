"""Discord release cards, with a readable text form for durable delivery and recovery."""

import re
from datetime import datetime
from typing import Protocol

import discord

from earnings_bot.formatting import GREEN, truncate
from earnings_bot.version import release_version

RELEASE = "EARNINGS RELEASE"
HIGHLIGHTS = "EARNINGS HIGHLIGHTS"
LINKS = "**Sources & documents**"
_HEADER = re.compile(r"🚨 \*\*(\$[^\n]+?) earnings are out\*\*.*")
# A summary longer than one embed shows continues in more, titled this, rather than being cut
CONTINUED = "Highlights, continued"
# Discord's limits: 4,096 characters in an embed's description, 6,000 in all of a message's
# embeds together (titles and author names included), and 10 embeds
DESCRIPTION, MESSAGE, EMBEDS = 4096, 6000, 10
_LEFT_OUT = "-# {count} more lines didn't fit in Discord's 6,000-character message limit"


def _pack(lines: list[str], room: int, slots: int) -> tuple[list[str], int]:
    """Splits a summary's lines into embed descriptions, at line breaks, within `room`
    characters in all (each extra embed's title and author included) and `slots` embeds.
    Returns the descriptions and how many lines didn't fit."""
    extra = len(CONTINUED) + len(HIGHLIGHTS)
    chunks: list[list[str]] = [[]]
    size = 0
    for n, whole in enumerate(lines):
        line = whole[:DESCRIPTION]
        cost = len(line) + (1 if chunks[-1] else 0)
        if size + cost > DESCRIPTION:
            if len(chunks) == slots or room < extra + len(line):
                return ["\n".join(chunk) for chunk in chunks], len(lines) - n
            chunks.append([])
            size = 0
            room -= extra
            cost = len(line)
        if cost > room:
            return ["\n".join(chunk) for chunk in chunks], len(lines) - n
        chunks[-1].append(line)
        size += cost
        room -= cost
    return ["\n".join(chunk) for chunk in chunks], 0


def _summary_text(embeds: list[discord.Embed]) -> str:
    """A summary's full text back from its embeds, the first and any continued ones."""
    parts = [e.description or "" for e in embeds if e.author.name == HIGHLIGHTS]
    lines = "\n".join(parts).split("\n")
    if lines and lines[-1].startswith("-# ") and "didn't fit" in lines[-1]:
        lines.pop()
    return "\n".join(lines)


class MessageView(Protocol):
    content: str

    @property
    def embeds(self) -> list[discord.Embed]: ...


def release_content(message: MessageView) -> str:
    """Recover the canonical alert text; legacy plain alerts keep working too."""
    card = next((e for e in message.embeds if e.author.name == RELEASE), None)
    if card is None or not card.description:
        return message.content
    lines = [line for line in card.description.splitlines() if line and line != LINKS]
    if not lines:
        return message.content
    return "\n".join([message.content, f"-# {lines[0]}", *lines[1:]])


def release_cards(
    content: str,
    *,
    previous: MessageView | None = None,
    first_seen: datetime | None = None,
    summary: tuple[str, str] | None = None,
) -> list[discord.Embed] | None:
    """Build locally, without another request before the first alert. Summary and source
    updates remain on one message; their text fits Discord's shared 6,000-character limit."""
    lines = content.splitlines()
    if len(lines) < 2 or not lines[1].startswith("-# "):
        return None
    header = _HEADER.fullmatch(lines[0])
    if header is None:
        return None
    identity = header[1].replace(" ", " · ", 1)
    prior = previous.embeds if previous is not None else []
    old_card = next((e for e in prior if e.author.name == RELEASE), None)
    if summary is None:
        old_summary = next(
            (
                e
                for e in prior
                if e.author.name == HIGHLIGHTS
                or (e.footer.text or "").startswith("Summary written by Claude")
            ),
            None,
        )
        if old_summary and old_summary.description:
            text = old_summary.description
            if old_summary.author.name == HIGHLIGHTS:
                text = _summary_text(prior)  # the first embed and any continued ones
            summary = text, old_summary.footer.text or ""
    details = lines[1].removeprefix("-# ")
    if lines[2:]:
        details += "\n\n" + ("" if summary else f"{LINKS}\n") + "\n".join(lines[2:])
    card = discord.Embed(
        title="Sources & documents" if summary else truncate(identity, 256),
        description=details,
        color=GREEN,
        timestamp=(old_card.timestamp if old_card else None) or first_seen,
    )
    card.set_author(name=RELEASE)
    card.set_footer(text=f"Earnings Bot {release_version()} · Links and files update here")
    if summary is None:
        return [card]
    text, footer = summary
    footer = truncate(footer, 2048)
    highlights = discord.Embed(title=truncate(identity, 256), color=GREEN)
    highlights.set_author(name=HIGHLIGHTS)
    highlights.set_footer(text=footer)
    # Every line shows: what one embed can't hold goes on in more embeds, and anything past
    # the message's limit is counted where people can see it. It used to be cut silently.
    lines_of_text = text.split("\n")
    room = MESSAGE - len(card) - len(highlights)
    descriptions, left_out = _pack(lines_of_text, room, EMBEDS - 1)
    if left_out:
        reserve = len(_LEFT_OUT.format(count=len(lines_of_text))) + 1
        descriptions, left_out = _pack(lines_of_text, room - reserve, EMBEDS - 1)
        descriptions[-1] += "\n" + _LEFT_OUT.format(count=left_out)
    highlights.description = descriptions[0]
    embeds = [highlights]
    for description in descriptions[1:]:
        more = discord.Embed(title=CONTINUED, description=description, color=GREEN)
        more.set_author(name=HIGHLIGHTS)
        embeds.append(more)
    return [*embeds, card]
