"""Keep caption delivery in admission order when primary and fallback calls race."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

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
    def __init__(self) -> None:
        self.tails: dict[Channel, asyncio.Future[None]] = {}

    def reserve(self, channel: Channel, *, enabled: bool) -> DeliveryTurn | None:
        previous = self.tails.get(channel)
        if not enabled and previous is None:
            return None
        done = asyncio.get_running_loop().create_future()
        self.tails[channel] = done

        def cleanup() -> None:
            if self.tails.get(channel) is done:
                del self.tails[channel]

        return DeliveryTurn(previous, done, cleanup)
