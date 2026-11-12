"""The translation scheduler: one bounded, fair queue per engine (#1188)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from portal.config import settings
from portal.translations import worker as worker_module
from portal.translations.engine import EngineKind, TranslationEngine
from portal.translations.providers.base import ProviderEngine
from portal.translations.providers.local import CT2_INTER_THREADS, LocalProvider
from portal.translations.scheduler import PER_CHANNEL, QUEUE_LIMIT, TranslationScheduler
from portal.translations.worker import TranslationWorker

pytestmark = pytest.mark.anyio


async def settle() -> None:
    """Let every task that can run do so."""
    for _ in range(5):
        await asyncio.sleep(0)


class Recorder:
    """Runs jobs that wait for a release, and records the order they were given a slot."""

    def __init__(self, scheduler: TranslationScheduler) -> None:
        self.scheduler = scheduler
        self.order: list[tuple[str, str]] = []
        self.peak = 0
        self.release: dict[tuple[str, str], list[asyncio.Event]] = {}

    def start(self, room: str, channel: str) -> asyncio.Task:
        job = self.scheduler.admit(room, channel)
        assert job is not None
        done = asyncio.Event()
        self.release.setdefault((room, channel), []).append(done)

        async def run() -> None:
            try:
                async with job:
                    self.order.append((room, channel))
                    self.peak = max(self.peak, self.scheduler.running)
                    await done.wait()
            finally:
                job.close()

        return asyncio.create_task(run())

    async def finish_all(self, tasks: list[asyncio.Task]) -> None:
        """Release each running job in turn until every task is done."""
        while not all(task.done() for task in tasks):
            await settle()
            for room, channel in list(self.order):
                for event in self.release[(room, channel)]:
                    event.set()
            await settle()
        await asyncio.gather(*tasks)


async def test_never_more_calls_in_flight_than_the_engine_serves():
    scheduler = TranslationScheduler(3)
    recorder = Recorder(scheduler)
    tasks = [recorder.start(f"room-{r}", f"room-{r}:{lang}") for r in range(10) for lang in ("fr", "de", "es", "it")]

    await settle()
    assert scheduler.running == 3
    assert scheduler.waiting() == len(tasks) - 3

    await recorder.finish_all(tasks)
    assert recorder.peak == 3
    assert len(recorder.order) == len(tasks)
    assert scheduler.running == 0 and scheduler.waiting() == 0


async def test_rooms_take_turns_so_a_busy_room_cannot_starve_a_small_one():
    scheduler = TranslationScheduler(1)
    recorder = Recorder(scheduler)
    blocker = recorder.start("other", "other:fr")
    await settle()
    # The busy room queues 8 languages before the small room's only language arrives.
    busy = [recorder.start("busy", f"busy:{lang}") for lang in ("fr", "de", "es", "it", "pt", "nl", "pl", "sv")]
    small = [recorder.start("small", "small:th") for _ in range(2)]

    await recorder.finish_all([blocker, *busy, *small])

    rooms = [room for room, _ in recorder.order[1:]]
    # While both rooms have work waiting, they alternate.
    assert rooms[:4] in (["busy", "small", "busy", "small"], ["small", "busy", "small", "busy"])
    assert rooms.count("busy") == 8 and rooms.count("small") == 2


async def test_languages_within_a_room_take_turns():
    scheduler = TranslationScheduler(1, per_channel=5)
    recorder = Recorder(scheduler)
    blocker = recorder.start("room", "room:es")
    await settle()
    tasks = [recorder.start("room", "room:fr") for _ in range(3)] + [recorder.start("room", "room:de") for _ in range(3)]

    await recorder.finish_all([blocker, *tasks])

    channels = [channel for _, channel in recorder.order[1:]]
    assert channels in (["room:fr", "room:de"] * 3, ["room:de", "room:fr"] * 3)


@pytest.mark.parametrize("returning", ["language", "room"])
async def test_a_queue_that_empties_and_comes_back_waits_behind_the_ones_already_waiting(returning):
    """A language (or room) whose jobs keep draining must not jump ahead of one that stays busy.

    Queues are removed when they empty, so a returning one must rejoin at the back; otherwise
    quickly drained languages are always served first and busy ones fall further behind.
    """
    scheduler = TranslationScheduler(1)
    recorder = Recorder(scheduler)
    blocker = recorder.start("room", "room:es")
    await settle()
    waiting = recorder.start("room", "room:fr")
    await settle()

    room, channel = ("room", "room:de") if returning == "language" else ("other", "other:de")
    # The queue empties (its only job leaves without running) and comes back.
    scheduler.admit(room, channel).close()
    assert scheduler.admitted(room, channel) == 0
    returned = recorder.start(room, channel)

    await recorder.finish_all([blocker, waiting, returned])
    assert recorder.order[1:] == [("room", "room:fr"), (room, channel)]


async def test_a_language_runs_at_most_two_calls_at_once():
    scheduler = TranslationScheduler(10)
    recorder = Recorder(scheduler)
    tasks = [recorder.start("room", "room:fr") for _ in range(5)]

    await settle()
    assert PER_CHANNEL == 2
    assert scheduler.running == 2
    assert scheduler.waiting() == 3

    await recorder.finish_all(tasks)
    assert recorder.peak == 2


async def test_a_full_language_queue_refuses_new_segments_until_one_leaves():
    scheduler = TranslationScheduler(1)
    held = [scheduler.admit("room", "room:fr") for _ in range(QUEUE_LIMIT)]

    assert all(held)
    assert scheduler.admit("room", "room:fr") is None
    # Other languages and rooms are not affected.
    other = scheduler.admit("room", "room:de")
    assert other is not None
    other.close()

    held[0].close()
    held[0].close()  # closing twice gives back only one place
    assert scheduler.admitted("room", "room:fr") == QUEUE_LIMIT - 1
    assert scheduler.admit("room", "room:fr") is not None


async def test_a_job_cancelled_while_waiting_gives_up_its_place():
    scheduler = TranslationScheduler(1)
    recorder = Recorder(scheduler)
    blocker = recorder.start("room", "room:fr")
    await settle()
    waiting = recorder.start("room", "room:de")
    after = recorder.start("other", "other:fr")
    await settle()
    assert scheduler.waiting() == 2

    waiting.cancel()
    await settle()
    assert scheduler.waiting() == 1
    assert scheduler.admitted("room", "room:de") == 0

    await recorder.finish_all([blocker, after])
    assert ("room", "room:de") not in recorder.order
    assert scheduler.running == 0


async def test_a_slot_given_to_a_job_cancelled_before_it_ran_goes_to_the_next_one():
    scheduler = TranslationScheduler(1)
    first = scheduler.admit("room", "room:fr")
    await first.__aenter__()

    entered = []

    async def wait_turn(channel: str) -> None:
        job = scheduler.admit("room", channel)
        try:
            async with job:
                entered.append(channel)
        finally:
            job.close()

    granted_then_cancelled = asyncio.create_task(wait_turn("room:de"))
    next_one = asyncio.create_task(wait_turn("room:es"))
    await settle()

    # The slot goes to the first waiter, which is cancelled before it gets to run.
    await first.__aexit__(None, None, None)
    granted_then_cancelled.cancel()
    await asyncio.gather(granted_then_cancelled, next_one, return_exceptions=True)

    assert entered == ["room:es"]
    assert scheduler.running == 0 and scheduler.waiting() == 0


async def test_a_job_takes_a_slot_only_once():
    scheduler = TranslationScheduler(1)
    job = scheduler.admit("room", "room:fr")
    async with job:
        pass
    with pytest.raises(RuntimeError):
        async with job:
            pass


@pytest.mark.parametrize("arguments", [{"capacity": 0}, {"capacity": 1, "per_channel": 0}, {"capacity": 1, "queue_limit": 0}])
def test_limits_must_be_positive(arguments):
    with pytest.raises(ValueError):
        TranslationScheduler(**arguments)


# ── Sizing each engine's scheduler ──


def test_the_local_model_is_scheduled_one_call_per_ct2_thread():
    assert LocalProvider.max_concurrency == CT2_INTER_THREADS
    assert worker_module.scheduler_for("local").capacity == CT2_INTER_THREADS


def test_hosted_and_self_hosted_limits_come_from_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "translation_api_concurrency", 7)
    monkeypatch.setattr(settings, "self_hosted_llm_concurrency", 3)

    assert worker_module.scheduler_for("openai").capacity == 7
    assert worker_module.scheduler_for("gemini").capacity == 7
    assert worker_module.scheduler_for("self_hosted").capacity == 3
    # One scheduler per provider, shared by every room.
    assert worker_module.scheduler_for("openai") is worker_module.scheduler_for("openai")
    assert worker_module.scheduler_for("openai") is not worker_module.scheduler_for("groq")

    monkeypatch.setattr(settings, "translation_api_concurrency", 9)
    assert worker_module.scheduler_for("openai").capacity == 9


def test_an_engine_without_its_own_limit_gets_the_default():
    engine = ProviderEngine("x", MagicMock(), EngineKind.LLM_API)
    assert engine.max_concurrency == TranslationEngine.max_concurrency


# ── Through the worker ──


class SlowEngine(TranslationEngine):
    """Takes a while per call, records the order of calls and the most in flight at once."""

    kind = EngineKind.MT_API

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls.extend(target.code for target in targets)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.in_flight -= 1
        # An empty translation closes the segment without storing or speaking it.
        return [["" for _ in targets] for _ in texts]


def fake_room(room_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=room_id,
        floor_ai_vocabulary_enabled=False,
        floor_ai_interpreter_persona=None,
        floor_ai_interpretation_style=None,
    )


async def test_the_worker_shares_one_engine_fairly_across_rooms(monkeypatch):
    engine = SlowEngine(max_concurrency=1)
    monkeypatch.setitem(worker_module.ENGINES, "slow", engine)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    worker = TranslationWorker(MagicMock())

    def translate(room_id: int, lang: str):
        return worker._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(room_id), "slow", "m", None, lang, lang, "en", "English",
            1, "Hello", f"event-{room_id}-floor", "uuid", 1,
        )

    busy = ["fr", "de", "es", "it", "pt", "nl", "pl", "sv"]
    # Eight segments of the busy room are queued before the small room's one.
    tasks = [asyncio.create_task(translate(1, lang)) for _ in range(3) for lang in busy]
    await settle()
    tasks.append(asyncio.create_task(translate(2, "th")))
    await asyncio.gather(*tasks)

    assert engine.peak == 1
    assert len(engine.calls) == len(tasks)
    # The small room waits for one call of the busy room, not for all of them.
    assert engine.calls.index("th") <= 2
    assert worker_module.scheduler_for("slow").running == 0
