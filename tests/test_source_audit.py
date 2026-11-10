import json
from datetime import date, datetime
from pathlib import Path

from earnings_bot.known import known_companies
from earnings_bot.recorder import ET
from earnings_bot.source_audit import Speed, Timing, _archived_sources, _measurements


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
    speed = Speed(date(2026, 11, 5), measured_headlines=headlines, measured_text=bodies)
    basis, order = speed.fastest()
    assert basis == "seen live by the bot"
    assert [(t.source, t.at.second) for t in order] == [("ir", 1), ("wire", 2)]
    assert [t.source for t in sorted(bodies, key=lambda t: t.at)] == ["wire", "ir"]
    assert comparison == (
        "[@earnings_guy posted](https://x.com/earnings_guy/status/123), "
        "and our alert went out 2.00s before it"
    )
    assert not _measurements(tmp_path, "NET", date(2026, 8, 1))[0]


def test_the_archive_reads_one_copy_of_each_feed() -> None:
    now = datetime(2026, 11, 9, 16, tzinfo=ET)
    names = [source.name for source in _archived_sources("LLY", known_companies()["LLY"], now)]
    assert names == ["prnewswire"]


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-05T{clock}-05:00")


def test_live_sightings_beat_publisher_timestamps_and_timestamps_beat_nothing() -> None:
    archive = [Timing("SEC 8-K acceptance", _at("16:08:17")), Timing("q4_press", _at("16:05:00"))]
    live = [Timing("edgar_latest", _at("16:08:53")), Timing("prnewswire", _at("16:06:02"))]

    assert Speed(date(2026, 11, 5), archive=archive).fastest() == (
        "publishers' timestamps",
        [archive[1], archive[0]],
    )
    assert Speed(date(2026, 11, 5), archive=archive, measured_headlines=live).fastest() == (
        "seen live by the bot",
        [live[1], live[0]],
    )
    assert Speed(date(2026, 11, 5)).fastest() == ("", [])


def test_a_saved_speed_check_reads_back_and_an_old_one_doesnt() -> None:
    speed = Speed(
        date(2026, 11, 5),
        confident=True,
        form="6-K",
        measured_text=[Timing("prnewswire", _at("16:06:03"), "https://example.com/a")],
        measured_headlines=[Timing("prnewswire", _at("16:06:02"))],
        archive=[Timing("SEC 6-K acceptance", _at("16:30:00"))],
        verified=["edgar"],
        benchmark="[@earnings_guy posted](https://x.com/a)",
        ir_json="`GET /feed`",
    )
    assert Speed.from_json(speed.to_json()) == speed
    assert Speed.from_json(Speed(None).to_json()) == Speed(None)
    # What the audit saved before it kept its findings as JSON
    assert Speed.from_json("**Last release | Nov 05, 2026**\nMeasured headlines | ...") is None
    assert Speed.from_json(None) is None
    assert Speed.from_json('{"day": null}') is None
