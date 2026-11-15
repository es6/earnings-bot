"""Repeat the real recorder/queue/pipeline fixture replay in one process, without network.

    uv run python -m tests.soak_replay --hours 4 --out recordings/soak --keep-rounds

HTTP, Discord and publication are simulated. Chrome and model clients are not used. This
does not measure publisher freshness, real Discord rate limits or sustained browser load.
"""

# Artifact I/O is between replay windows; keeping it here avoids adding observer threads.
# ruff: noqa: ASYNC240

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from earnings_bot.resources import snapshot
from tests.burst_replay import Settings, replay


def handles() -> int | None:
    if sys.platform != "win32":
        return None
    count = ctypes.c_ulong()
    process = ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess())
    return (
        count.value
        if ctypes.windll.kernel32.GetProcessHandleCount(process, ctypes.byref(count))
        else None
    )


@dataclass
class Baseline:
    private_mb: int | None
    tasks: int
    threads: int
    handles: int | None


def failures(result: dict[str, Any], used: dict[str, Any], baseline: Baseline | None) -> list[str]:
    reasons = []
    if result["missed_alerts"] or result["duplicate_alerts"] or result["no_body"]:
        reasons.append("delivery invariant")
    if result["sec"]["most_in_any_second"] > 8:
        reasons.append("SEC pacing")
    if baseline:
        private = used.get("bot_private_mb")
        if (
            private is not None
            and baseline.private_mb is not None
            and private > baseline.private_mb + 256
        ):
            reasons.append("private memory growth exceeded 256 MiB")
        if used["tasks"] > baseline.tasks + 8 or used["threads"] > baseline.threads + 8:
            reasons.append("task/thread growth exceeded eight")
        count = used.get("handles")
        if count is not None and baseline.handles is not None and count > baseline.handles + 128:
            reasons.append("handle growth exceeded 128")
    return reasons


def write(folder: Path, record: dict[str, Any]) -> None:
    with (folder / "soak.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"utc": datetime.now(UTC).isoformat(), **record}) + "\n")


async def run(folder: Path, hours: float, iterations: int | None, tickers: int, keep: bool) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    # A new directory for a new run keeps failed and successful runs reviewable.
    with (folder / "soak.jsonl").open("x", encoding="utf-8"):
        pass
    root = Path(__file__).resolve().parents[1]
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "src/earnings_bot/fetch.py",
            "src/earnings_bot/recorder.py",
            "src/earnings_bot/pipeline.py",
            "src/earnings_bot/text.py",
            "tests/burst_replay.py",
        )
    }
    write(
        folder,
        {
            "event": "started",
            "duration_seconds": hours * 3600,
            "iterations_limit": iterations,
            "tickers": tickers,
            "source_sha256": hashes,
            "scope": (
                "real recorder/Fetcher queues/pipeline; simulated HTTP/Discord; no Chrome or model"
            ),
        },
    )
    started = time.monotonic()
    count = 0
    baseline = None
    try:
        while time.monotonic() - started < hours * 3600 and (
            iterations is None or count < iterations
        ):
            result = await replay(Settings(tickers=tickers, seed=count + 1))
            count += 1
            gc.collect()
            used = snapshot([]) | {
                "tasks": len(asyncio.all_tasks()),
                "threads": threading.active_count(),
                "handles": handles(),
            }
            if count == 5:
                baseline = Baseline(
                    used.get("bot_private_mb"), used["tasks"], used["threads"], used["handles"]
                )
            reasons = failures(result, used, baseline)
            write(
                folder,
                {
                    "event": "iteration",
                    "iteration": count,
                    "elapsed_seconds": time.monotonic() - started,
                    "resources": used,
                    "missed": result["missed_alerts"],
                    "duplicates": result["duplicate_alerts"],
                    "no_body": result["no_body"],
                    "sec_max_per_second": result["sec"]["most_in_any_second"],
                    "loop_lag": result["loop_lag"],
                    "alert": result["alert"],
                    "body": result["body"],
                    "failures": reasons,
                },
            )
            if keep:
                (folder / f"round-{count:04d}.json").write_text(
                    json.dumps(result), encoding="utf-8"
                )
            if reasons:
                raise RuntimeError(", ".join(reasons))
        write(
            folder,
            {
                "event": "finished",
                "iterations": count,
                "elapsed_seconds": time.monotonic() - started,
            },
        )
    except Exception as error:
        write(folder, {"event": "failed", "type": type(error).__name__})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=4)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--tickers", type=int, default=20)
    parser.add_argument("--keep-rounds", action="store_true")
    args = parser.parse_args()
    if (
        not 0 < args.hours <= 24
        or not 1 <= args.tickers <= 100
        or (args.iterations is not None and args.iterations < 1)
    ):
        parser.error("Use 0<hours<=24, 1<=tickers<=100 and a positive iteration limit")
    asyncio.run(run(args.out, args.hours, args.iterations, args.tickers, args.keep_rounds))


if __name__ == "__main__":
    main()
