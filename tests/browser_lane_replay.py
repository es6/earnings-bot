"""Local browser-worker contention replay; no browser process or public requests.

The real DesktopChrome request/lock code and Fetcher pacing run against simulated warm
tabs. Listing answers take 1.6 seconds and articles 50 ms. Compare a one-tab control
with the urgent tab. This measures worker contention, not publisher freshness.
"""

import argparse
import asyncio
import json
import statistics
import time
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import patch

from earnings_bot import browser
from earnings_bot.browser import DesktopChrome, _Tab
from earnings_bot.fetch import Fetcher
from tests.test_browser import FakeTabSocket


async def sample(separate: bool) -> dict[str, Any]:
    chrome = DesktopChrome()
    host = "ir.example.com"
    chrome._tabs[host] = _Tab("listing", FakeTabSocket())  # type: ignore[arg-type]
    chrome._tabs[host + "#urgent"] = _Tab("urgent", FakeTabSocket())  # type: ignore[arg-type]
    fetcher = Fetcher("Local offline replay")
    turns: list[float] = []

    async def started() -> None:
        pass

    async def evaluate(socket: object, expression: str) -> dict[str, Any]:
        await asyncio.sleep(0.05 if "/article" in expression else 1.6)
        return {"status": 200, "headers": {}, "body": ""}

    async def turn(asked: str, urgent: bool = False) -> None:
        await fetcher.wait_turn(asked, urgent)
        turns.append(time.monotonic())

    with patch.object(chrome, "_start", started), patch.object(browser, "_evaluate", evaluate):
        listing = asyncio.create_task(
            chrome.request(f"https://{host}/listing", f"https://{host}/", 3, turn)
        )
        await asyncio.sleep(0.02)
        begun = time.monotonic()
        article = await chrome.request(
            f"https://{host}/article",
            f"https://{host}/",
            3,
            lambda asked: turn(asked, True),
            urgent=separate,
        )
        seconds = time.monotonic() - begun
        await listing
    gaps = [b - a for a, b in pairwise(turns)]
    assert len(turns) == 2 and all(gap >= 1.0 for gap in gaps)
    return {"article_seconds": seconds, "turn_gaps": gaps, "waits": article.waits}


async def replay() -> dict[str, Any]:
    results = {}
    for name, separate in (("one_tab_control", False), ("urgent_tab", True)):
        rows = [await sample(separate) for _ in range(3)]
        results[name] = {
            "runs": rows,
            "median_article_seconds": statistics.median(r["article_seconds"] for r in rows),
        }
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(replay())
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({name: row["median_article_seconds"] for name, row in result.items()}))
