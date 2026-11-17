"""One shared file result per release attachment, independent of Discord delivery."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from first_print.fetch import Response


@dataclass
class _File:
    result: asyncio.Future[Response]
    worker: asyncio.Task[None] | None = None


class FileDownloads:
    """Retain successful bytes, retry failures, and own workers beyond a reader's lifetime."""

    def __init__(self) -> None:
        self._files: dict[str, _File] = {}

    async def get(
        self,
        name: str,
        request: Callable[[], Awaitable[Response]],
        *,
        ready: Response | None = None,
    ) -> Response:
        file = self._files.get(name)
        if file is None:
            file = _File(asyncio.get_running_loop().create_future())
            self._files[name] = file
        if ready is not None:
            # A poll may already have the bytes while an earlier fetch is still waiting.
            if not file.result.done():
                file.result.set_result(ready)
            if file.worker is not None and not file.worker.done() and not file.worker.cancelling():
                file.worker.cancel()
        elif file.worker is None and not file.result.done():
            file.worker = asyncio.create_task(self._read(name, file, request))
        # Cancelling an attachment/summary reader must not cancel another reader's bytes.
        return await asyncio.shield(file.result)

    async def _read(
        self, name: str, file: _File, request: Callable[[], Awaitable[Response]]
    ) -> None:
        try:
            response = await request()
            if not file.result.done():
                file.result.set_result(response)
        except asyncio.CancelledError:
            if not file.result.done():
                file.result.cancel()
            raise
        except Exception as error:
            if not file.result.done():
                file.result.set_exception(error)
                file.result.exception()  # Observe failures even if every reader was cancelled.
        finally:
            failed = file.result.cancelled() or (
                file.result.done()
                and (file.result.exception() is not None or file.result.result().status != 200)
            )
            if failed and self._files.get(name) is file:
                del self._files[name]

    async def finish(self) -> None:
        try:
            await asyncio.gather(
                *(file.worker for file in self._files.values() if file.worker is not None),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            await self.cancel()
            raise

    async def cancel(self) -> None:
        files = tuple(self._files.values())
        for file in files:
            if not file.result.done():
                file.result.cancel()
            if file.worker is not None and not file.worker.done() and not file.worker.cancelling():
                file.worker.cancel()
        await asyncio.gather(
            *(file.worker for file in files if file.worker is not None),
            return_exceptions=True,
        )
        self._files = {
            name: file for name, file in self._files.items() if not file.result.cancelled()
        }
