"""Watch a changing schedule directory with one polling and delivery coordinator."""

import asyncio
import tomllib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from earnings_bot.pipeline import DiscordPoster, DurableDiscordPoster, Releases, keep_warm
from earnings_bot.recorder import ET, Journal, Plan, Polls, Target, Watch, record_target
from earnings_bot.source_audit import audit_sources
from earnings_bot.sources import Sighting, Source
from earnings_bot.summary import Summarizer
from earnings_bot.text import release_text, verified_report
from earnings_bot.track_progress import TrackProgress

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
    ) -> None:
        self.directory = directory
        self.fetcher = fetcher
        self.journal = journal
        self.polls = Polls(fetcher, journal)
        self.releases = releases
        self.store = store
        self.calendar = Calendar(store)
        self.planned: dict[str, Target] = {}
        self.dates = DateDiscovery(self.calendar, fetcher, journal.now, self.busy, journal.say)
        self.status = status
        self.benchmark = benchmark
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
        lines = [f"Watching {self.directory.name}/. Active: {', '.join(live) or 'none'}."]
        if next_window:
            start = next_window.window[0].astimezone(ET)
            lines.append(f"Next: {next_window.ticker} at {start:%b %d %H:%M} ET.")
        if self.last_scan:
            lines.append(f"Last scan: {self.last_scan.astimezone(ET):%H:%M:%S} ET.")
        if self.problems:
            lines.append(f"Problems: {len(self.problems)}. See the status log.")
        if self.latest_benchmark:
            lines.append(self.latest_benchmark)
        return "\n".join(lines)

    def _problem(self, name: str, message: str | None) -> None:
        previous = self.problems.get(name)
        if message is not None:
            if message != previous:
                self.status(f"{name}: {message}")
            self.problems[name] = message
        elif previous is not None:
            del self.problems[name]
            self.status(f"{name}: recovered")

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
                        plan = Plan.model_validate(tomllib.loads(data.decode("utf-8-sig")))
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
                    self.status(f"{window.target.ticker}: schedule changed or removed")
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
                    self.status(f"{target.ticker}: window expired and {outcome}")
                    self.store.close_window(*token)
                self.closed.add(token)
                continue
            self.windows[key] = _Window(target, asyncio.create_task(self._watch(target)))
        self.last_scan = now
        self.store.scanned(now)
        # Keep a just-opened or imminently opening trial clear of browser teardown.
        browser_horizon = self.journal.now() + timedelta(seconds=SCAN_SECONDS)
        if not any(
            not window.task.done()
            and window.target.window[0] <= browser_horizon
            and window.target.trial.ir is not None
            and (window.target.trial.ir.listing or window.target.trial.ir.pdfs)
            for window in self.windows.values()
        ):
            await self.fetcher.close_browser()

    async def _watch(self, target: Target) -> None:
        # The next scan retries a failed window within seconds, so a lasting failure is
        # reported once, not every 3 s
        problem = f"{target.ticker} monitoring"
        trial_reads: dict[str, asyncio.Task[None]] = {}

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
                    self._measure_trial(target, source, sighting)
                )

        benchmark = None
        if self.benchmark:
            benchmark = asyncio.create_task(self.benchmark.watch(target.ticker, *target.window))
        try:
            watches = await record_target(target, self.fetcher, self.journal, on_new, self.polls)
            self._problem(problem, None)
            self.store.close_window(target.release_key, target.expected)
            comparison = ""
            if benchmark is not None:
                comparison = await self._compare(target, watches, benchmark)
            self.status(_window_report(target, watches, comparison))
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

    async def _measure_trial(self, target: Target, source: Source, sighting: Sighting) -> None:
        if verified_report(await release_text(self.fetcher, source, sighting)):
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
        return f"@earnings_guy: {state.get(result.state, 'lookup unavailable')}."
    post = result.post
    local = f"{post.posted.astimezone(ET):%H:%M:%S.%f}"[:-3]
    text = f"@earnings_guy posted at {local} ET"
    if alert:
        delta = (alert - post.posted).total_seconds()
        if abs(delta) < 0.005:
            text += ". Our Discord alert tied"
        else:
            direction = "earlier" if delta < 0 else "later"
            text += f". Our Discord alert was {abs(delta):.2f}s {direction}"
    else:
        text += ". No Discord alert recorded"
    return f"{text}. <{post.url}>"


def _window_report(target: Target, watches: list[Watch], comparison: str = "") -> str:
    def label(watch: Watch) -> str:
        return watch.source.name + (" (trial)" if watch.source.trial else "")

    seen = sorted(
        ((watch.first, label(watch)) for watch in watches if watch.first),
    )
    if seen:
        stamp, winner = seen[0]
        behind = ", ".join(f"{name} +{(at - stamp).total_seconds():.2f}s" for at, name in seen[1:])
        local = f"{stamp.astimezone(ET):%H:%M:%S.%f}"[:-3]
        outcome = f"{winner} first at {local} ET"
        if behind:
            outcome += f". {behind}"
    else:
        caught_up = ", ".join(label(w) for w in watches if w.caught_up)
        outcome = f"already out on first poll ({caught_up})" if caught_up else "no release seen"
    stopped = ", ".join(label(w) for w in watches if w.stopped)
    if stopped:
        outcome += f". Stopped sources: {stopped}"
    heading = f"{target.ticker} window closed."
    if comparison:
        heading += f" {comparison}"
    return f"{heading} {outcome}."


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
                    status(f"Pending alert {pending.key} needs attention: {error!r}")
                else:
                    status(f"Recovered pending alert {pending.key}.")

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
                directory, fetcher, journal, releases, store, status, benchmark
            )
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
                partial(audit_sources, fetcher, directory, benchmark=benchmark),
            )
            ready(watcher)
            status(f"Earnings monitoring started. Reading {directory.name}/.")
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
