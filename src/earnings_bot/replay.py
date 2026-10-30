"""Replays a past release through the real pipeline and into Discord, to test it end to end.

    uv run earnings-bot-replay schedules/2026-11-04.toml UBER --date 2026-08-05 \\
        --q4-release 1585 --edgar 0001543151-26-000027 \\
        --file https://s23.q4cdn.com/407969754/files/doc_earnings/2026/q2/earnings-result/Uber-Q2-26-Earnings-Press-Release.pdf

It feeds the pipeline the same items the pollers would have seen, one second apart: the IR
release, then the files, then the 8-K. The post is marked as a replay.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

import discord

from earnings_bot.config import Settings
from earnings_bot.fetch import Fetcher
from earnings_bot.pipeline import DiscordPoster, Release
from earnings_bot.recorder import ET, Journal, Target, load_plan, sync_clock
from earnings_bot.sources import EdgarFilings, GuessedFile, Q4PressReleases, Sighting, Source
from earnings_bot.summary import Summarizer


async def past_sightings(
    fetcher: Fetcher, target: Target, args: argparse.Namespace
) -> list[tuple[Source, Sighting]]:
    found: list[tuple[Source, Sighting]] = []
    if args.q4_release:
        if not target.q4_host:
            sys.exit(f"{target.ticker} has no q4_host in the schedule")
        # Deep enough to reach a release from last quarter, and its body for the summary
        q4 = Q4PressReleases(target.q4_host, page_size=40)
        response = await fetcher.request("GET", q4.url())
        matches = [s for s in q4.parse(response) if s.key == args.q4_release]
        if not matches:
            sys.exit(f"Q4 release {args.q4_release} isn't among {target.ticker}'s last 40")
        found.append((q4, matches[0]))
    for url in args.file:
        guess = GuessedFile(url)
        found.append((guess, Sighting(url, guess.name, url, earnings=True)))
    if args.edgar:
        edgar = EdgarFilings(target.cik, limit=300)
        response = await fetcher.request("GET", edgar.url())
        matches = [s for s in edgar.parse(response) if s.key == args.edgar]
        if not matches:
            sys.exit(f"Filing {args.edgar} isn't among {target.ticker}'s last 300")
        found.append((edgar, matches[0]))
    return found


async def replay(args: argparse.Namespace, settings: Settings) -> None:
    targets = [t for t in load_plan(args.schedule).target if t.ticker == args.ticker]
    if not targets:
        sys.exit(f"{args.schedule} has no {args.ticker}")
    target = targets[0]
    if args.date:
        # Estimates are looked up for the release's own day
        day = date.fromisoformat(args.date)
        expected = datetime.combine(day, target.expected.timetz()).replace(tzinfo=ET)
        target = target.model_copy(update={"expected": expected})
    if settings.discord_channel_id is None or settings.sec_user_agent is None:
        sys.exit("Set DISCORD_CHANNEL_ID and SEC_USER_AGENT in .env")
    key, fmp_key = settings.anthropic_api_key, settings.fmp_api_key

    client = discord.Client(intents=discord.Intents.none())
    # REST only: sending and editing messages doesn't need the Gateway connection
    await client.login(settings.discord_token.get_secret_value())
    try:
        channel = await client.fetch_channel(settings.discord_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            sys.exit(f"Channel {settings.discord_channel_id} can't take messages")
        log_path = args.schedule.with_name(f"replay-{args.ticker}.jsonl")
        with log_path.open("a", encoding="utf-8") as file:
            journal = Journal(file)
            await sync_clock(journal)
            async with Fetcher(settings.sec_user_agent) as fetcher:
                sightings = await past_sightings(fetcher, target, args)
                release = Release(
                    target,
                    DiscordPoster(channel),
                    fetcher,
                    journal,
                    Summarizer(key.get_secret_value()) if key else None,
                    fmp_key.get_secret_value() if fmp_key else None,
                    note=" (replay of a past release)",
                )
                for source, sighting in sightings:
                    journal.say(f"{target.ticker} {source.name}: replaying {sighting.title}")
                    release.add(source, sighting)
                    await asyncio.sleep(1)
                await release.finish()
        journal.say(f"Done. Events are in {log_path}")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="earnings-bot-replay", description="Replay a past release into Discord."
    )
    parser.add_argument("schedule", type=Path, help="schedule file that lists the company")
    parser.add_argument("ticker")
    parser.add_argument("--date", help="the past release's date, for estimates (YYYY-MM-DD)")
    parser.add_argument("--q4-release", help="PressReleaseId in the company's Q4 feed")
    parser.add_argument("--edgar", help="accession number of the earnings 8-K")
    parser.add_argument("--file", action="append", default=[], help="release file URL")
    args = parser.parse_args()

    # Pyright can't see that BaseSettings reads its fields from the environment
    settings = Settings()  # pyright: ignore[reportCallIssue]
    asyncio.run(replay(args, settings))


if __name__ == "__main__":
    main()
