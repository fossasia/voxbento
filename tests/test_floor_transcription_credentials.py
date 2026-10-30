"""Provider-key updates reach active floor workers without disrupting other credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest

# Tests load the root ASGI entry point; portal.* does not export the application.
from fastapi_app import app
from portal import meeting_source as meeting
from portal import program_ingest as ingest
from portal.auth import create_user_token
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.crypto import decrypt_val, encrypt_val
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room, User
from portal.transcription import worker
from portal.transcription.providers.base import ProviderConfig

OLD_KEY = "old-dummy-provider-key"
NEW_KEY = "replacement-dummy-provider-key"


class DummyFfmpeg:
    """Keep worker lifecycle tests independent of external media processes."""

    def __init__(self, *args: object) -> None:
        """Accept the production process constructor arguments."""

    async def __aenter__(self) -> object:
        """Supply a placeholder process to the recording provider."""
        return object()

    async def __aexit__(self, *args: object) -> None:
        """Finish the simulated process without external resources."""


class RecordingProvider:
    """Capture effective credentials on initial connections and retries."""

    def __init__(self) -> None:
        """Prepare an observable connection queue and controllable failure."""
        self.calls: asyncio.Queue[tuple[str | None, str]] = asyncio.Queue()
        self.retry_requested = asyncio.Event()

    async def run_stream(
        self, process, language_code, model_variant, config, broadcast_callback, booth_id, room_id
    ) -> None:
        """Record a connection and fail only when the test requests a retry."""
        await self.calls.put((config.get_key(), language_code))
        await self.retry_requested.wait()
        self.retry_requested.clear()
        request = httpx.Request("GET", "https://provider.invalid/transcribe")
        raise httpx.HTTPStatusError(
            "Simulated provider connection failure",
            request=request,
            response=httpx.Response(503, request=request),
        )


@dataclass
class Environment:
    """Exercise either floor source through real settings and worker lifecycles."""

    source: str
    event_id: int
    room_id: int
    booth_id: str
    token: str
    provider: RecordingProvider
    clock: float = 1000.0

    async def tick(self) -> None:
        """Reconcile one healthy floor-source update using persisted settings."""
        self.clock += 1
        meeting.audio_progress[self.booth_id] = self.clock
        async with ingest.room_lock(self.room_id), get_session() as session:
            room = await session.get(Room, self.room_id)
            event = await session.get(Event, self.event_id)
            path = ingest.MediaPath(
                name=make_mediamtx_path(event.slug, room.id, "floor"),
                available=True,
                source=ingest.MediaSource(type="webRTCSession", id="dummy-publisher"),
                tracks=["Opus"],
                inboundBytes=int(self.clock),
            )
            if self.source == "meeting":
                await meeting.reconcile_room(room, event, {"state": "healthy", "stage": "in_meeting"}, path)
            else:
                current = ingest.health.setdefault(room.id, ingest.IngestHealth())
                current.last_progress = self.clock
                await ingest.reconcile_processing(room, event, path, current, self.booth_id, self.clock)

    async def save_key(self, key: str, *, field: str = "deepgram_api_key", event_id: int | None = None) -> None:
        """Save credentials through the authenticated settings HTTP endpoint."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"user_token": self.token},
        ) as client:
            response = await client.post(
                f"/admin/events/{event_id or self.event_id}/api-settings",
                data={"transcription_api_enabled": "true", field: key},
                headers={"origin": "http://test"},
            )
        assert response.status_code == 303, response.text

    async def next_call(self) -> tuple[str | None, str]:
        """Await the next provider connection with a bounded failure timeout."""
        return await asyncio.wait_for(self.provider.calls.get(), timeout=3)


@pytest.fixture(params=["meeting", "program"])
async def environment(request, monkeypatch):
    """Isolate the database and workers for each managed floor source."""
    configure("sqlite+aiosqlite://")
    await init_db()
    provider = RecordingProvider()
    monkeypatch.setitem(worker.PROVIDERS, "deepgram", provider)
    monkeypatch.setattr(worker, "FfmpegProcess", DummyFfmpeg)
    monkeypatch.setattr(worker, "active_workers", {})
    monkeypatch.setattr(meeting, "health", {})
    monkeypatch.setattr(ingest, "health", {})
    monkeypatch.setattr(ingest, "room_locks", {})
    progress: dict[str, float] = {}
    monkeypatch.setattr(meeting, "audio_progress", progress)
    monkeypatch.setattr(ingest, "audio_progress", progress)
    async with get_session() as session:
        user = User(email="rotation-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        event = Event(
            slug="rotation-test",
            display_name="Rotation Test",
            transcription_api_enabled=True,
            encrypted_deepgram_api_key=encrypt_val(OLD_KEY),
        )
        session.add_all([user, event])
        await session.flush()
        room = Room(
            event_id=event.id,
            display_name="Main Hall",
            floor_transcription_enabled=True,
            floor_transcription_provider="deepgram",
            floor_transcription_model="nova-2",
            floor_language_code="en",
            floor_source="jitsi_bot" if request.param == "meeting" else "program_ingest",
            meeting_agent_enabled=request.param == "meeting",
        )
        session.add(room)
        await session.flush()
        env = Environment(
            request.param,
            event.id,
            room.id,
            make_booth_id(event.slug, room.id, "floor"),
            create_user_token(user_id=user.id, email=user.email, is_admin=True),
            provider,
        )
    monkeypatch.setattr(meeting, "monotonic", lambda: env.clock)
    try:
        yield env
    finally:
        await worker.stop_transcription_worker(env.booth_id)
        await dispose()


@pytest.mark.anyio
async def test_key_rotation_replaces_active_worker_and_its_retry_credentials(environment: Environment) -> None:
    """A saved replacement reaches both the active worker and later retries."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]

    await env.save_key(NEW_KEY)
    async with get_session() as session:
        event = await session.get(Event, env.event_id)
        assert decrypt_val(event.encrypted_deepgram_api_key) == NEW_KEY
    await env.tick()
    assert await env.next_call() == (NEW_KEY, "en")
    current = worker.active_workers[env.booth_id]
    assert current is not original
    assert original.state == worker.State.STOPPED

    for _ in range(3):
        await env.tick()
    assert worker.active_workers[env.booth_id] is current
    current_health = meeting.health[env.room_id] if env.source == "meeting" else ingest.health[env.room_id]
    assert NEW_KEY not in repr(current_health)

    env.provider.retry_requested.set()
    assert await env.next_call() == (NEW_KEY, "en")


@pytest.mark.anyio
async def test_resaving_same_key_does_not_replace_active_worker(environment: Environment) -> None:
    """Fernet re-encryption of the same plaintext must not trigger replacement."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]
    async with get_session() as session:
        old_ciphertext = (await session.get(Event, env.event_id)).encrypted_deepgram_api_key

    await env.save_key(OLD_KEY)
    async with get_session() as session:
        event = await session.get(Event, env.event_id)
        assert event.encrypted_deepgram_api_key != old_ciphertext
        assert decrypt_val(event.encrypted_deepgram_api_key) == OLD_KEY
    await env.tick()
    assert worker.active_workers[env.booth_id] is original
    assert env.provider.calls.empty()


@pytest.mark.anyio
async def test_updating_unused_provider_key_does_not_replace_active_worker(environment: Environment) -> None:
    """Only the provider selected for the floor controls credential refresh."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]

    await env.save_key(NEW_KEY, field="openai_api_key")
    await env.tick()
    assert worker.active_workers[env.booth_id] is original
    assert env.provider.calls.empty()


@pytest.mark.anyio
async def test_updating_another_event_key_does_not_replace_active_worker(environment: Environment) -> None:
    """Credential changes remain isolated to the event owning the worker."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]
    async with get_session() as session:
        event = Event(slug="another-event", display_name="Another event")
        session.add(event)
        await session.flush()
        other_id = event.id

    await env.save_key(NEW_KEY, event_id=other_id)
    await env.tick()
    assert worker.active_workers[env.booth_id] is original
    assert env.provider.calls.empty()


@pytest.mark.anyio
async def test_language_change_still_replaces_active_worker(environment: Environment) -> None:
    """Credential refresh preserves existing replacement on language changes."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]
    async with get_session() as session:
        room = await session.get(Room, env.room_id)
        room.floor_language_code = "de"
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "de")
    assert worker.active_workers[env.booth_id] is not original


@pytest.mark.anyio
async def test_simultaneous_key_refresh_starts_only_one_replacement(environment: Environment) -> None:
    """Concurrent refresh requests share one serialized replacement worker."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]

    async def refresh() -> bool:
        """Request the same replacement credential from a concurrent caller."""
        return await worker.start_transcription_worker(
            "rotation-test",
            "floor",
            env.booth_id,
            AsyncMock(),
            "deepgram",
            "nova-2",
            ProviderConfig(api_key=NEW_KEY),
            transcription_language="en",
            room_id=env.room_id,
            restart_on_key_change=True,
        )

    results = await asyncio.wait_for(asyncio.gather(refresh(), refresh()), timeout=5)
    assert sorted(results) == [False, True]
    assert await env.next_call() == (NEW_KEY, "en")
    assert env.provider.calls.empty()
    assert original.state == worker.State.STOPPED


@pytest.mark.anyio
async def test_key_refresh_is_opt_in_for_other_worker_callers(environment: Environment) -> None:
    """Existing callers retain their running worker unless they opt into refresh."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]

    started = await worker.start_transcription_worker(
        "rotation-test",
        "floor",
        env.booth_id,
        AsyncMock(),
        "deepgram",
        "nova-2",
        ProviderConfig(api_key=NEW_KEY),
        room_id=env.room_id,
    )
    assert started is False
    assert worker.active_workers[env.booth_id] is original
    assert original.config.get_key() == OLD_KEY
    assert env.provider.calls.empty()


@pytest.mark.anyio
async def test_key_rotation_resets_worker_start_time(environment: Environment) -> None:
    """Replacement workers receive a fresh startup grace period."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    current_health = meeting.health[env.room_id] if env.source == "meeting" else ingest.health[env.room_id]
    current_health.worker_started_at = env.clock - 100

    await env.save_key(NEW_KEY)
    await env.tick()
    assert await env.next_call() == (NEW_KEY, "en")
    assert current_health.worker_started_at == env.clock


@pytest.mark.anyio
async def test_key_refresh_waits_for_stopped_worker_cleanup(environment: Environment, monkeypatch) -> None:
    """A STOPPED session must finish its asynchronous cleanup before replacement."""
    env = environment
    await env.tick()
    assert await env.next_call() == (OLD_KEY, "en")
    original = worker.active_workers[env.booth_id]
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    replacement_waiting = asyncio.Event()
    original_wait = original.wait_until_stopped

    async def enqueue_webhook(event_name: str, payload: dict) -> None:
        """Pause the old session after STOPPED while its task still owns cleanup."""
        if event_name == "booth.transcription.stopped" and payload["session_id"] == original.session_id:
            cleanup_started.set()
            await release_cleanup.wait()

    async def wait_until_stopped() -> None:
        """Signal that replacement is waiting for the old task's completion."""
        replacement_waiting.set()
        await original_wait()

    monkeypatch.setattr(worker._wh_worker, "enqueue_webhook", enqueue_webhook)
    monkeypatch.setattr(original, "wait_until_stopped", wait_until_stopped)
    original.stop()
    await asyncio.wait_for(cleanup_started.wait(), timeout=3)
    assert original.state == worker.State.STOPPED
    assert not original.task.done()

    refresh = asyncio.create_task(
        worker.start_transcription_worker(
            "rotation-test",
            "floor",
            env.booth_id,
            AsyncMock(),
            "deepgram",
            "nova-2",
            ProviderConfig(api_key=NEW_KEY),
            transcription_language="en",
            room_id=env.room_id,
            restart_on_key_change=True,
        )
    )
    try:
        await asyncio.wait_for(replacement_waiting.wait(), timeout=3)
        assert worker.active_workers[env.booth_id] is original
        assert not refresh.done()
        assert env.provider.calls.empty()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(refresh, timeout=3)

    assert original.task.done()
    assert refresh.result() is True
    assert await env.next_call() == (NEW_KEY, "en")
