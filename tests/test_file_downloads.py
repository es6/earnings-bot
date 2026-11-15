import asyncio

import pytest

from earnings_bot.fetch import Response
from earnings_bot.file_downloads import FileDownloads

URL = "https://ir.example.com/release.pdf"
PDF = Response(URL, 200, {}, b"%PDF-complete-report", 0.01)


@pytest.mark.parametrize("cancel_reader", [False, True], ids=["shared", "cancelled-reader"])
async def test_concurrent_readers_share_one_fetch_without_sharing_cancellation(
    cancel_reader: bool,
) -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        started, complete = asyncio.Event(), asyncio.Event()
        calls = 0

        async def request() -> Response:
            nonlocal calls
            calls += 1
            started.set()
            await complete.wait()
            return PDF

        first = asyncio.create_task(downloads.get("release.pdf", request))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(downloads.get("release.pdf", request))
        if cancel_reader:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        complete.set()
        assert await second == PDF
        if not cancel_reader:
            assert await first == PDF
        assert await downloads.get("release.pdf", request) == PDF
        await downloads.finish()
        assert calls == 1


async def test_verified_poll_bytes_release_all_readers_and_cancel_the_obsolete_fetch() -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = 0

        async def stalled() -> Response:
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return PDF

        waiting = asyncio.create_task(downloads.get("release.pdf", stalled))
        await asyncio.wait_for(started.wait(), 1)
        assert await downloads.get("release.pdf", stalled, ready=PDF) == PDF
        assert await waiting == PDF
        assert await downloads.get("release.pdf", stalled, ready=PDF) == PDF
        await downloads.finish()
        assert cancelled.is_set() and calls == 1


@pytest.mark.parametrize("failure", ["exception", "404"])
async def test_a_failed_acquisition_can_be_retried(failure: str) -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        calls = 0

        async def request() -> Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                if failure == "exception":
                    raise OSError("connection lost")
                return Response(URL, 404, {}, b"", 0.01)
            return PDF

        if failure == "exception":
            with pytest.raises(OSError, match="connection lost"):
                await downloads.get("release.pdf", request)
        else:
            assert (await downloads.get("release.pdf", request)).status == 404
        assert await downloads.get("release.pdf", request) == PDF
        await downloads.finish()
        assert calls == 2


async def test_finish_cancellation_reaps_a_fetch_after_its_only_reader_was_cancelled() -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def stalled() -> Response:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return PDF

        reader = asyncio.create_task(downloads.get("release.pdf", stalled))
        await asyncio.wait_for(started.wait(), 1)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        finish = asyncio.create_task(downloads.finish())
        await asyncio.sleep(0)
        finish.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish
        assert cancelled.is_set()


async def test_cancelling_before_a_worker_starts_does_not_poison_a_later_retry() -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        calls = 0

        async def request() -> Response:
            nonlocal calls
            calls += 1
            return PDF

        reader = asyncio.create_task(downloads.get("release.pdf", request))
        await asyncio.sleep(0)
        await downloads.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert calls == 0
        assert await downloads.get("release.pdf", request) == PDF
        await downloads.finish()
        assert calls == 1
