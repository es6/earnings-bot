"""Release alerts go before scorecards in Discord.

Both live in one channel and share its edit limits. Discord also caps edits to messages more
than an hour old (error 30046), and days-old scorecards count against that cap. On Nov 12 a
restart's scorecard edits got 429 every few seconds for minutes, and NBIS's summary edit on its
resumed alert failed once with 30046 and landed 72 s late."""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Any


class CardDeferred(Exception):
    """A release step started while a scorecard request was out, so the card request was
    cancelled. The card is tried again later."""


class EditPriority:
    """Each release post, edit or summary holds `release()` from before it waits its turn until
    it's done. While one does, no scorecard request starts, and one already out is cancelled:
    discord.py could otherwise be sleeping on a 429 and retrying it, spending the channel's edit
    budget while the alert waits."""

    def __init__(self) -> None:
        self._releases = 0
        self._cards: set[asyncio.Task[Any]] = set()

    @property
    def release_waiting(self) -> bool:
        return self._releases > 0

    @asynccontextmanager
    async def release(self) -> AsyncIterator[None]:
        self._releases += 1
        for task in self._cards:
            task.cancel()
        try:
            yield
        finally:
            self._releases -= 1

    async def card[T](self, request: Coroutine[Any, Any, T]) -> T:
        """Runs one scorecard request. Raises CardDeferred, without running it, while a release
        step is waiting, or if one starts before the request finishes."""
        if self.release_waiting:
            request.close()
            raise CardDeferred
        task = asyncio.ensure_future(request)
        self._cards.add(task)
        try:
            await asyncio.wait({task})
        finally:
            self._cards.discard(task)
            if not task.done():
                task.cancel()  # this caller was cancelled
        if task.cancelled():
            raise CardDeferred
        return task.result()


def release_step(priority: EditPriority | None) -> AbstractAsyncContextManager[None]:
    """`priority.release()`, or nothing when there's no scorecard to hold back."""
    return priority.release() if priority is not None else nullcontext()
