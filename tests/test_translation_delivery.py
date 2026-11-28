from __future__ import annotations

import asyncio

import pytest

from portal.translations.delivery import TranslationDeliveryOrder

pytestmark = pytest.mark.anyio


async def test_cancelled_middle_turn_cannot_release_its_successor_early():
    order = TranslationDeliveryOrder()
    first = order.reserve((1, "floor", "fr"), enabled=True)
    middle = order.reserve((1, "floor", "fr"), enabled=True)
    last = order.reserve((1, "floor", "fr"), enabled=True)

    async def wait(turn):
        try:
            await turn.wait()
        finally:
            turn.close()

    cancelled = asyncio.create_task(wait(middle))
    following = asyncio.create_task(wait(last))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not following.done()
    first.close()
    await asyncio.wait_for(following, 1)
    assert not order.tails


async def test_unrelated_channels_continue_independently():
    order = TranslationDeliveryOrder()
    first = order.reserve((1, "floor", "fr"), enabled=True)
    other = order.reserve((1, "floor", "de"), enabled=True)
    await asyncio.wait_for(other.wait(), 1)
    other.close()
    first.close()
    assert not order.tails


async def test_disabling_fallback_does_not_overtake_an_existing_turn():
    order = TranslationDeliveryOrder()
    first = order.reserve((1, "floor", "fr"), enabled=True)
    following = order.reserve((1, "floor", "fr"), enabled=False)
    task = asyncio.create_task(following.wait())
    await asyncio.sleep(0)
    assert not task.done()
    first.close()
    await asyncio.wait_for(task, 1)
    following.close()
    assert order.reserve((1, "floor", "fr"), enabled=False) is None


async def test_cancelled_rejection_keeps_its_budget_until_the_predecessor_finishes():
    order = TranslationDeliveryOrder(rejection_limit=1)
    channel = (1, "floor", "fr")
    first = order.reserve(channel, enabled=True)
    rejected = order.reserve(channel, enabled=True, rejected=True)
    rejected.close()
    assert order.reserve(channel, enabled=True, rejected=True) is None
    # The limit belongs to one channel, so another language remains usable.
    other = order.reserve((1, "floor", "de"), enabled=True, rejected=True)
    other.close()
    first.close()
    await asyncio.sleep(0)
    assert not order.tails and not order.rejections
    replacement = order.reserve(channel, enabled=True, rejected=True)
    replacement.close()
    assert not order.tails and not order.rejections
