import json
from datetime import date, datetime
from pathlib import Path

from earnings_bot.known import known_companies
from earnings_bot.recorder import ET
from earnings_bot.source_audit import _archived_sources, _measurements, _rank


def test_last_release_ranks_body_separately_from_headline(tmp_path: Path) -> None:
    def event(kind: str, clock: str, source: str, **fields: object) -> dict:
        stamp = f"2026-11-05T{clock}+00:00"
        return dict(event=kind, at=stamp, seen=stamp, source=source, ticker="NET", **fields)

    events = [
        event("new", "21:15:01", "ir", title="Third Quarter Earnings", earnings=True),
        event("new", "21:15:02", "wire", title="Third Quarter Earnings", earnings=True),
        event(
            "new", "21:14:58", "late", title="Third Quarter Earnings", earnings=True, caught_up=True
        ),
        event("source_body", "21:15:03", "wire"),
        event("source_body", "21:15:05", "ir"),
        event(
            "benchmark",
            "21:30:00",
            "",
            x_post="https://x.com/earnings_guy/status/123",
            x_posted="2026-11-05T21:15:04+00:00",
            discord_alert="2026-11-05T21:15:02+00:00",
        ),
    ]
    (tmp_path / "watch.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    (tmp_path / "replay-NET.jsonl").write_text(
        json.dumps(event("source_body", "21:14:00", "replay")), encoding="utf-8"
    )
    headlines, bodies, comparison = _measurements(tmp_path, "NET", date(2026, 11, 5))
    assert "ir +0.00s, wire +1.00s" in _rank("Headlines", headlines)
    assert "wire +0.00s, ir +2.00s" in _rank("Reports", bodies)
    assert "Discord 2.00s earlier" in comparison
    assert not _measurements(tmp_path, "NET", date(2026, 8, 1))[0]


def test_the_archive_reads_one_copy_of_each_feed() -> None:
    now = datetime(2026, 11, 9, 16, tzinfo=ET)
    names = [source.name for source in _archived_sources("LLY", known_companies()["LLY"], now)]
    assert names == ["prnewswire"]
