"""Per-room translation failover, bounded scheduling and primary recovery (#1195)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from portal.crypto import encrypt_val
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room, RoomTranslationLanguage, TranscriptSegment, TranscriptTranslation
from portal.translations import circuit as circuit_module
from portal.translations import worker as worker_module
from portal.translations.circuit import CircuitBreaker, circuit_for
from portal.translations.engine import EngineKind, TranslationEngine
from portal.translations.providers.local import _in_thread
from portal.translations.worker import FallbackTranslation, TranslationWorker


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_circuit_opens_after_consecutive_failures_and_allows_one_recovery_probe():
    clock = Clock()
    circuit = CircuitBreaker(clock=clock)
    for _ in range(3):
        circuit.start().finish(False)
    assert circuit.start() is None
    clock.now = 29.9
    assert circuit.start() is None
    clock.now = 30
    probe = circuit.start()
    assert probe.probe and circuit.start() is None
    probe.finish(True)
    assert circuit.failures == 0 and circuit.opened_until is None
    assert circuit.start() is not None


def test_failed_probe_restarts_cooldown_and_cancelled_probe_can_be_retried():
    clock = Clock()
    circuit = CircuitBreaker(threshold=1, clock=clock)
    circuit.start().finish(False)
    clock.now = 30
    probe = circuit.start()
    probe.finish(None)
    circuit.start().finish(False)
    assert circuit.start() is None
    clock.now = 60
    circuit.start().finish(True)
    assert circuit.start() is not None


def test_late_success_does_not_close_newly_opened_circuit():
    circuit = CircuitBreaker(threshold=1)
    slow, failing = circuit.start(), circuit.start()
    failing.finish(False)
    slow.finish(True)
    assert circuit.start() is None
    slow.finish(False)  # Completion is idempotent.
    assert circuit.active == 0


def test_success_resets_consecutive_failures_and_cancellation_is_not_failure():
    circuit = CircuitBreaker()
    circuit.start().finish(False)
    circuit.start().finish(True)
    circuit.start().finish(None)
    assert circuit.failures == 0 and circuit.opened_until is None


def test_rooms_and_primary_engines_have_independent_breakers(monkeypatch):
    monkeypatch.setattr(circuit_module, "CIRCUITS", {})
    a = circuit_for(1, "self_hosted", "a", "local", "nllb")
    b = circuit_for(2, "self_hosted", "a", "local", "nllb")
    booth = circuit_for(1, "openai", "b", "local", "nllb")
    for _ in range(3):
        a.start().finish(False)
    assert circuit_for(1, "self_hosted", "a", "local", "nllb") is a
    assert a.start() is None
    assert b.start() is not None and booth.start() is not None
    changed = circuit_for(1, "self_hosted", "a", "local", "madlad")
    assert changed is not a and changed.start() is not None


@pytest.mark.parametrize("threshold,recovery", [(0, 30), (3, 0)])
def test_invalid_breaker_configuration_is_rejected(threshold, recovery):
    with pytest.raises(ValueError):
        CircuitBreaker(threshold=threshold, recovery=recovery)


class Engine(TranslationEngine):
    kind = EngineKind.LLM_API
    max_concurrency = 1

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        self.calls.append((source, targets, texts, context, model, api_key))
        if isinstance(self.result, Exception):
            raise self.result
        return [[self.result]]


@pytest.fixture
async def pipeline(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = Event(slug="fallback", display_name="Fallback")
        session.add(event)
        await session.flush()
        room = Room(
            event_id=event.id,
            display_name="Room",
            floor_language_code="en",
            floor_translation_enabled=True,
            floor_translation_provider="self_hosted",
            floor_translation_model="primary",
            translation_fallback_provider="local",
            translation_fallback_model="nllb-200-distilled-600M",
        )
        session.add(room)
        await session.flush()
        session.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=True))
        segment = TranscriptSegment(room_id=room.id, text="Hello", language_code="en")
        session.add(segment)
        await session.flush()
    primary, backup = Engine(None), Engine("Bonjour")
    monkeypatch.setitem(worker_module.ENGINES, "self_hosted", primary)
    monkeypatch.setitem(worker_module.ENGINES, "local", backup)
    monkeypatch.setattr(worker_module, "SCHEDULERS", {})
    monkeypatch.setattr(circuit_module, "CIRCUITS", {})
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda *args: 1)
    bundle = AsyncMock()
    synthesize = AsyncMock(return_value=b"audio")
    monkeypatch.setattr("portal.websockets.manager.tts_manager.has_listeners", lambda *args: True)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", bundle)
    monkeypatch.setattr("portal.websockets.manager.listener_manager.broadcast", AsyncMock())
    monkeypatch.setattr("portal.tts.worker.synthesize", synthesize)
    worker = TranslationWorker(MagicMock())
    yield SimpleNamespace(
        worker=worker,
        room=room,
        event=event,
        segment=segment,
        primary=primary,
        backup=backup,
        bundle=bundle,
        synthesize=synthesize,
    )
    await dispose()


async def run(env):
    async with get_session() as session:
        segment = TranscriptSegment(room_id=env.room.id, text="Hello", language_code="en")
        session.add(segment)
        await session.flush()
    await env.worker.handle_translation(env.room.id, segment.id, "Hello", "fallback-1-floor", "uuid", segment.id)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, RuntimeError("private upstream response")])
async def test_primary_failure_uses_fallback_and_persists_and_speaks_its_text(pipeline, failure):
    env = pipeline
    env.primary.result = failure
    await run(env)
    async with get_session() as session:
        rows = list(await session.scalars(select(TranscriptTranslation)))
    assert [(row.language_code, row.text) for row in rows] == [("fr", "Bonjour")]
    assert len(env.primary.calls) == len(env.backup.calls) == 1
    assert env.backup.calls[0][1][0].code == "fr"
    assert env.bundle.await_count == 2
    assert all(call.args[1] == "fr" and call.args[7] == "Bonjour" for call in env.bundle.await_args_list)
    env.synthesize.assert_awaited_once_with(env.room.id, "Bonjour", "fr")


@pytest.mark.anyio
async def test_repeated_failure_skips_primary_until_successful_probe(pipeline, monkeypatch):
    clock = Clock()
    circuit = CircuitBreaker(clock=clock)
    monkeypatch.setattr(worker_module, "circuit_for", lambda *args: circuit)
    env = pipeline
    for _ in range(5):
        await run(env)
    assert len(env.primary.calls) == 3 and len(env.backup.calls) == 5
    clock.now = 30
    env.primary.result = "Recovered"
    await run(env)
    assert len(env.primary.calls) == 4 and len(env.backup.calls) == 5
    assert circuit.opened_until is None
    assert env.bundle.await_args.args[7] == "Recovered"


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["", "  ", "Bonjour primaire"])
async def test_empty_or_successful_primary_does_not_use_fallback(pipeline, value):
    env = pipeline
    env.primary.result = value
    await run(env)
    assert len(env.primary.calls) == 1 and not env.backup.calls
    assert env.bundle.await_args.args[8] is None
    if not value.strip():
        env.synthesize.assert_not_awaited()
        async with get_session() as session:
            assert list(await session.scalars(select(TranscriptTranslation))) == []


@pytest.mark.anyio
async def test_timeout_falls_back_while_primary_thread_keeps_its_slot(pipeline, monkeypatch):
    env = pipeline
    started, release = threading.Event(), threading.Event()

    def blocked():
        started.set()
        release.wait(3)
        return [["Too late"]]

    async def translate(*args, **kwargs):
        return await _in_thread(blocked)

    monkeypatch.setattr(env.primary, "translate_batch", translate)
    monkeypatch.setattr(
        worker_module, "_inference_timeout", lambda provider, model: 0.02 if provider == "self_hosted" else 1
    )
    try:
        await run(env)
        assert started.is_set() and len(env.backup.calls) == 1
        scheduler = worker_module.scheduler_for("self_hosted", "primary", env.event.id)
        assert scheduler.running == 1
        assert env.bundle.await_args.args[7] == "Bonjour"
    finally:
        release.set()
    for _ in range(100):
        if scheduler.idle():
            break
        await asyncio.sleep(0.001)
    assert scheduler.idle()


@pytest.mark.anyio
async def test_parent_cancellation_does_not_fallback_or_count_failure(pipeline, monkeypatch):
    env = pipeline
    entered = asyncio.Event()

    async def translate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(env.primary, "translate_batch", translate)
    task = asyncio.create_task(run(env))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.backup.calls
    circuit = next(iter(circuit_module.CIRCUITS.values()))
    assert circuit.failures == 0 and circuit.active == 0
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_failed_fallback_closes_segment_once_without_speaking(pipeline):
    env = pipeline
    env.backup.result = None
    await run(env)
    env.bundle.assert_awaited_once()
    assert env.bundle.await_args.args[8] == "pipeline_failed"
    env.synthesize.assert_not_awaited()


@pytest.mark.anyio
async def test_no_fallback_preserves_existing_failure_behaviour(pipeline):
    env = pipeline
    async with get_session() as session:
        room = await session.get(Room, env.room.id)
        room.translation_fallback_provider = None
        room.translation_fallback_model = None
    await run(env)
    assert len(env.primary.calls) == 1 and not env.backup.calls
    assert env.bundle.await_args.args[8] == "pipeline_failed"
    assert not circuit_module.CIRCUITS


@pytest.mark.anyio
async def test_only_one_half_open_probe_runs_across_worker_instances(pipeline, monkeypatch):
    env = pipeline
    clock = Clock()
    circuit = CircuitBreaker(threshold=1, clock=clock)
    circuit.start().finish(False)
    clock.now = 30
    entered, release = asyncio.Event(), asyncio.Event()

    async def translate(*args, **kwargs):
        entered.set()
        await release.wait()
        return [["Recovered"]]

    monkeypatch.setattr(worker_module, "circuit_for", lambda *args: circuit)
    monkeypatch.setattr(env.primary, "translate_batch", translate)
    probe = asyncio.create_task(run(env))
    await entered.wait()
    try:
        # The real aggregator creates a worker per segment; the circuit must be shared.
        other = SimpleNamespace(**vars(env))
        other.worker = TranslationWorker(MagicMock())
        await run(other)
        assert len(env.backup.calls) == 1 and circuit.probing
    finally:
        release.set()
        await probe
    assert not circuit.probing and circuit.opened_until is None


@pytest.mark.anyio
async def test_missing_primary_key_still_allows_keyless_fallback(pipeline):
    env = pipeline
    async with get_session() as session:
        room = await session.get(Room, env.room.id)
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
    await run(env)
    assert not env.primary.calls and len(env.backup.calls) == 1
    assert env.bundle.await_args.args[7] == "Bonjour"


@pytest.mark.anyio
async def test_fallback_reuses_encrypted_event_key_and_translation_context(pipeline, monkeypatch):
    env = pipeline
    async with get_session() as session:
        room = await session.get(Room, env.room.id)
        room.translation_fallback_provider = "openai"
        room.translation_fallback_model = "gpt-4o-mini"
        room.floor_ai_interpreter_persona = "Conference interpreter"
        room.floor_ai_interpretation_style = "Concise"
        event = await session.get(Event, env.event.id)
        event.encrypted_translation_openai_api_key = encrypt_val("fallback-test-key")
    monkeypatch.setitem(worker_module.ENGINES, "openai", env.backup)
    await run(env)
    call = env.backup.calls[0]
    assert call[-1] == "fallback-test-key"
    assert (call[3].persona, call[3].style) == ("Conference interpreter", "Concise")
    assert call[0].code == "en" and call[1][0].code == "fr"


@pytest.mark.anyio
async def test_open_circuit_does_not_queue_behind_blocked_primary(pipeline, monkeypatch):
    env = pipeline
    circuit = CircuitBreaker(threshold=1)
    circuit.start().finish(False)
    monkeypatch.setattr(worker_module, "circuit_for", lambda *args: circuit)
    scheduler = worker_module.scheduler_for("self_hosted", "primary", env.event.id)
    blocker = scheduler.admit(env.room.id, "busy:fr")
    async with blocker:
        await run(env)
        assert scheduler.running == 1 and not env.primary.calls and len(env.backup.calls) == 1
    blocker.close()


@pytest.mark.anyio
async def test_queued_segments_skip_primary_after_another_segment_opens_the_circuit(pipeline):
    env = pipeline
    env.backup.result = ""  # A valid empty result needs neither persistence nor TTS.
    tasks = [
        env.worker._translate_and_broadcast(
            env.event,
            env.room,
            "self_hosted",
            "primary",
            None,
            "fr",
            "French",
            "en",
            "English",
            env.segment.id,
            "Hello",
            "fallback-1-floor",
            f"uuid-{i}",
            i,
            fallback=FallbackTranslation("local", "nllb-200-distilled-600M", None),
        )
        for i in range(10)
    ]
    await asyncio.gather(*tasks)
    assert len(env.primary.calls) == 3
    assert len(env.backup.calls) == 10
    assert all(scheduler.idle() for scheduler in worker_module.SCHEDULERS.values())


def test_fallback_credentials_are_not_in_configuration_repr():
    assert "private-key" not in repr(FallbackTranslation("openai", "gpt-4o-mini", "private-key"))


def test_idle_open_circuit_retains_its_single_recovery_probe(monkeypatch):
    monkeypatch.setattr(circuit_module, "CIRCUITS", {})
    circuit = circuit_for(1, "local", "primary", "self_hosted", "backup")
    for _ in range(3):
        circuit.start().finish(False)
    now = circuit.last_used + 100
    circuit.clock = lambda: now
    monkeypatch.setattr(circuit_module.time, "monotonic", lambda: now)
    same = circuit_for(1, "local", "primary", "self_hosted", "backup")
    assert same is circuit
    probe = same.start()
    assert probe is not None and probe.probe
    assert same.start() is None
    probe.finish(True)


@pytest.mark.anyio
async def test_downloading_primary_uses_fallback_without_tripping_the_circuit(pipeline, monkeypatch):
    env = pipeline
    async with get_session() as session:
        room = await session.get(Room, env.room.id)
        room.floor_translation_provider = "local"
        room.floor_translation_model = "nllb-200-distilled-600M"
        room.translation_fallback_provider = "self_hosted"
        room.translation_fallback_model = "backup"
    monkeypatch.setitem(worker_module.ENGINES, "local", env.primary)
    monkeypatch.setitem(worker_module.ENGINES, "self_hosted", env.backup)
    downloading = True
    monkeypatch.setattr(
        worker_module, "_inference_timeout", lambda provider, model: None if provider == "local" and downloading else 1
    )
    for _ in range(4):
        await run(env)
    circuit = next(iter(circuit_module.CIRCUITS.values()))
    assert circuit.failures == 0 and circuit.opened_until is None
    assert not env.primary.calls and len(env.backup.calls) == 4
    downloading = False
    env.primary.result = "Ready"
    await run(env)
    assert len(env.primary.calls) == 1 and len(env.backup.calls) == 4
    assert env.bundle.await_args.args[7] == "Ready"
