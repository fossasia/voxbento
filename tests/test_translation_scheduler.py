"""The translation scheduler: one bounded, fair queue per engine (#1188)."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from portal.config import settings
from portal.translations import worker as worker_module
from portal.translations.engine import EngineKind, TranslationEngine
from portal.translations.providers.base import ProviderEngine
from portal.translations.providers.local import CT2_INTER_THREADS, LocalProvider, _in_thread
from portal.translations.scheduler import PER_CHANNEL, QUEUE_LIMIT, StaleTranslationJob, TranslationScheduler
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


async def test_a_changed_limit_counts_the_calls_already_running():
    scheduler = TranslationScheduler(2)
    recorder = Recorder(scheduler)
    running = [recorder.start(f"room-{n}", f"room-{n}:fr") for n in range(2)]
    await settle()
    waiting = recorder.start("room-9", "room-9:fr")
    await settle()

    # Lowered below what is running: nothing new starts until the running calls are down to it.
    scheduler.set_capacity(1)
    await settle()
    assert scheduler.running == 2 and scheduler.waiting() == 1

    # Raised: a waiting job starts straight away.
    scheduler.set_capacity(3)
    await settle()
    assert scheduler.running == 3 and scheduler.waiting() == 0

    await recorder.finish_all([*running, waiting])


async def test_a_slot_stays_taken_until_a_call_the_job_gave_up_on_ends():
    scheduler = TranslationScheduler(1)
    still_running = asyncio.get_running_loop().create_future()
    job = scheduler.admit("room", "room:fr")
    async with job:
        job.keep_slot_until(still_running)
    job.close()

    recorder = Recorder(scheduler)
    next_one = recorder.start("other", "other:fr")
    await settle()
    assert scheduler.running == 1 and scheduler.waiting() == 1
    assert scheduler.admitted("room", "room:fr") == 1

    still_running.set_result(None)
    await settle()
    assert recorder.order == [("other", "other:fr")]
    assert scheduler.admitted("room", "room:fr") == 0
    await recorder.finish_all([next_one])


async def test_a_cancelled_local_call_ends_only_when_its_thread_does():
    """A thread cannot be stopped, so the call (and with it the engine slot) lasts as long as it."""
    release = threading.Event()
    thread_done = threading.Event()

    def inference():
        release.wait(5)
        thread_done.set()
        return "done"

    call = asyncio.ensure_future(_in_thread(inference))
    await asyncio.sleep(0.05)
    call.cancel()
    await asyncio.sleep(0.05)
    assert not call.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert thread_done.is_set()


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

    # A changed limit applies to the same scheduler, so calls already running still count.
    before = worker_module.scheduler_for("openai")
    monkeypatch.setattr(settings, "translation_api_concurrency", 9)
    assert worker_module.scheduler_for("openai") is before
    assert before.capacity == 9


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


class StuckEngine(SlowEngine):
    """Never answers in time; its calls end only when told to, like a local model's thread."""

    def __init__(self) -> None:
        super().__init__(max_concurrency=1)
        self.release = asyncio.Event()

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls.extend(target.code for target in targets)
        try:
            await asyncio.shield(self.release.wait())
        except asyncio.CancelledError:
            # Like _in_thread: the work goes on after a cancel, and the call ends with it.
            await self.release.wait()
            raise
        finally:
            self.in_flight -= 1
        return [["late" for _ in targets] for _ in texts]


async def test_a_timed_out_call_keeps_its_slot_until_the_engine_is_done(monkeypatch):
    engine = StuckEngine()
    monkeypatch.setitem(worker_module.ENGINES, "stuck", engine)
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda provider, model: 0.05)
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    worker = TranslationWorker(MagicMock())

    def translate(room_id: int, lang: str):
        return worker._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(room_id), "stuck", "m", None, lang, lang, "en", "English",
            1, "Hello", f"event-{room_id}-floor", "uuid", 1,
        )

    # The first call times out: listeners hear of it, but the engine is still busy with it.
    await translate(1, "fr")
    assert bundle.await_args.args[8] == "pipeline_failed"
    assert worker_module.scheduler_for("stuck").running == 1

    second = asyncio.create_task(translate(2, "de"))
    await settle()
    assert engine.calls == ["fr"]
    assert engine.peak == 1

    engine.release.set()
    await second
    assert engine.calls == ["fr", "de"]
    assert engine.peak == 1
    assert worker_module.scheduler_for("stuck").running == 0


async def test_a_segment_waiting_when_the_download_starts_is_dropped_at_its_turn(monkeypatch):
    engine = SlowEngine(max_concurrency=1)
    monkeypatch.setitem(worker_module.ENGINES, "slow", engine)
    # The model is ready when the segment arrives and downloading by the time it gets a slot.
    answers = iter([60.0, None])
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda provider, model: next(answers))
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)

    await TranslationWorker(MagicMock())._translate_and_broadcast(
        SimpleNamespace(id=1), fake_room(1), "slow", "m", None, "fr", "French", "en", "English",
        1, "Hello", "event-1-floor", "uuid", 1,
    )

    assert engine.calls == []
    assert bundle.await_args.args[8] == "model_downloading"
    assert worker_module.scheduler_for("slow").admitted(1, "event-1-floor:fr") == 0


async def test_a_segment_leaves_its_language_queue_before_its_speech_is_made(monkeypatch):
    """Slow speech synthesis must not count against the language's 15 admitted segments."""
    engine = SlowEngine(max_concurrency=1)

    async def answer(source, targets, texts, context, *, model, api_key=None):
        return [["Bonjour" for _ in targets] for _ in texts]

    engine.translate_batch = answer
    monkeypatch.setitem(worker_module.ENGINES, "answering", engine)
    stored = MagicMock(add=MagicMock(), flush=AsyncMock())

    @asynccontextmanager
    async def fake_session():
        yield stored

    monkeypatch.setattr(worker_module, "get_session", fake_session)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    monkeypatch.setattr("portal.websockets.manager.listener_manager.broadcast", AsyncMock())
    monkeypatch.setattr(worker_module.youtube_captions, "offer", MagicMock())
    speaking = asyncio.Event()
    finish_speaking = asyncio.Event()

    async def slow_synthesize(room_id, text, lang_code):
        speaking.set()
        await finish_speaking.wait()
        return b"audio"

    monkeypatch.setattr("portal.tts.worker.synthesize", slow_synthesize)

    task = asyncio.create_task(
        TranslationWorker(MagicMock())._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(1), "answering", "m", None, "fr", "French", "en", "English",
            1, "Hello", "event-1-floor", "uuid", 1, "event-1-fr",
        )
    )
    await asyncio.wait_for(speaking.wait(), timeout=5)

    scheduler = worker_module.scheduler_for("answering")
    assert scheduler.admitted(1, "event-1-floor:fr") == 0
    assert scheduler.running == 0

    finish_speaking.set()
    await task
    stored.add.assert_called_once()


# ── Stale segments and listeners who left (#1189) ──


async def test_a_job_that_cannot_start_within_the_lag_budget_is_dropped_without_a_slot():
    scheduler = TranslationScheduler(1, lag_budget=0.05)
    recorder = Recorder(scheduler)
    busy = recorder.start("room-1", "room-1:fr")
    await settle()

    late = scheduler.admit("room-2", "room-2:de")
    with pytest.raises(StaleTranslationJob) as dropped:
        async with late:
            pytest.fail("a stale job must not start")
    late.close()

    assert dropped.value.waited >= 0.05
    assert scheduler.running == 1
    assert scheduler.waiting() == 0
    assert scheduler.admitted("room-2", "room-2:de") == 0

    # The busy job still finishes, and the next one gets its slot.
    scheduler.lag_budget = None
    after = recorder.start("room-2", "room-2:de")
    await recorder.finish_all([busy, after])
    assert recorder.order == [("room-1", "room-1:fr"), ("room-2", "room-2:de")]
    assert scheduler.running == 0


async def test_a_job_already_past_the_lag_budget_is_dropped_even_with_a_free_slot():
    """The budget counts from admission, so time spent before asking for a slot counts too."""
    scheduler = TranslationScheduler(1, lag_budget=0.01)
    job = scheduler.admit("room", "room:fr")
    await asyncio.sleep(0.02)

    with pytest.raises(StaleTranslationJob):
        async with job:
            pytest.fail("a stale job must not start")
    job.close()

    assert scheduler.running == 0
    assert scheduler.admitted("room", "room:fr") == 0


async def test_a_job_handed_its_slot_after_the_deadline_is_dropped_even_if_its_timeout_has_not_fired():
    """A late event loop can wake the waiter with its slot before the timeout callback runs."""
    scheduler = TranslationScheduler(1, lag_budget=0.05)
    busy = scheduler.admit("room-1", "room-1:fr")
    release = asyncio.Event()

    async def hold() -> None:
        try:
            async with busy:
                await release.wait()
                # Blocks the loop past the waiter's deadline, then frees the slot in the same step,
                # so the waiter is woken with the slot before its timeout callback gets to run.
                time.sleep(0.1)
        finally:
            busy.close()

    late = scheduler.admit("room-2", "room-2:de")
    started = False

    async def wait() -> None:
        nonlocal started
        try:
            async with late:
                started = True
        finally:
            late.close()

    holder = asyncio.create_task(hold())
    await settle()
    waiter = asyncio.create_task(wait())
    await settle()
    release.set()

    with pytest.raises(StaleTranslationJob):
        await waiter
    await holder
    assert not started
    assert scheduler.running == 0
    assert scheduler.admitted("room-2", "room-2:de") == 0


async def test_the_lag_budget_counts_from_the_segment_arriving_not_from_admission():
    scheduler = TranslationScheduler(1, lag_budget=0.5)
    # The segment arrived a second ago (its job was resolved meanwhile) and is admitted only now.
    job = scheduler.admit("room", "room:fr", arrived_at=time.monotonic() - 1.0)

    with pytest.raises(StaleTranslationJob) as dropped:
        async with job:
            pytest.fail("a stale job must not start")
    job.close()

    assert dropped.value.waited >= 1.0
    assert scheduler.running == 0
    assert scheduler.admitted("room", "room:fr") == 0


async def test_a_job_that_starts_within_the_lag_budget_runs():
    scheduler = TranslationScheduler(1, lag_budget=5.0)
    recorder = Recorder(scheduler)
    tasks = [recorder.start("room", "room:fr"), recorder.start("room", "room:de")]

    await recorder.finish_all(tasks)
    assert len(recorder.order) == 2


class HeldEngine(SlowEngine):
    """Answers (with nothing to say) only when released, so a test decides when its slot frees up."""

    def __init__(self) -> None:
        super().__init__(max_concurrency=1)
        self.release = asyncio.Event()

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.calls.extend(target.code for target in targets)
        await self.release.wait()
        return [["" for _ in targets] for _ in texts]


async def test_the_worker_drops_a_segment_that_waited_too_long_as_stale(monkeypatch):
    engine = HeldEngine()
    monkeypatch.setitem(worker_module.ENGINES, "held", engine)
    monkeypatch.setattr(settings, "translation_max_lag_seconds", 0.05)
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    worker = TranslationWorker(MagicMock())

    def translate(room_id: int, lang: str):
        return worker._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(room_id), "held", "m", None, lang, lang, "en", "English",
            1, "Hello", f"event-{room_id}-floor", "uuid", 1,
        )

    first = asyncio.create_task(translate(1, "fr"))
    await settle()
    # The engine is busy with the first segment for longer than the second may wait.
    await translate(2, "de")

    assert engine.calls == ["fr"]
    assert bundle.await_count == 1
    # Reported as stale, not as a failure.
    assert bundle.await_args.args[2] == "event-2-floor"
    assert bundle.await_args.args[8] == "stale"

    engine.release.set()
    await first
    assert worker_module.scheduler_for("held").running == 0
    assert worker_module.scheduler_for("held").admitted(2, "event-2-floor:de") == 0


@pytest.mark.parametrize("left", [True, False])
async def test_a_segment_is_skipped_when_its_last_listener_left_while_it_waited(monkeypatch, left):
    engine = HeldEngine()
    monkeypatch.setitem(worker_module.ENGINES, "held", engine)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    listening = {"fr": True, "de": True}
    monkeypatch.setattr(
        "portal.websockets.manager.tts_manager.has_listeners", lambda room_id, lang, booth_id: listening[lang]
    )
    monkeypatch.setattr("portal.websockets.manager.listener_manager.has_listeners", lambda booth_id: False)
    monkeypatch.setattr(worker_module.youtube_captions, "wants", lambda room_id, lang: False)
    job = worker_module._ResolvedTranslation(
        event=SimpleNamespace(id=1, slug="event"),
        room=fake_room(1),
        provider="held",
        model="m",
        api_key=None,
        enabled_langs=[
            SimpleNamespace(language_code="fr", language_name="French"),
            SimpleNamespace(language_code="de", language_name="German"),
        ],
        source_lang_code="en",
        source_lang_name="English",
    )
    worker = TranslationWorker(MagicMock())

    tasks = [
        asyncio.create_task(task)
        for task in worker._build_language_tasks(job, 1, "Hello", "event-1-floor", "uuid", 1)
    ]
    await settle()
    # German waits for the engine's only slot while French is translated.
    assert engine.calls == ["fr"]
    if left:
        listening["de"] = False

    engine.release.set()
    await asyncio.gather(*tasks)

    # German's inference runs only if someone still listens when its turn comes.
    assert engine.calls == (["fr"] if left else ["fr", "de"])
    assert worker_module.scheduler_for("held").running == 0


async def test_the_worker_counts_the_lag_budget_from_when_the_segment_arrived(monkeypatch):
    """Time spent resolving the job before it reaches the scheduler counts against the budget."""
    engine = HeldEngine()
    monkeypatch.setitem(worker_module.ENGINES, "held", engine)
    monkeypatch.setattr(settings, "translation_max_lag_seconds", 0.5)
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.has_listeners", lambda room_id, lang, booth_id: True)
    monkeypatch.setattr("portal.websockets.manager.listener_manager.has_listeners", lambda booth_id: False)
    job = worker_module._ResolvedTranslation(
        event=SimpleNamespace(id=1, slug="event"),
        room=fake_room(1),
        provider="held",
        model="m",
        api_key=None,
        enabled_langs=[SimpleNamespace(language_code="fr", language_name="French")],
        source_lang_code="en",
        source_lang_name="English",
    )

    # The engine is free, but the segment arrived a second ago.
    tasks = TranslationWorker(MagicMock())._build_language_tasks(
        job, 1, "Hello", "event-1-floor", "uuid", 1, arrived_at=time.monotonic() - 1.0
    )
    await asyncio.gather(*tasks)

    assert engine.calls == []
    assert bundle.await_args.args[8] == "stale"
    assert worker_module.scheduler_for("held").admitted(1, "event-1-floor:fr") == 0
