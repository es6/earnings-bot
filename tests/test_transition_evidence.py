"""Raw bodies distinguish publication/filtering failures; evidence never blocks delivery."""

import hashlib
import io
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from first_print.browser_sources import HttpRelease
from first_print.fetch import Response
from first_print.recorder import Journal, Watch, poll, take
from first_print.sources import RssFeed, Sighting
from first_print.transition_evidence import TransitionEvidence
from tests.fakes import PinnedClock, RoutedFetcher
from tests.test_independent_newsrooms import ARTICLE, BODY, LISTING, TITLE, source


def journal(root: Path, **kwargs: object) -> tuple[Journal, TransitionEvidence]:
    evidence = TransitionEvidence(root, **kwargs)  # type: ignore[arg-type]
    return Journal(io.StringIO(), PinnedClock(), evidence=evidence), evidence


def watch(log: Journal, source: RssFeed | HttpRelease) -> Watch:
    return Watch(
        "TEST",
        source,
        (log.now() - timedelta(minutes=1), log.now() + timedelta(minutes=1)),
        period="FY2026Q3",
    )


def used_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.iterdir() if p.suffix != ".txt")


def manifests(root: Path, slot: str) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in root.glob(f"*.{slot}.json")]


async def test_last_negative_and_first_positive_include_raw_body_parser_ids_and_cache_headers(
    tmp_path: Path,
) -> None:
    log, evidence = journal(tmp_path)
    feed = RssFeed("issuer", "https://issuer.example/feed")
    w = watch(log, feed)
    sent = log.now()
    for body in (b"older negative", b"last negative"):
        take(
            w,
            sent,
            Response(feed.url(), 200, {"Age": "8", "Set-Cookie": "private-cookie"}, body, 0.01),
            log,
            items=[],
        )
    item = Sighting(
        "release",
        "Example reports third quarter 2026 results",
        "https://issuer.example/q3",
        earnings=True,
    )
    positive = Response(feed.url(), 200, {"x-cache-hits": "0"}, b"first positive", 0.01)
    take(w, sent, positive, log, items=[item])
    take(w, sent, Response(feed.url(), 200, {}, b"later positive", 0.01), log, items=[item])
    await log.flush_evidence()

    [negative], [first] = manifests(tmp_path, "negative"), manifests(tmp_path, "positive")
    assert (tmp_path / negative["parts"][0]["file"]).read_bytes() == b"last negative"
    assert (tmp_path / first["parts"][0]["file"]).read_bytes() == b"first positive"
    assert first["parsed"][0]["key"] == "release" and first["matched"]
    assert not negative["matched"] and negative["parts"][0]["headers"] == {"age": "8"}
    assert first["parts"][0]["sha256"] == hashlib.sha256(b"first positive").hexdigest()
    assert not evidence.errors and "private-cookie" not in json.dumps([negative, first])


async def test_filtering_failure_keeps_a_successful_raw_feed_containing_the_unmatched_release(
    tmp_path: Path,
) -> None:
    log, _ = journal(tmp_path)
    feed = RssFeed("globenewswire", "https://rss.globenewswire.com/feed", ["Wrong Publisher"])
    w = watch(log, feed)
    body = (Path(__file__).parent / "fixtures/globe-wrd-20261120.xml").read_bytes()
    response = Response(feed.url(), 200, {}, body, 0.01)
    take(w, log.now(), response, log)
    await log.flush_evidence()

    [negative] = manifests(tmp_path, "negative")
    assert negative["parsed_count"] == 0 and not manifests(tmp_path, "positive")
    assert (tmp_path / negative["parts"][0]["file"]).read_bytes() == body


async def test_html_transition_keeps_the_listing_and_the_separate_article(tmp_path: Path) -> None:
    log, _ = journal(tmp_path)
    room = source()
    assert isinstance(room, HttpRelease)
    w = watch(log, room)
    fetcher = RoutedFetcher({LISTING: (200, b"no results yet"), ARTICLE: (200, BODY)})
    response, items = await poll(room, fetcher)
    take(w, log.now(), response, log, items=items)
    listing_body = f'<a href="{ARTICLE}">{TITLE}</a>'.encode()
    fetcher.routes[LISTING] = 200, listing_body
    response, items = await poll(room, fetcher)
    take(w, log.now(), response, log, items=items)
    await log.flush_evidence()

    [positive] = manifests(tmp_path, "positive")
    listing, article = positive["parts"]
    assert (tmp_path / listing["file"]).read_bytes() == listing_body
    assert (tmp_path / article["file"]).read_bytes() == BODY


async def test_http_ir_listing_body_is_retained_before_the_article_fetch(tmp_path: Path) -> None:
    log, _ = journal(tmp_path)
    listing, article = "https://issuer.example/news", "https://issuer.example/q3"
    title = "Example Reports Third Quarter 2026 Results"
    reader = HttpRelease(listing, title, chrome_fallback=False)
    raw = f'<a href="{article}">{title}</a>'.encode()
    body = f"<h1>{title}</h1>" + "<p>Details of the financial results.</p>" * 100
    fetcher = RoutedFetcher({article: (200, body.encode()), listing: (200, raw)})
    w = watch(log, reader)
    response, items = await poll(reader, fetcher)
    take(w, log.now(), response, log, items=items)
    await log.flush_evidence()

    [positive] = manifests(tmp_path, "positive")
    assert (tmp_path / positive["parts"][0]["file"]).read_bytes() == raw


async def test_disk_work_runs_after_the_alert_callback_on_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, evidence = journal(tmp_path)
    feed = RssFeed("issuer", "https://issuer.example/feed")
    w = watch(log, feed)
    w.known = set()
    alerted: list[str] = []
    main = threading.get_ident()
    original = evidence._write

    def write(snapshot: object) -> None:
        assert alerted and threading.get_ident() != main
        original(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(evidence, "_write", write)
    item = Sighting(
        "current",
        "Example Reports Third Quarter 2026 Results",
        "https://issuer.example/q3",
        earnings=True,
    )
    take(
        w,
        log.now(),
        Response(feed.url(), 200, {}, b"release", 0.01),
        log,
        lambda source, item: alerted.append(item.key),
        items=[item],
    )
    assert alerted == ["current"]
    await log.flush_evidence()
    assert manifests(tmp_path, "positive")


async def test_body_and_retention_limits_preserve_unrelated_files_and_redact_request_credentials(
    tmp_path: Path,
) -> None:
    log, evidence = journal(tmp_path, max_routes=1, max_body=4, max_bytes=5000, checkpoint=0)
    unrelated = tmp_path / "owner-notes.txt"
    unrelated.write_text("keep")
    for index in range(12):
        response = Response(
            "https://user:password@issuer.example/feed?api_key=private-key&a=%69",
            200,
            {},
            b"too large",
            0.01,
        )
        evidence.offer(str(index), {"matched": False}, response, positive=False)
        await evidence.flush()
    assert len(evidence.negative) <= 1
    assert used_bytes(tmp_path) <= 5000
    assert unrelated.read_text() == "keep"
    for saved in manifests(tmp_path, "negative"):
        part = saved["parts"][0]
        assert "omitted" in part and "file" not in part
        assert "private-key" not in part["url"] and "password" not in part["url"]
        assert "a=%69" in part["url"]
    await log.flush_evidence()


async def test_capture_failure_cannot_stop_alert_delivery_or_poll_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, evidence = journal(tmp_path)
    feed = RssFeed("issuer", "https://issuer.example/feed")
    w = watch(log, feed)
    w.known = set()
    posted: list[str] = []

    def failed(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(evidence, "offer", failed)
    item = Sighting(
        "release",
        "Example Reports Third Quarter 2026 Results",
        "https://issuer.example/q3",
        earnings=True,
    )
    take(
        w,
        log.now(),
        Response(feed.url(), 200, {}, b"release", 0.01),
        log,
        lambda source, item: posted.append(item.key),
        items=[item],
    )
    assert posted == ["release"] and w.previous is not None
    assert "source_evidence_error" in log.file.getvalue()  # type: ignore[attr-defined]


async def test_full_queue_and_background_write_failure_are_bounded_and_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log, evidence = journal(tmp_path, max_routes=1, checkpoint=0)

    def failed(snapshot: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(evidence, "_write", failed)
    response = Response("https://issuer.example/feed", 200, {}, b"body", 0.01)
    for index in range(20):
        evidence.offer(str(index), {"matched": False}, response, positive=False)
    assert evidence._queue.qsize() <= 2 and len(evidence.negative) <= 1
    await log.flush_evidence()
    assert evidence.dropped and evidence.errors == ["OSError", "OSError"]
    assert "source_evidence_incomplete" in log.file.getvalue()  # type: ignore[attr-defined]
