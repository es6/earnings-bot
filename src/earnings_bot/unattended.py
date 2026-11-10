"""Watch a changing schedule directory with one polling and delivery coordinator."""

import asyncio
import tomllib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import aiohttp
import discord
from pydantic import ValidationError

from earnings_bot.benchmark import BenchmarkResult, EarningsBenchmark
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore, retry_delivery
from earnings_bot.fetch import Fetcher
from earnings_bot.formatting import (
    AMBER,
    GREEN,
    clock,
    day,
    duration,
    period_label,
    source_label,
    stamp,
    truncate,
)
from earnings_bot.investigate import wire_seen, with_wire
from earnings_bot.issuers import Issuers
from earnings_bot.pipeline import (
    DiscordPoster,
    DurableDiscordPoster,
    Releases,
    ReleaseText,
    keep_warm,
)
from earnings_bot.recorder import (
    FOLLOW_UP,
    Journal,
    Plan,
    Polls,
    Target,
    Watch,
    follow_up,
    parse_plan,
    record_target,
)
from earnings_bot.sec_results import SixKResults
from earnings_bot.source_audit import audit_sources
from earnings_bot.sources import EdgarFilings, Sighting, Source
from earnings_bot.summary import Summarizer
from earnings_bot.track_progress import TrackProgress
from earnings_bot.version import release_version

SCAN_SECONDS = 3.0


@dataclass
class _Schedule:
    data: bytes
    plan: Plan | None
    problem: str | None


@dataclass
class _Window:
    target: Target
    task: asyncio.Task[None]


def _expected(target: Target) -> datetime:
    return target.expected


def _period(target: Target) -> str:
    return f" {period_label(target.period)}" if target.period else ""


class DirectoryWatcher:
    def __init__(
        self,
        directory: Path,
        fetcher: Fetcher,
        journal: Journal,
        releases: Releases,
        store: DeliveryStore,
        status: Callable[[str], None],
        benchmark: EarningsBenchmark | None = None,
        fmp_api_key: str | None = None,
    ) -> None:
        self.directory = directory
        self.fetcher = fetcher
        self.journal = journal
        self.sixk = SixKResults(fetcher)
        self.polls = Polls(fetcher, journal, self.sixk)
        self.releases = releases
        self.store = store
        self.calendar = Calendar(store)
        self.planned: dict[str, Target] = {}
        self.dates = DateDiscovery(
            self.calendar, fetcher, journal.now, self.busy, status, fmp_api_key, sixk=self.sixk
        )
        self.issuers = Issuers(fetcher)
        self.status = status
        self.benchmark = benchmark
        # How long after a window to keep looking for the release's other links; none unless set
        self.follow_up = timedelta(0)
        self.latest_benchmark: str | None = None
        self.progress: TrackProgress | None = None
        self.guild_id: int | None = None
        self.schedules: dict[Path, _Schedule] = {}
        self.windows: dict[str, _Window] = {}
        self.problems: dict[str, str] = {}
        self.last_scan: datetime | None = None
        self.since = store.last_scan() or journal.now()
        self.closed: set[tuple[str, datetime]] = set()

    def open_windows(self) -> Iterator[tuple[datetime, datetime]]:
        """The windows of the targets being watched, for keep_warm."""
        return (w.target.window for w in self.windows.values() if not w.task.done())

    def busy(self) -> bool:
        now = self.journal.now()
        return any(
            target.window[0] - timedelta(minutes=2) <= now <= target.window[1]
            for target in self.planned.values()
        )

    def health(self) -> str:
        now = self.journal.now()
        running = [w.target for w in self.windows.values() if not w.task.done()]
        live = [t.ticker for t in running if t.window[0] <= now < t.window[1]]
        upcoming = [t for t in running if t.window[0] > now]
        next_window = min(upcoming, key=lambda t: t.window[0], default=None)
        lines = [
            f"🟢 **Watching** `{self.directory.name}/` · live now: {', '.join(live) or 'none'}"
        ]
        if next_window:
            start = next_window.window[0]
            lines.append(
                f"⏭️ Next: **{next_window.ticker}** from {day(start)}, {clock(start)} "
                f"({stamp(start, 'R')})"
            )
        if self.last_scan:
            lines.append(f"🕑 Last schedule scan {clock(self.last_scan, seconds=True)}")
        if self.problems:
            lines.append(f"⚠️ {len(self.problems)} problem(s). See the status channel.")
        if self.latest_benchmark:
            lines.append(f"🏁 {self.latest_benchmark}")
        return "\n".join(lines)

    def health_embed(self, version: str, revision: str | None) -> discord.Embed:
        """/watch_status: what's live, what's next with its expected time and its watch, and
        anything wrong."""
        now = self.journal.now()
        running = [w.target for w in self.windows.values() if not w.task.done()]
        live = sorted((t for t in running if t.window[0] <= now < t.window[1]), key=_expected)
        coming = sorted((t for t in running if t.window[0] > now), key=lambda t: t.window[0])
        color = AMBER if self.problems else GREEN
        embed = discord.Embed(title=f"Earnings Bot {version}", color=color)
        scan = f" · last scan {clock(self.last_scan, seconds=True)}" if self.last_scan else ""
        embed.description = f"🟢 Watching `{self.directory.name}/`{scan}"
        if live:
            lines = [
                f"**{t.ticker}**{_period(t)} · results expected {clock(t.expected)} · "
                f"watching until {clock(t.window[1])}"
                for t in live
            ]
            embed.add_field(name="🔴 Live now", value=truncate("\n".join(lines)), inline=False)
        lines = [
            f"**{t.ticker}**{_period(t)} · {day(t.expected)}, results about {clock(t.expected)}\n"
            f"-# Watch opens {clock(t.window[0])} ({stamp(t.window[0], 'R')})"
            for t in coming[:5]
        ]
        embed.add_field(
            name="⏭️ Next windows",
            value=truncate("\n".join(lines)) if lines else "None scheduled",
            inline=False,
        )
        if self.problems:
            problems = [f"**{name}**: {message}" for name, message in self.problems.items()]
            embed.add_field(name="⚠️ Problems", value=truncate("\n".join(problems)), inline=False)
        if self.latest_benchmark:
            embed.add_field(name="🏁 Latest benchmark", value=self.latest_benchmark, inline=False)
        if revision:
            embed.set_footer(text=f"Build {revision}")
        return embed

    def _learn_wire(self, target: Target, watches: list[Watch]) -> None:
        """Keep the wire a live release came out on, for a tracked company whose research
        couldn't find it."""
        row = self.calendar.find(target.ticker)
        if row is None or row.company.wire != "unknown":
            return
        seen = sorted(
            (w for w in watches if w.first or w.caught_up),
            key=lambda w: w.first or datetime.max.replace(tzinfo=UTC),
        )
        if found := wire_seen([w.source.name for w in seen]):
            self.calendar.save_profile(row.cik, with_wire(row.company, found))
            self.journal.say(f"{target.ticker}: its releases go out on {found}")

    def _problem(self, name: str, message: str | None) -> None:
        previous = self.problems.get(name)
        if message is not None:
            if message != previous:
                self.status(f"⚠️ **{name}**: {message}")
            self.problems[name] = message
        elif previous is not None:
            del self.problems[name]
            self.status(f"✅ **{name}** recovered")

    def _targets(self) -> dict[str, Target] | None:
        try:
            paths = sorted(path for path in self.directory.iterdir() if path.suffix == ".toml")
        except OSError as error:
            self._problem("Schedule directory", str(error))
            return None
        self._problem("Schedule directory", None)

        for path in self.schedules.keys() - set(paths):
            del self.schedules[path]
            self._problem(path.name, None)
        targets: dict[str, Target] = {}
        duplicates: set[str] = set()
        for path in paths:
            current = self.schedules.get(path)
            try:
                data = path.read_bytes()
            except OSError as error:
                self._problem(path.name, str(error))
            else:
                if current is None or data != current.data:
                    try:
                        plan = parse_plan(data)
                    except (UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
                        current = _Schedule(data, current.plan if current else None, str(error))
                    else:
                        current = _Schedule(data, plan, None)
                    self.schedules[path] = current
                self._problem(path.name, current.problem)
            if current and current.plan:
                for target in current.plan.target:
                    existing = targets.setdefault(target.release_key, target)
                    if existing != target:
                        conflict = f"Duplicate {target.release_key}"
                        duplicates.add(conflict)
                        self._problem(
                            conflict,
                            f"{path.name} also defines {target.ticker}. Using the first schedule.",
                        )
        for name in set(self.problems) - duplicates:
            if name.startswith("Duplicate "):
                self._problem(name, None)
        return targets

    async def scan(self) -> None:
        targets = self._targets()
        if targets is None:
            return
        targets = self.calendar.plan(targets)
        self.planned = targets
        now = self.journal.now()
        for key, window in list(self.windows.items()):
            if window.target != targets.get(key) or window.task.done():
                if not window.task.done():
                    window.task.cancel()
                    await asyncio.gather(window.task, return_exceptions=True)
                    self.status(f"🔁 **{window.target.ticker}**: schedule changed or removed")
                del self.windows[key]

        for key, target in targets.items():
            if key in self.windows:
                continue
            token = (key, target.expected)
            if token in self.closed or self.store.window_closed(*token):
                self.closed.add(token)
                continue
            if target.window[1] <= now:
                if target.window[1] >= self.since:
                    saved = self.store.get(key)
                    outcome = (
                        "the alert was posted"
                        if saved and saved.message_id
                        else "no completed watch was recorded"
                    )
                    self.status(
                        f"⌛ **{target.ticker}**: window ended while the bot was off, and {outcome}"
                    )
                    self.store.close_window(*token)
                self.closed.add(token)
                continue
            self.windows[key] = _Window(target, asyncio.create_task(self._watch(target)))
        self.last_scan = now
        self.store.scanned(now)
        # Keep a just-opened or imminently opening IR source clear of browser teardown.
        browser_horizon = self.journal.now() + timedelta(seconds=SCAN_SECONDS)
        if not any(
            not window.task.done()
            and window.target.window[0] <= browser_horizon
            and window.target.ir is not None
            and (window.target.ir.listing or window.target.ir.rss or window.target.ir.pdfs)
            for window in self.windows.values()
        ):
            await self.fetcher.close_browser()

    async def _watch(self, target: Target) -> None:
        # The next scan retries a failed window within seconds, so a lasting failure is
        # reported once, not every 3 s
        problem = f"{target.ticker} monitoring"
        trial_reads: dict[str, asyncio.Task[None]] = {}
        trial_text = ReleaseText(self.fetcher, verified_only=True)

        def on_new(target: Target, source: Source, sighting: Sighting) -> None:
            measure = self.calendar.contains(target.cik)
            self.releases.on_new(target, source, sighting, measure=measure)
            if (
                measure
                and source.trial
                and not target.trial.post
                and sighting.earnings
                and not sighting.caught_up
                and source.name not in trial_reads
            ):
                trial_reads[source.name] = asyncio.create_task(
                    self._measure_trial(target, source, sighting, trial_text)
                )

        benchmark = None
        if self.benchmark:
            benchmark = asyncio.create_task(self.benchmark.watch(target.ticker, *target.window))
        try:
            watches = await record_target(target, self.fetcher, self.journal, on_new, self.polls)
            self._problem(problem, None)
            self.store.close_window(target.release_key, target.expected)
            self.dates.changed.set()
            comparison = ""
            if benchmark is not None:
                comparison = await self._compare(target, watches, benchmark)
            self.status(_window_report(target, watches, comparison))
            self._learn_wire(target, watches)
            release = self.releases.active.get(target.release_key)
            if self.follow_up and release is not None and release.posted:
                await follow_up(
                    target, watches, self.fetcher, self.journal, on_new, self.sixk, self.follow_up
                )
            await self.releases.finish_target(target)
            await asyncio.gather(*trial_reads.values(), return_exceptions=True)
            if self.calendar.contains(target.cik):
                self.calendar.clear_review(target.cik)
                if self.progress:
                    self.progress.next_attempt.pop(target.cik, None)
                    self.progress.changed.set()
        except Exception as error:
            self.journal.write("error", ticker=target.ticker, message=repr(error))
            self._problem(problem, f"failed and will retry: {error!r}")
        finally:
            for read in trial_reads.values():
                if not read.done():
                    read.cancel()
            await asyncio.gather(*trial_reads.values(), return_exceptions=True)
            if benchmark is not None:
                benchmark.cancel()
                await asyncio.gather(benchmark, return_exceptions=True)

    async def _measure_trial(
        self, target: Target, source: Source, sighting: Sighting, text: ReleaseText
    ) -> None:
        if await text.read(source, sighting):
            self.journal.write(
                "source_body",
                ticker=target.ticker,
                source=source.name,
                key=sighting.key,
                url=sighting.url,
            )

    async def _compare(
        self, target: Target, watches: list[Watch], task: asyncio.Task[BenchmarkResult]
    ) -> str:
        try:
            result = await asyncio.wait_for(task, timeout=30)
        except Exception as error:
            self.journal.write("benchmark_error", ticker=target.ticker, message=repr(error))
            result = BenchmarkResult("unavailable")
        delivered = self.store.get(target.release_key)
        alert = (
            discord.utils.snowflake_time(delivered.message_id)
            if delivered and delivered.message_id is not None
            else None
        )
        release = self.releases.active.get(target.release_key)
        first_text = release.first_text_at if release else None
        first_seen = min((w.first for w in watches if w.first), default=None)
        post = result.post
        self.journal.write(
            "benchmark",
            ticker=target.ticker,
            period=target.period,
            account="earnings_guy",
            state=result.state,
            x_post=post.url if post else None,
            x_posted=post.posted if post else None,
            first_seen=first_seen,
            first_text=first_text,
            discord_alert=alert,
        )
        outcome = _benchmark_report(result, alert)
        self.latest_benchmark = f"{target.ticker}: {outcome}"
        return outcome

    async def run(self) -> None:
        dates = asyncio.create_task(self.dates.run())
        try:
            while True:
                await self.scan()
                await asyncio.sleep(SCAN_SECONDS)
        finally:
            dates.cancel()
            await asyncio.gather(dates, return_exceptions=True)
            for window in self.windows.values():
                window.task.cancel()
            await asyncio.gather(
                *(window.task for window in self.windows.values()), return_exceptions=True
            )
            await self.releases.finish()


def _benchmark_report(result: BenchmarkResult, alert: datetime | None) -> str:
    if result.post is None:
        state = {
            "no_post_found": "no post found",
            "unavailable": "lookup unavailable",
            "unverified": "candidate could not be verified",
        }
        return f"@earnings_guy: {state.get(result.state, 'lookup unavailable')}"
    post = result.post
    text = f"[@earnings_guy](<{post.url}>) posted at {clock(post.posted, millis=True)}"
    if alert:
        delta = (alert - post.posted).total_seconds()
        if abs(delta) < 0.005:
            text += ", the same moment as our alert"
        else:
            direction = "after" if delta < 0 else "before"
            text += f", {abs(delta):.2f}s {direction} our alert"
    else:
        text += ". No Discord alert was recorded"
    return text


def _window_report(target: Target, watches: list[Watch], comparison: str = "") -> str:
    def label(watch: Watch) -> str:
        form = watch.source.form if isinstance(watch.source, EdgarFilings) else "8-K"
        return source_label(watch.source.name, form) + (" (trial)" if watch.source.trial else "")

    period = f" {period_label(target.period)}" if target.period else ""
    lines = [f"🏁 **{target.ticker}{period} window closed**"]
    seen: dict[str, datetime] = {}
    for at, name in sorted((watch.first, label(watch)) for watch in watches if watch.first):
        seen.setdefault(name, at)
    if seen:
        (winner, first), *rest = seen.items()
        lines.append(f"🥇 {winner} at {clock(first, millis=True)}")
        if rest:
            behind = (f"{name} +{duration((at - first).total_seconds())}" for name, at in rest)
            lines.append("Then " + " · ".join(behind))
    else:
        caught_up = list(dict.fromkeys(label(w) for w in watches if w.caught_up))
        lines.append(
            "Already out when the window opened: " + ", ".join(caught_up)
            if caught_up
            else "No release seen"
        )
    if stopped := list(dict.fromkeys(label(w) for w in watches if w.stopped)):
        lines.append(f"⚠️ Stopped early: {', '.join(stopped)}")
    if comparison:
        lines.append(f"-# {comparison}")
    return "\n".join(lines)


async def watch_directory(
    directory: Path,
    channel: discord.abc.Messageable,
    channel_id: int,
    author_id: int,
    sec_user_agent: str,
    summarizer: Summarizer | None,
    fmp_api_key: str | None,
    status_channel: discord.abc.Messageable | None,
    ready: Callable[[DirectoryWatcher], None],
    ping: Callable[[], Awaitable[object]] | None = None,
) -> None:
    progress_channel = status_channel or channel
    progress_id = getattr(progress_channel, "id", channel_id)
    with (
        DeliveryStore(directory / ".tracking-progress.sqlite3", progress_id) as progress_store,
        (directory / "watch.jsonl").open("a", encoding="utf-8") as file,
        DeliveryStore(directory / ".earnings-state.sqlite3", channel_id) as store,
    ):
        journal = Journal(file)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def status(message: str) -> None:
            journal.write("status", message=message)
            journal.say(message)
            if status_channel is not None:
                queue.put_nowait(message)

        async def report(channel: discord.abc.Messageable) -> None:
            while True:
                message = await queue.get()
                try:
                    await channel.send(
                        message[:1900], allowed_mentions=discord.AllowedMentions.none()
                    )
                except (aiohttp.ClientError, discord.HTTPException, TimeoutError) as error:
                    journal.say(f"Status post failed: {error!r}")

        async def recover_pending() -> None:
            for pending in store.pending():
                poster = DurableDiscordPoster(channel, store, pending.key, author_id)
                try:
                    await retry_delivery(partial(poster.send, pending.content))
                except (aiohttp.ClientError, discord.HTTPException, TimeoutError) as error:
                    status(f"⚠️ Pending alert {pending.key} needs attention: {error!r}")
                else:
                    status(f"✅ Recovered pending alert {pending.key}")

        async with Fetcher(sec_user_agent) as fetcher:
            releases = Releases(
                lambda: DiscordPoster(channel),
                fetcher,
                journal,
                summarizer,
                fmp_api_key,
                lambda target: DurableDiscordPoster(channel, store, target.release_key, author_id),
            )
            benchmark = EarningsBenchmark(fetcher, journal.now)
            watcher = DirectoryWatcher(
                directory, fetcher, journal, releases, store, status, benchmark, fmp_api_key
            )
            watcher.follow_up = FOLLOW_UP
            if isinstance(channel, discord.abc.GuildChannel | discord.Thread):
                watcher.guild_id = channel.guild.id
            watcher.progress = TrackProgress(
                watcher.calendar,
                progress_store,
                progress_channel,
                author_id,
                lambda: watcher.planned,
                journal.now,
                watcher.busy,
                partial(audit_sources, fetcher, directory, benchmark=benchmark, sixk=watcher.sixk),
                date_phase=watcher.dates.phases.get,
                research=watcher.dates.reports.get,
                ir_note=watcher.dates.ir_notes.get,
                cache_notes=watcher.dates.cache_notes.get,
            )
            watcher.dates.on_progress = watcher.progress.changed.set
            watcher.progress.follow_calendar()
            ready(watcher)
            status(
                f"🟢 **Earnings monitoring started** · {release_version()} · `{directory.name}/`"
            )
            tasks = [
                asyncio.create_task(recover_pending()),
                asyncio.create_task(watcher.progress.run()),
            ]
            if status_channel is not None:
                tasks.append(asyncio.create_task(report(status_channel)))
            if ping is not None:
                tasks.append(asyncio.create_task(keep_warm(ping, watcher.open_windows, journal)))
            try:
                await watcher.run()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
