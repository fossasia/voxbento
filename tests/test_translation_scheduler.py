"""The translation scheduler: one bounded, fair queue per engine (#1188)."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from portal.config import settings
from portal.translations import worker as worker_module
from portal.translations.engine import EngineKind, TranslationEngine
from portal.translations.providers.base import ProviderEngine
from portal.translations.providers.local import CT2_INTER_THREADS, LocalProvider, _in_thread
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
    # The job has left its channel, but the engine is still busy with its call.
    assert scheduler.running == 1 and scheduler.waiting() == 1
    assert scheduler.admitted("room", "room:fr") == 0

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


def test_each_local_model_is_scheduled_one_call_per_ct2_thread():
    """Each loaded model has its own CTranslate2 threads, so each model has its own scheduler."""
    assert LocalProvider.max_concurrency == CT2_INTER_THREADS
    key = worker_module._scheduler_key
    assert key("local", "nllb-200-distilled-600M", None) != key("local", "nllb-200-1.3B", None)
    assert worker_module.scheduler_for("local", "nllb-200-distilled-600M").capacity == CT2_INTER_THREADS


def test_each_hosted_api_key_has_its_own_scheduler():
    """Each event's key has its own quota, so a failing key only holds up its own event."""
    key = worker_module._scheduler_key
    first = key("openai", "gpt-4o-mini", "sk-first-event")
    assert first != key("openai", "gpt-4o-mini", "sk-second-event")
    assert first == key("openai", "gpt-4o", "sk-first-event")  # the key decides, not the model
    # The schedulers keep no copy of a key.
    assert "sk-first-event" not in first[1]
    # The self-hosted server is one server, whichever key reaches it.
    assert key("self_hosted", "llama3.2", "a") == key("self_hosted", "llama3.2", "b")


def test_idle_schedulers_are_dropped_when_a_new_one_is_made(monkeypatch):
    monkeypatch.setattr(worker_module, "SCHEDULERS", {})
    busy = worker_module.scheduler_for("openai", "m", "busy-key")
    job = busy.admit("room", "room:fr")
    worker_module.scheduler_for("openai", "m", "idle-key")

    worker_module.scheduler_for("openai", "m", "third-key")

    assert busy in worker_module.SCHEDULERS.values()
    assert len(worker_module.SCHEDULERS) == 2  # the idle one is gone
    job.close()


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


async def test_a_segment_for_a_downloading_model_is_dropped_after_its_slot_is_free(monkeypatch):
    """Checked once the segment has its turn; listeners are told after the slot is given back."""
    engine = SlowEngine(max_concurrency=1)
    monkeypatch.setitem(worker_module.ENGINES, "slow", engine)
    checks = []

    def downloading(provider, model):
        checks.append(worker_module.scheduler_for("slow").running)
        return None

    monkeypatch.setattr(worker_module, "_inference_timeout", downloading)
    running_when_told = []

    async def bundle(*args, **kwargs):
        running_when_told.append(worker_module.scheduler_for("slow").running)

    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    told = AsyncMock(side_effect=bundle)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", told)

    await TranslationWorker(MagicMock())._translate_and_broadcast(
        SimpleNamespace(id=1), fake_room(1), "slow", "m", None, "fr", "French", "en", "English",
        1, "Hello", "event-1-floor", "uuid", 1,
    )

    assert engine.calls == []
    assert checks == [1]  # one check, made while holding the slot
    assert told.await_args.args[8] == "model_downloading"
    assert running_when_told == [0]
    assert worker_module.scheduler_for("slow").admitted(1, "event-1-floor:fr") == 0


def test_a_loaded_local_model_skips_the_download_lookup(monkeypatch):
    monkeypatch.setattr(worker_module.local_provider, "is_model_loaded", lambda model: True)
    lookup = MagicMock(side_effect=AssertionError("no lookup on disk for a loaded model"))
    monkeypatch.setattr(worker_module.local_provider, "get_download_progress", lookup)

    assert worker_module._inference_timeout("local", "nllb-200-distilled-600M") == 60.0


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


async def test_a_segment_counts_in_its_language_queue_until_its_text_is_delivered(monkeypatch):
    """A slow save frees the engine for others but still counts against the language's 15 segments."""
    engine = SlowEngine(max_concurrency=1)

    async def answer(source, targets, texts, context, *, model, api_key=None):
        return [["Bonjour" for _ in targets] for _ in texts]

    engine.translate_batch = answer
    monkeypatch.setitem(worker_module.ENGINES, "answering", engine)
    saving = asyncio.Event()
    finish_saving = asyncio.Event()

    async def slow_flush():
        saving.set()
        await finish_saving.wait()

    stored = MagicMock(add=MagicMock(), flush=slow_flush)

    @asynccontextmanager
    async def fake_session():
        yield stored

    monkeypatch.setattr(worker_module, "get_session", fake_session)
    bundle = AsyncMock()
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    monkeypatch.setattr("portal.websockets.manager.listener_manager.broadcast", AsyncMock())
    monkeypatch.setattr(worker_module.youtube_captions, "offer", MagicMock())
    monkeypatch.setattr("portal.tts.worker.synthesize", AsyncMock(return_value=b"audio"))

    task = asyncio.create_task(
        TranslationWorker(MagicMock())._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(1), "answering", "m", None, "fr", "French", "en", "English",
            1, "Hello", "event-1-floor", "uuid", 1, "event-1-fr",
        )
    )
    await asyncio.wait_for(saving.wait(), timeout=5)

    scheduler = worker_module.scheduler_for("answering")
    assert scheduler.running == 0
    assert scheduler.admitted(1, "event-1-floor:fr") == 1

    finish_saving.set()
    await task
    assert scheduler.admitted(1, "event-1-floor:fr") == 0
    assert bundle.await_count == 2


# ── A room whose calls hang (maintainer review) ──


async def test_a_room_holding_more_than_its_share_lets_a_waiting_room_go_first():
    """Once a second room waits, every slot that frees goes to it until both hold their share."""
    scheduler = TranslationScheduler(4, per_channel=4)
    recorder = Recorder(scheduler)
    hanging = [recorder.start("busy", "busy:fr") for _ in range(4)]
    await settle()
    more_busy = [recorder.start("busy", "busy:fr") for _ in range(2)]
    small = [recorder.start("small", "small:th") for _ in range(2)]
    await settle()
    assert scheduler.running == 4

    # Two of the busy room's calls end: both slots go to the small room, though the busy
    # room has waited longer and is first in turn.
    for event in recorder.release[("busy", "busy:fr")][:2]:
        event.set()
    await settle()
    assert recorder.order[4:] == [("small", "small:th"), ("small", "small:th")]

    await recorder.finish_all([*hanging, *more_busy, *small])


async def test_spare_slots_still_go_to_a_room_above_its_share():
    """The share only decides who goes first: no slot sits idle while a room waits."""
    scheduler = TranslationScheduler(4, per_channel=4)
    recorder = Recorder(scheduler)
    small = recorder.start("small", "small:th")
    busy = [recorder.start("busy", "busy:fr") for _ in range(4)]
    await settle()

    # Two rooms, so each one's share is 2; the small room needs only 1, so busy gets 3.
    assert scheduler.running == 4
    assert recorder.order.count(("busy", "busy:fr")) == 3

    await recorder.finish_all([small, *busy])


class ModelEngine(SlowEngine):
    """Like a local model: one call at a time per model, and the calls wait until released."""

    def __init__(self) -> None:
        super().__init__(max_concurrency=1)
        self.release = asyncio.Event()

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls.append(model)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return [["" for _ in targets] for _ in texts]


async def test_rooms_on_different_local_models_translate_at_the_same_time(monkeypatch):
    engine = ModelEngine()
    monkeypatch.setitem(worker_module.ENGINES, "local", engine)
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda provider, model: 5.0)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    worker = TranslationWorker(MagicMock())

    def translate(room_id: int, model: str):
        return worker._translate_and_broadcast(
            SimpleNamespace(id=1), fake_room(room_id), "local", model, None, "fr", "French", "en", "English",
            1, "Hello", f"event-{room_id}-floor", "uuid", 1,
        )

    tasks = [asyncio.create_task(translate(1, "model-a")), asyncio.create_task(translate(2, "model-b"))]
    await settle()
    assert sorted(engine.calls) == ["model-a", "model-b"]
    assert engine.peak == 2

    engine.release.set()
    await asyncio.gather(*tasks)


async def test_an_event_with_a_stuck_api_key_does_not_hold_up_another_event(monkeypatch):
    stuck = asyncio.Event()

    class KeyedEngine(SlowEngine):
        async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
            self.calls.append(api_key)
            if api_key == "stuck-key":
                await stuck.wait()
            return [["" for _ in targets] for _ in texts]

    engine = KeyedEngine(max_concurrency=1)
    monkeypatch.setitem(worker_module.ENGINES, "keyed", engine)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    worker = TranslationWorker(MagicMock())

    def translate(room_id: int, key: str):
        return worker._translate_and_broadcast(
            SimpleNamespace(id=room_id), fake_room(room_id), "keyed", "m", key, "fr", "French", "en", "English",
            1, "Hello", f"event-{room_id}-floor", "uuid", 1,
        )

    blocked = asyncio.create_task(translate(1, "stuck-key"))
    await settle()
    # The other event's call goes straight to its own key, though the engine limit is one.
    await asyncio.wait_for(translate(2, "good-key"), timeout=5)
    assert engine.calls == ["stuck-key", "good-key"]

    stuck.set()
    await blocked


async def test_the_live_demo_waits_for_the_local_models_slot(monkeypatch):
    from portal import live_demo

    model = settings.live_demo_translation_model
    calls = []

    class FakeTranslator:
        async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
            calls.append(model)
            return [[f"{texts[0]} ({target.code})" for target in targets]]

    monkeypatch.setattr(live_demo, "_translator", lambda: FakeTranslator())
    request = SimpleNamespace(source=SimpleNamespace(code="en", name="English"), glossary=())
    targets = [SimpleNamespace(code="fr", name="French")]

    # A booth call holds the model's only slot.
    booth = worker_module.scheduler_for("local", model).admit("room", "room:fr")
    await booth.__aenter__()
    demo = asyncio.create_task(live_demo._local_translate("Hello", request, targets))
    await settle()
    assert calls == []

    booth.close()
    assert await asyncio.wait_for(demo, timeout=5) == {"fr": "Hello (fr)"}
    assert calls == [model]
    assert worker_module.scheduler_for("local", model).idle()
