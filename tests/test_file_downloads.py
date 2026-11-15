import asyncio

import pytest

from earnings_bot.fetch import Response
from earnings_bot.file_downloads import FileDownloads
from tests.fakes import GatedRequest

URL = "https://ir.example.com/release.pdf"
PDF = Response(URL, 200, {}, b"%PDF-complete-report", 0.01)


async def test_concurrent_readers_share_one_fetch_without_sharing_cancellation() -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        request = GatedRequest(PDF)
        first = asyncio.create_task(downloads.get("release.pdf", request))
        await request.started.wait()
        readers = [asyncio.create_task(downloads.get("release.pdf", request)) for _ in range(2)]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        request.release.set()
        assert await asyncio.gather(*readers) == [PDF, PDF]
        assert await downloads.get("release.pdf", request) == PDF
        await downloads.finish()
        assert request.calls == 1


async def test_verified_poll_bytes_release_all_readers_and_cancel_the_obsolete_fetch() -> None:
    async with asyncio.timeout(5):
        downloads = FileDownloads()
        request = GatedRequest(PDF)
        waiting = asyncio.create_task(downloads.get("release.pdf", request))
        await request.started.wait()
        assert await downloads.get("release.pdf", request, ready=PDF) == PDF
        assert await waiting == PDF
        await downloads.finish()
        assert request.finished.is_set() and not request.release.is_set()
        assert request.calls == 1


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
        request = GatedRequest(PDF)
        reader = asyncio.create_task(downloads.get("release.pdf", request))
        await request.started.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        finish = asyncio.create_task(downloads.finish())
        await asyncio.sleep(0)
        finish.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish
        assert request.finished.is_set() and not request.release.is_set()


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
