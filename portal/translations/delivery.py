"""Keep caption delivery in admission order when primary and fallback calls race."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from portal.translations.scheduler import QUEUE_LIMIT

Channel = tuple[int, str, str]


class DeliveryTurn:
    def __init__(
        self, previous: asyncio.Future[None] | None, done: asyncio.Future[None], cleanup: Callable[[], None]
    ) -> None:
        self.previous = previous
        self.done = done
        self.cleanup = cleanup
        self.closed = False

    async def wait(self) -> None:
        if self.previous is not None:
            await asyncio.shield(self.previous)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.previous is None or self.previous.done():
            self.complete()
        else:
            # A cancelled middle request must not let its successor overtake its predecessor.
            self.previous.add_done_callback(lambda _: self.complete())

    def complete(self) -> None:
        if not self.done.done():
            self.done.set_result(None)
            self.cleanup()


class TranslationDeliveryOrder:
    def __init__(self, *, rejection_limit: int = QUEUE_LIMIT) -> None:
        if rejection_limit < 1:
            raise ValueError("rejection_limit must be at least 1")
        self.tails: dict[Channel, asyncio.Future[None]] = {}
        self.rejection_limit = rejection_limit
        self.rejections: dict[Channel, int] = {}

    def reserve(self, channel: Channel, *, enabled: bool, rejected: bool = False) -> DeliveryTurn | None:
        # A rejected segment has no scheduler admission keeping its notification bounded.
        # Omit excess rejection notices instead of growing a chain behind a stalled caption.
        if rejected and self.rejections.get(channel, 0) >= self.rejection_limit:
            return None
        previous = self.tails.get(channel)
        if not enabled and not rejected and previous is None:
            return None
        done = asyncio.get_running_loop().create_future()
        self.tails[channel] = done
        if rejected:
            self.rejections[channel] = self.rejections.get(channel, 0) + 1

        def cleanup() -> None:
            if self.tails.get(channel) is done:
                del self.tails[channel]
            if rejected:
                remaining = self.rejections[channel] - 1
                if remaining:
                    self.rejections[channel] = remaining
                else:
                    del self.rejections[channel]

        return DeliveryTurn(previous, done, cleanup)


# Reserve before asynchronous transcript persistence, so database lookups cannot reorder
# translation dispatch. The same per-channel budget bounds pending resolution work.
RESOLUTION_ORDER = TranslationDeliveryOrder()
