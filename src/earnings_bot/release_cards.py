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
            summary = old_summary.description, old_summary.footer.text or ""
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
    highlights.description = truncate(text, min(4096, 6000 - len(card) - len(highlights)))
    return [highlights, card]
