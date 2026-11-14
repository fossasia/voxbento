"""Tests for the /api/v1 room transcription stop and status routes.

Both routes used to call make_booth_id without room_id and raised TypeError on
every request. The status route additionally called a BoothRegistry method that
does not exist, and read transcription state from an attribute nothing sets.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portal.routers.api_v1 as api_v1
from portal.auth import require_oauth_scope
from portal.booth_identity import make_booth_id
from portal.config import Settings, settings
from portal.crypto import encrypt_val
from portal.database import create_booth, create_event, create_room
from portal.models import Base, OAuthToken, TranscriptSegment, TranscriptTranslation
from portal.transcription.providers.base import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.providers.local import _active_booths_per_model
from portal.transcription.worker import (
    ERROR_CODE_SPAWN_FAILED,
    ERROR_CODE_STOPPING_TIMEOUT,
    State,
    TranscriptionStartResult,
    TranscriptionWorkerSession,
    _effective_language,
    _languages_match,
    _worker_config_matches,
    active_workers,
    active_workers_lock,
    start_transcription_worker,
    stop_transcription_worker,
)

EVENT_SLUG = "pycon2026"
ROOM_ID = 3


def _result(first=None, all_=()):
    """Mimic the SQLAlchemy result shape these routes read from."""
    result = MagicMock()
    result.scalars.return_value.first.return_value = first
    result.scalars.return_value.all.return_value = list(all_)
    return result


def _db(*results):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock()
    return db


@pytest.fixture
def event():
    return SimpleNamespace(
        id=1,
        slug=EVENT_SLUG,
        transcription_api_enabled=False,
        encrypted_openai_api_key=None,
        encrypted_deepgram_api_key=None,
        encrypted_nvidia_api_key=None,
        encrypted_elevenlabs_api_key=None,
        encrypted_atlascloud_api_key=None,
    )


@pytest.fixture(autouse=True)
def _skip_rbac():
    # Authorisation is covered elsewhere; these tests are about what happens
    # after the caller has been let in.
    class _DefaultMockSessionContext:
        async def __aenter__(self):
            session = MagicMock()
            session.scalar = AsyncMock(return_value=1)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with (
        patch.object(api_v1, "_verify_token_rbac", AsyncMock()),
        patch.object(api_v1, "get_session", return_value=_DefaultMockSessionContext()),
    ):
        yield


@pytest.fixture
def running_worker():
    """Register a transcription worker for one booth, as start would."""
    registered = []

    def _register(booth_id):
        active_workers[booth_id] = SimpleNamespace(is_running=True)
        registered.append(booth_id)

    yield _register
    for booth_id in registered:
        active_workers.pop(booth_id, None)


async def _cleanup_test_worker(booth_id: str):
    """Safely stop and wait for a test-owned worker session before patches unbind."""
    session = active_workers.get(booth_id)
    if session is None:
        return

    from portal.transcription.worker import stop_transcription_worker

    stopped_cleanly = False
    try:
        stopped = await asyncio.wait_for(stop_transcription_worker(booth_id), timeout=2.0)
        assert stopped is True, f"stop_transcription_worker returned False for {booth_id}"
        task = getattr(session, "task", None)
        if task is not None and task.done():
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    raise exc
            stopped_cleanly = True
        elif task is None:
            stopped_cleanly = True
    except TimeoutError:
        # Stop timed out: cancel task as last resort
        task = getattr(session, "task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except (TimeoutError, asyncio.CancelledError):
                pass
            if task.done():
                stopped_cleanly = True
        raise AssertionError(f"worker did not stop within 2s: {booth_id}")
    finally:
        task = getattr(session, "task", None)
        if stopped_cleanly and (task is None or task.done()):
            if active_workers.get(booth_id) is session:
                active_workers.pop(booth_id, None)


# ── stop ──────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stop_uses_the_room_scoped_booth_id(event):
    stop = AsyncMock()
    with patch.object(api_v1, "stop_transcription_worker", stop):
        response = await api_v1.stop_transcription(
            EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=event)), token=object()
        )

    assert response == {"status": "stopped", "booth_id": "pycon2026-3-en"}
    stop.assert_awaited_once_with("pycon2026-3-en")


@pytest.mark.anyio
async def test_stop_for_an_unknown_event_is_a_404():
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.stop_transcription(EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=None)), token=object())

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_delete_booth_stops_transcription_worker_with_correct_booth_id(event):
    """Deleting a booth stops its transcription worker before deleting the database row."""
    from portal.booth_identity import make_booth_id

    booth = SimpleNamespace(id=42, room_id=ROOM_ID, language_code="en", event_id=event.id)
    db = _db(
        _result(first=event),
        _result(first=booth),
    )

    calls = []

    async def mock_stop(booth_id):
        calls.append(("stop", booth_id))

    async def mock_revoke(db, level, target_id):
        calls.append(("revoke", level, target_id))

    async def mock_delete(db, booth_id):
        calls.append(("delete", booth_id))

    with (
        patch("portal.routers.api_v1.stop_transcription_worker", side_effect=mock_stop),
        patch("portal.routers.api_v1.revoke_booths_for_deleted_entity", side_effect=mock_revoke),
        patch("portal.database.delete_booth", side_effect=mock_delete),
    ):
        resp = await api_v1.delete_booth_endpoint(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert resp == {"status": "deleted"}
    expected_booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    assert calls == [
        ("revoke", "booth", 42),
        ("delete", 42),
        ("stop", expected_booth_id),
    ]


@pytest.mark.anyio
async def test_delete_booth_canonicalizes_uppercase_language(event):
    """Deleting a booth with uppercase language canonicalizes it and stops the canonical booth ID."""
    from portal.booth_identity import make_booth_id

    booth = SimpleNamespace(id=42, room_id=ROOM_ID, language_code="en", event_id=event.id)
    db = _db(
        _result(first=event),
        _result(first=booth),
    )

    calls = []

    async def mock_stop(booth_id):
        calls.append(("stop", booth_id))

    with (
        patch("portal.routers.api_v1.stop_transcription_worker", side_effect=mock_stop),
        patch("portal.routers.api_v1.revoke_booths_for_deleted_entity", AsyncMock()),
        patch("portal.database.delete_booth", AsyncMock()),
    ):
        resp = await api_v1.delete_booth_endpoint(EVENT_SLUG, ROOM_ID, "EN", db=db, token=object())

    assert resp == {"status": "deleted"}
    expected_booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    assert calls == [("stop", expected_booth_id)]


@pytest.mark.anyio
async def test_delete_booth_succeeds_when_no_worker_running(event):
    """Deleting a booth succeeds cleanly even when no transcription worker was active."""
    booth = SimpleNamespace(id=42, room_id=ROOM_ID, language_code="en", event_id=event.id)
    db = _db(
        _result(first=event),
        _result(first=booth),
    )

    with (
        patch("portal.routers.api_v1.revoke_booths_for_deleted_entity", AsyncMock()),
        patch("portal.database.delete_booth", AsyncMock()),
    ):
        resp = await api_v1.delete_booth_endpoint(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert resp == {"status": "deleted"}


# ── status ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_status_reports_every_booth_in_the_room(event):
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["room_id"] == ROOM_ID
    assert set(response["statuses"]) == {"en", "fr"}


@pytest.mark.anyio
async def test_status_reports_transcription_running_from_the_worker_registry(event, running_worker):
    running_worker("pycon2026-3-en")
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    # This was always False before: it read an attribute Booth does not have.
    assert response["statuses"]["en"]["transcription_running"] is True
    assert response["statuses"]["fr"]["transcription_running"] is False


@pytest.mark.anyio
async def test_status_does_not_count_a_worker_in_another_room(event, running_worker):
    running_worker("pycon2026-4-en")
    db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["statuses"]["en"]["transcription_running"] is False


@pytest.mark.anyio
async def test_status_reports_whether_the_booth_is_live_in_memory(event):
    db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))
    live = SimpleNamespace(booth_id="pycon2026-3-en")

    with patch.object(
        api_v1.booths, "get_booth_sync", side_effect=lambda bid: live if bid == "pycon2026-3-en" else None
    ):
        response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["statuses"]["en"]["is_active"] is True


@pytest.mark.anyio
async def test_status_for_a_room_with_no_booths_is_empty(event):
    db = _db(_result(first=event), _result(all_=[]))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response == {"room_id": ROOM_ID, "statuses": {}}


@pytest.mark.anyio
async def test_status_reports_false_for_stopping_and_stopped_workers(event):
    """Workers in STOPPING or STOPPED state are not reported as transcription_running."""
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import TranscriptionWorkerSession

    booth_en = "pycon2026-3-en"
    booth_fr = "pycon2026-3-fr"
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    stopping_worker = TranscriptionWorkerSession(
        "pycon2026", "en", booth_en, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    stopping_worker.state = State.STOPPING
    stopped_worker = TranscriptionWorkerSession(
        "pycon2026", "fr", booth_fr, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    stopped_worker.state = State.STOPPED
    active_workers[booth_en] = stopping_worker
    active_workers[booth_fr] = stopped_worker

    try:
        response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())
        assert response["statuses"]["en"]["transcription_running"] is False
        assert response["statuses"]["fr"]["transcription_running"] is False
    finally:
        active_workers.pop(booth_en, None)
        active_workers.pop(booth_fr, None)


@pytest.mark.anyio
async def test_status_reports_false_for_starting_worker_with_error_or_dead_task(event):
    """A worker in STARTING state, with a recorded start_error, or whose task has exited is not running."""
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import TranscriptionWorkerSession

    booth_en = "pycon2026-3-en"
    booth_fr = "pycon2026-3-fr"
    booth_es = "pycon2026-3-es"
    booths_in_room = [
        SimpleNamespace(language_code="en"),
        SimpleNamespace(language_code="fr"),
        SimpleNamespace(language_code="es"),
    ]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    starting_worker = TranscriptionWorkerSession(
        "pycon2026", "en", booth_en, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    starting_worker.state = State.STARTING

    errored_worker = TranscriptionWorkerSession(
        "pycon2026", "fr", booth_fr, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    errored_worker.state = State.RUNNING
    errored_worker.start_error = "ffmpeg failed"

    done_task = MagicMock()
    done_task.done.return_value = True
    dead_task_worker = TranscriptionWorkerSession(
        "pycon2026", "es", booth_es, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    dead_task_worker.state = State.RUNNING
    dead_task_worker.task = done_task

    active_workers[booth_en] = starting_worker
    active_workers[booth_fr] = errored_worker
    active_workers[booth_es] = dead_task_worker

    try:
        response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())
        assert response["statuses"]["en"]["transcription_running"] is False
        assert response["statuses"]["fr"]["transcription_running"] is False
        assert response["statuses"]["es"]["transcription_running"] is False
    finally:
        active_workers.pop(booth_en, None)
        active_workers.pop(booth_fr, None)
        active_workers.pop(booth_es, None)


@pytest.mark.anyio
async def test_status_reports_true_for_running_session(event):
    """An active session with State.RUNNING, no start error, and live task reports True."""
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import TranscriptionWorkerSession

    booth_en = "pycon2026-3-en"
    booths_in_room = [SimpleNamespace(language_code="en")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    live_task = MagicMock()
    live_task.done.return_value = False
    running_worker = TranscriptionWorkerSession(
        "pycon2026", "en", booth_en, MagicMock(), "local", "tiny", ProviderConfig(api_key=None)
    )
    running_worker.state = State.RUNNING
    running_worker.task = live_task
    active_workers[booth_en] = running_worker

    try:
        response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())
        assert response["statuses"]["en"]["transcription_running"] is True
    finally:
        active_workers.pop(booth_en, None)


# ── status against a real database ────────────────────────────────────────


@pytest.fixture
async def real_db():
    """An in-memory SQLite session, as in test_database.py."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.anyio
async def test_status_reports_only_the_requested_rooms_booths(real_db, running_worker):
    """The mocked tests above hand the route preselected rows, so they cannot
    catch a missing or wrong room filter. Two rooms with overlapping languages
    do: only the requested room's booths may appear, and only its worker may
    count as running."""
    event = await create_event(real_db, slug=EVENT_SLUG, display_name="PyCon 2026")
    room_a = await create_room(real_db, event_id=event.id, display_name="Hall A")
    room_b = await create_room(real_db, event_id=event.id, display_name="Hall B")
    await create_booth(real_db, event_id=event.id, room_id=room_a.id, language_code="en", language_name="English")
    await create_booth(real_db, event_id=event.id, room_id=room_b.id, language_code="en", language_name="English")
    await create_booth(real_db, event_id=event.id, room_id=room_b.id, language_code="de", language_name="German")
    running_worker(f"{EVENT_SLUG}-{room_b.id}-en")

    response = await api_v1.get_transcription_status(EVENT_SLUG, room_a.id, db=real_db, token=object())

    assert response["room_id"] == room_a.id
    assert set(response["statuses"]) == {"en"}
    assert response["statuses"]["en"]["transcription_running"] is False


# ── export ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_export_returns_segments_for_a_booth(event):
    """Source segments are returned chronologically in a structured envelope."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Hello world", created_at=now),
        SimpleNamespace(id=2, text="Second segment", created_at=now),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response == {
        "event_slug": EVENT_SLUG,
        "room_id": ROOM_ID,
        "language_code": "en",
        "target_lang": None,
        "segments": [
            {"id": 1, "text": "Hello world", "created_at": now.isoformat()},
            {"id": 2, "text": "Second segment", "created_at": now.isoformat()},
        ],
    }


@pytest.mark.anyio
async def test_export_returns_empty_list_when_no_segments(event):
    """An empty segment list is returned when no transcript rows exist."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    db = _db(_result(first=event), _result(first=booth), _result(all_=[]))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response["segments"] == []


@pytest.mark.anyio
async def test_export_404_for_unknown_event():
    """Missing event raises HTTP 404."""
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=None)), token=object())

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Event not found"


@pytest.mark.anyio
async def test_export_404_for_soft_deleted_event(real_db):
    """Soft-deleted event is excluded by deleted_at filter and raises HTTP 404."""
    event = await create_event(real_db, slug=EVENT_SLUG, display_name="PyCon 2026")
    event.deleted_at = datetime.now(timezone.utc)
    await real_db.flush()

    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=real_db, token=object())

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Event not found"


@pytest.mark.anyio
async def test_export_404_for_unknown_booth(event):
    """Missing booth in event/room raises HTTP 404."""
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.export_transcript(
            EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=event), _result(first=None)), token=object()
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Booth not found"


@pytest.mark.anyio
async def test_export_returns_translations_when_target_lang_given(event):
    """Target language query returns translation text with source segment id and created_at."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    segment = SimpleNamespace(id=5, created_at=now)
    translation = SimpleNamespace(id=101, text="Bonjour le monde")
    result_mock = MagicMock()
    result_mock.all.return_value = [(segment, translation)]
    db = _db(_result(first=event), _result(first=booth), result_mock)

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", target_lang="fr", db=db, token=object())

    assert response == {
        "event_slug": EVENT_SLUG,
        "room_id": ROOM_ID,
        "language_code": "en",
        "target_lang": "fr",
        "segments": [
            {"id": 5, "text": "Bonjour le monde", "created_at": now.isoformat()},
        ],
    }


@pytest.mark.anyio
async def test_export_returns_empty_when_no_translations_for_target_lang(event):
    """Target language query returns empty segments when no translations match."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    result_mock = MagicMock()
    result_mock.all.return_value = []
    db = _db(_result(first=event), _result(first=booth), result_mock)

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", target_lang="fr", db=db, token=object())

    assert response["segments"] == []


@pytest.mark.anyio
async def test_export_segments_ordered_by_created_at(real_db):
    """Segments and translations are ordered chronologically and tie-broken by id."""
    event = await create_event(real_db, slug=EVENT_SLUG, display_name="PyCon 2026")
    room = await create_room(real_db, event_id=event.id, display_name="Hall A")
    await create_booth(real_db, event_id=event.id, room_id=room.id, language_code="en", language_name="English")

    t1 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 1, 10, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 8, 1, 10, 2, 0, tzinfo=timezone.utc)

    # Insert out of chronological order so insertion order (seg1, seg2, seg3, seg4)
    # differs from expected chronological order (seg2, seg3, seg1, seg4).
    seg1 = TranscriptSegment(room_id=room.id, language_code="en", text="Later timestamp, inserted first", created_at=t2)
    seg2 = TranscriptSegment(
        room_id=room.id, language_code="en", text="Earlier timestamp, inserted second", created_at=t1
    )
    seg3 = TranscriptSegment(
        room_id=room.id, language_code="en", text="Earlier timestamp tie, inserted third", created_at=t1
    )
    seg4 = TranscriptSegment(
        room_id=room.id, language_code="en", text="Latest timestamp, inserted fourth", created_at=t3
    )
    real_db.add_all([seg1, seg2, seg3, seg4])
    await real_db.flush()

    response = await api_v1.export_transcript(EVENT_SLUG, room.id, "en", db=real_db, token=object())

    # Verified ordered by (created_at, id), NOT insertion/id order [seg1, seg2, seg3, seg4]
    assert [s["id"] for s in response["segments"]] == [seg2.id, seg3.id, seg1.id, seg4.id]
    assert [s["text"] for s in response["segments"]] == [
        "Earlier timestamp, inserted second",
        "Earlier timestamp tie, inserted third",
        "Later timestamp, inserted first",
        "Latest timestamp, inserted fourth",
    ]

    # Add translations out of order with varying translation timestamps
    trans1 = TranscriptTranslation(segment_id=seg1.id, language_code="es", text="Medio", created_at=t3)
    trans2 = TranscriptTranslation(segment_id=seg2.id, language_code="es", text="Primero", created_at=t2)
    trans3 = TranscriptTranslation(segment_id=seg3.id, language_code="es", text="Primero-atado", created_at=t1)
    real_db.add_all([trans1, trans2, trans3])
    await real_db.flush()

    response_es = await api_v1.export_transcript(
        EVENT_SLUG, room.id, "en", target_lang="es", db=real_db, token=object()
    )
    assert [s["id"] for s in response_es["segments"]] == [seg2.id, seg3.id, seg1.id]
    assert [s["text"] for s in response_es["segments"]] == ["Primero", "Primero-atado", "Medio"]
    assert [s["created_at"] for s in response_es["segments"]] == [t1.isoformat(), t1.isoformat(), t2.isoformat()]


@pytest.mark.anyio
async def test_export_normalizes_naive_timestamps_to_utc(event):
    """Naive created_at timestamps are normalized to UTC while aware timestamps preserve offsets."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    naive_dt = datetime(2026, 8, 1, 12, 0, 0)
    aware_dt = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    segments = [
        SimpleNamespace(id=1, text="Naive", created_at=naive_dt),
        SimpleNamespace(id=2, text="Aware", created_at=aware_dt),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response["segments"][0]["created_at"] == "2026-08-01T12:00:00+00:00"
    assert response["segments"][1]["created_at"] == "2026-08-01T12:00:00+05:30"


@pytest.mark.anyio
async def test_export_normalizes_naive_timestamps_in_translations(event):
    """Naive segment timestamps in translation export are normalized to UTC."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    naive_dt = datetime(2026, 8, 1, 12, 0, 0)
    seg = SimpleNamespace(id=5, created_at=naive_dt)
    trans = SimpleNamespace(id=101, text="Traduction")

    result_mock = MagicMock()
    result_mock.all.return_value = [(seg, trans)]
    db = _db(_result(first=event), _result(first=booth), result_mock)

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", target_lang="fr", db=db, token=object())

    assert response["segments"][0]["created_at"] == "2026-08-01T12:00:00+00:00"


def test_export_rejects_empty_target_lang():
    """Empty target_lang query param fails validation with HTTP 422."""
    app = FastAPI()
    app.include_router(api_v1.router)
    route = next(r for r in api_v1.router.routes if getattr(r, "path", "").endswith("/transcripts/export"))
    auth_dep = route.dependant.dependencies[1].call
    app.dependency_overrides[auth_dep] = lambda: None

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/api/v1/events/{EVENT_SLUG}/rooms/{ROOM_ID}/booths/en/transcripts/export?target_lang=")
    assert response.status_code == 422
    errors = response.json().get("detail", [])
    assert any(err.get("loc") == ["query", "target_lang"] for err in errors)


# ── start ─────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_start_success_invokes_worker_with_resolved_config(event):
    """Success path resolves booth config and invokes worker with correct parameters."""
    start_result = api_v1.TranscriptionStartResult(
        status="started",
        provider="local",
        model="tiny",
    )
    start_mock = AsyncMock(return_value=start_result)
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", start_mock),
    ):
        response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response == {
        "status": "started",
        "already_running": False,
        "booth_id": "pycon2026-3-en",
        "provider": "local",
        "model": "tiny",
    }
    start_mock.assert_awaited_once()
    args, kwargs = start_mock.await_args
    assert len(args) == 7
    assert args[0] == EVENT_SLUG
    assert args[1] == "en"
    assert args[2] == "pycon2026-3-en"
    assert args[3] == api_v1.broadcast_transcription
    assert args[4] == "local"
    assert args[5] == "tiny"
    _, _, expected_config = api_v1.resolve_transcription_config(db_booth, event=event)
    assert args[6] == expected_config
    assert kwargs.get("room_id") == ROOM_ID


@pytest.mark.anyio
async def test_start_uppercase_language_canonicalized_and_reaches_worker(event):
    """Calling start_transcription with uppercase language canonicalizes the code and invokes worker."""
    start_result = api_v1.TranscriptionStartResult(
        status="started",
        provider="local",
        model="tiny",
    )
    start_mock = AsyncMock(return_value=start_result)
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", start_mock),
    ):
        response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "EN", db=db, token=object())

    assert response["status"] == "started"
    start_mock.assert_awaited_once()
    args, _ = start_mock.await_args
    assert args[1] == "en"
    assert args[2] == "pycon2026-3-en"
    query_stmt = db.execute.call_args_list[1][0][0]
    compiled_params = query_stmt.compile().params
    assert "en" in compiled_params.values()
    assert compiled_params.get("language_code_1") == "en"


@pytest.mark.anyio
async def test_start_external_provider_resolves_api_key(event):
    """External provider resolves API key and passes it in ProviderConfig to worker."""
    event.transcription_api_enabled = True
    start_result = api_v1.TranscriptionStartResult(
        status="started",
        provider="openai",
        model="whisper-1",
    )
    start_mock = AsyncMock(return_value=start_result)
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="openai",
        transcription_model="whisper-1",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", start_mock),
        patch("portal.routers.api.get_api_key", return_value="known-key") as mock_get_key,
    ):
        response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    mock_get_key.assert_called_once_with(event, ProviderEnum.OPENAI)
    assert response == {
        "status": "started",
        "already_running": False,
        "booth_id": "pycon2026-3-en",
        "provider": "openai",
        "model": "whisper-1",
    }
    start_mock.assert_awaited_once()
    args, _ = start_mock.await_args
    assert len(args) == 7
    assert args[6].api_key == "known-key"


@pytest.mark.anyio
async def test_start_invalid_language_code_raises_400(event):
    """Calling start_transcription with an invalid language code raises 400 Bad Request."""
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.start_transcription(
            EVENT_SLUG, ROOM_ID, "xx-garbage", db=_db(_result(first=event)), token=object()
        )

    assert exc_info.value.status_code == 400
    assert "Language code must be exactly two lowercase ASCII letters" in exc_info.value.detail


@pytest.mark.anyio
async def test_start_booth_exists_only_in_db_succeeds(event):
    """API v1 start for a booth that exists only in DB (not in memory) succeeds."""
    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    db_booth = SimpleNamespace(
        id=42,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    class DummySessionContext:
        async def __aenter__(self):
            session = MagicMock()
            session.scalar = AsyncMock(return_value=db_booth.id)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def dummy_aenter(self):
        self.process = MagicMock(pid=1234)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    try:
        # Booth does NOT exist in memory (get_booth_sync returns None)
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=None),
            patch.object(api_v1, "get_session", return_value=DummySessionContext()),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
            assert response["status"] == "started"
            assert response["booth_id"] == booth_id
            assert booth_id in active_workers
    finally:
        await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_start_racing_delete_committing_before_registration_registers_no_worker(event):
    """When a delete commits between route DB read and worker registration, start returns 404 and no worker is registered."""
    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        id=88,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    class DeletedSessionContext:
        async def __aenter__(self):
            session = MagicMock()
            # Delete committed in DB: row is gone
            session.scalar = AsyncMock(return_value=None)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    # Notice: booth is still in memory (mock_booth), but deleted in DB
    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "get_session", return_value=DeletedSessionContext()),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Booth is no longer active"
        assert booth_id not in active_workers


@pytest.mark.anyio
async def test_start_disabled_transcription_returns_disabled(event):
    """If transcription is disabled for the booth in DB, return disabled status."""
    db_booth = SimpleNamespace(
        transcription_enabled=False,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with patch.object(api_v1.booths, "get_booth_sync", return_value=None):
        response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response["status"] == "disabled"
    assert "not enabled" in response["message"]


@pytest.mark.anyio
async def test_start_capacity_limit_raises_429(event):
    """When the worker raises ValueError (e.g. system at maximum capacity), route raises 429."""
    start_mock = AsyncMock(side_effect=ValueError("System at maximum capacity (10 concurrent transcription booths)."))
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", start_mock),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert exc_info.value.status_code == 429
    assert "maximum capacity" in exc_info.value.detail


@pytest.mark.anyio
async def test_start_missing_rbac_raises_403(event):
    """When token RBAC verification fails, route raises 403 Forbidden."""
    with patch.object(
        api_v1,
        "_verify_token_rbac",
        AsyncMock(side_effect=api_v1.HTTPException(status_code=403, detail="User lost RBAC access to this resource")),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=event)), token=object())
    assert exc_info.value.status_code == 403
    assert "RBAC access" in exc_info.value.detail


def test_start_transcription_missing_auth_returns_401():
    """Unauthenticated requests without a bearer token fail with HTTP 401."""
    app = FastAPI()
    app.include_router(api_v1.router)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(f"/api/v1/events/{EVENT_SLUG}/rooms/{ROOM_ID}/booths/en/transcription/start")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_start_transcription_scope_checker_rejects_missing_scope():
    """OAuth dependency requires sessions:manage scope and rejects tokens without it."""

    dep = require_oauth_scope("sessions:manage")
    mock_token = OAuthToken(
        id=1,
        client_id="client1",
        user_id=1,
        event_id=1,
        scopes=["sessions:read"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    mock_token.client = SimpleNamespace(status="active")
    db = _db(_result(first=mock_token))
    credentials = SimpleNamespace(credentials="token_secret")

    with pytest.raises(api_v1.HTTPException) as exc_info:
        await dep(request=MagicMock(), db=db, token=credentials)

    assert exc_info.value.status_code == 403
    assert "missing required scope" in exc_info.value.detail.lower()


@pytest.mark.anyio
async def test_start_ffmpeg_launch_failure_returns_500_and_not_counted_running(event):
    """When FFmpeg fails to launch, route returns 500 and worker is not counted as running."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch(
            "portal.transcription.worker.FfmpegProcess.__aenter__",
            side_effect=FileNotFoundError("ffmpeg binary not found"),
        ),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert exc_info.value.status_code == 500
        assert "Failed to start transcription worker" in exc_info.value.detail

    assert booth_id not in active_workers
    status_db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))
    status_resp = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=status_db, token=object())
    assert status_resp["statuses"]["en"]["transcription_running"] is False


@pytest.mark.anyio
async def test_start_already_running_same_config_returns_already_running(event):
    """When a worker is already running with identical config, route returns already_running status."""

    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    existing_session = SimpleNamespace(
        provider_name="local",
        model_size="tiny",
        transcription_language=None,
        language_code="en",
        config=ProviderConfig(api_key=None),
        state=State.RUNNING,
        task=None,
        stop=MagicMock(),
        wait_until_stopped=AsyncMock(),
    )
    active_workers[booth_id] = existing_session

    try:
        with patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth):
            response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

        assert response == {
            "status": "started",
            "already_running": True,
            "booth_id": booth_id,
            "provider": "local",
            "model": "tiny",
        }
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_already_running_rotated_api_key_restarts_worker(event):
    """When a worker is running but API key is rotated, worker is restarted with new settings."""

    booth_id = "pycon2026-3-en"
    event.transcription_api_enabled = True
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="openai",
        transcription_model="whisper-1",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    stopped_called = False

    class OldSession:
        def __init__(self):
            self.provider_name = "openai"
            self.model_size = "whisper-1"
            self.language_code = "en"
            self.transcription_language = None
            self.config = ProviderConfig(api_key="old-key")
            self.state = State.RUNNING
            self.session_id = "old-session-123"
            self.task = None

        def stop(self):
            nonlocal stopped_called
            stopped_called = True
            self.state = State.STOPPED

        async def wait_until_stopped(self):
            pass

    old_session = OldSession()
    active_workers[booth_id] = old_session

    async def dummy_aenter(self):
        self.process = MagicMock(pid=9999)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.routers.api.get_api_key", return_value="new-rotated-key"),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
            patch(
                "portal.transcription.worker.PROVIDERS",
                {"openai": MagicMock(sample_rate=16000), "local": MagicMock(sample_rate=16000)},
            ),
        ):
            try:
                response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
                assert stopped_called is True
                assert response == {
                    "status": "started",
                    "already_running": False,
                    "booth_id": booth_id,
                    "provider": "openai",
                    "model": "whisper-1",
                }
                new_session = active_workers[booth_id]
                assert new_session is not old_session
                assert new_session.config.api_key == "new-rotated-key"
            finally:
                await _cleanup_test_worker(booth_id)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_normal_success_with_real_worker_lifecycle(event):
    """Normal successful start waits for worker to become ready and registers running status."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    async def dummy_aenter(self):
        self.process = MagicMock(pid=1234)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            try:
                response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

                assert response == {
                    "status": "started",
                    "already_running": False,
                    "booth_id": booth_id,
                    "provider": "local",
                    "model": "tiny",
                }
                assert booth_id in active_workers
                session = active_workers[booth_id]
                assert session.state == State.RUNNING

                status_db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))
                status_resp = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=status_db, token=object())
                assert status_resp["statuses"]["en"]["transcription_running"] is True
            finally:
                await _cleanup_test_worker(booth_id)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_startup_timeout_cleans_up_task_and_registry(event):
    """When worker startup exceeds timeout, task is cancelled, process cleaned, and 500 returned."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    async def hanging_aenter(self):
        await asyncio.sleep(0.5)

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.settings.transcription_startup_timeout_secs", 0.05),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", hanging_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            with pytest.raises(api_v1.HTTPException) as exc_info:
                await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
            assert exc_info.value.status_code == 500
            assert "Failed to start transcription worker" in exc_info.value.detail

        assert booth_id not in active_workers
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_language_code_and_transcription_language_equivalence(event):
    """transcription_language=None with language_code='en' matches language_code='floor' with transcription_language='en'."""

    dummy_session = SimpleNamespace(
        provider_name="local",
        model_size="tiny",
        language_code="floor",
        transcription_language="en",
        config=ProviderConfig(api_key=None),
    )

    # Comparing with incoming request language_code='en', transcription_language=None
    matches = _worker_config_matches(
        dummy_session,
        "local",
        "tiny",
        ProviderConfig(api_key=None),
        transcription_language=None,
        language_code="en",
    )
    assert matches is True

    # Comparing with different language
    matches_diff = _worker_config_matches(
        dummy_session,
        "local",
        "tiny",
        ProviderConfig(api_key=None),
        transcription_language="de",
        language_code="de",
    )
    assert matches_diff is False


@pytest.mark.anyio
async def test_start_concurrent_requests_during_starting_state(event):
    """Concurrent requests during STARTING both wait for readiness and do not assume success."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )

    ready_gate = asyncio.Event()

    async def slow_aenter(self):
        await ready_gate.wait()
        self.process = MagicMock(pid=5555)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", slow_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            try:
                db1 = _db(_result(first=event), _result(first=db_booth))
                db2 = _db(_result(first=event), _result(first=db_booth))

                task1 = asyncio.create_task(
                    api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db1, token=object())
                )
                # Give task1 a tick to enter start_transcription_worker and reach State.STARTING
                await asyncio.sleep(0.01)

                assert booth_id in active_workers
                assert active_workers[booth_id].state == State.STARTING

                task2 = asyncio.create_task(
                    api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object())
                )
                await asyncio.sleep(0.01)

                # Unblock FFmpeg
                ready_gate.set()

                res1, res2 = await asyncio.gather(task1, task2)
                assert res1["status"] == "started"
                assert res1["already_running"] is False
                assert res2["status"] == "started"
                assert res2["already_running"] is True
                assert res1["booth_id"] == booth_id
                assert res2["booth_id"] == booth_id
            finally:
                ready_gate.set()
                await _cleanup_test_worker(booth_id)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_concurrent_requests_both_fail_if_startup_fails(event):
    """Concurrent requests during STARTING both receive 500 if the worker fails during startup."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )

    ready_gate = asyncio.Event()

    async def failing_aenter(self):
        await ready_gate.wait()
        raise RuntimeError("Fatal process crash")

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", failing_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            db1 = _db(_result(first=event), _result(first=db_booth))
            db2 = _db(_result(first=event), _result(first=db_booth))

            task1 = asyncio.create_task(api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db1, token=object()))
            await asyncio.sleep(0.01)

            task2 = asyncio.create_task(api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object()))
            await asyncio.sleep(0.01)

            ready_gate.set()

            results = await asyncio.gather(task1, task2, return_exceptions=True)
            assert isinstance(results[0], api_v1.HTTPException)
            assert isinstance(results[1], api_v1.HTTPException)
            assert results[0].status_code == 500
            assert results[1].status_code == 500
            assert "Failed to start transcription worker" in results[0].detail
            assert "Failed to start transcription worker" in results[1].detail

        assert booth_id not in active_workers
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_startup_timeout_with_cleanup_timeout_preserves_registry_and_prevents_duplicate(event):
    """When worker startup times out and teardown also times out, session is retained to prevent duplicate workers."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    async def hanging_aenter(self):
        # We simulate a subprocess start that hangs and takes 0.3s to clean up
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            raise

    # Create a session and track its task manually
    slow_done = asyncio.Event()

    async def slow_wait_until_stopped(self):
        # Hang wait_until_stopped beyond the timeout
        await slow_done.wait()

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.settings.transcription_startup_timeout_secs", 0.05),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", hanging_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
            patch(
                "portal.transcription.worker.TranscriptionWorkerSession.wait_until_stopped",
                slow_wait_until_stopped,
            ),
        ):
            with pytest.raises(api_v1.HTTPException) as exc_info:
                await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
            assert exc_info.value.status_code == 500
            assert "Failed to start transcription worker" in exc_info.value.detail

        # Crucial invariant: session remains registered in active_workers so another caller
        # cannot immediately register a duplicate worker while old process is still unwinding.
        assert booth_id in active_workers
        retained_session = active_workers[booth_id]
        assert retained_session.state == State.STOPPING

        # A subsequent start request sees the worker is still stopping and waits / does not duplicate
        slow_done.set()  # Unblock wait_until_stopped for subsequent attempts
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.settings.transcription_startup_timeout_secs", 0.05),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            # When wait_until_stopped succeeds, next start cleanly succeeds
            if retained_session.task:
                try:
                    await asyncio.wait_for(retained_session.task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            retained_session.state = State.STOPPED
            db2 = _db(_result(first=event), _result(first=db_booth))

            async def instant_aenter(self):
                self.process = MagicMock(pid=8888)
                self.stderr_task = MagicMock(done=MagicMock(return_value=True))
                return self.process

            with patch("portal.transcription.worker.FfmpegProcess.__aenter__", instant_aenter):
                try:
                    resp = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object())
                    assert resp["status"] == "started"
                    assert active_workers[booth_id] is not retained_session
                finally:
                    await _cleanup_test_worker(booth_id)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_failure_does_not_emit_stopped_webhook(event):
    """When worker fails before readiness, booth.transcription.stopped is NOT emitted."""
    booth_id = "pycon2026-3-en"
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    emitted_events = []

    async def mock_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))
        return True

    try:
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=mock_enqueue),
            patch(
                "portal.transcription.worker.FfmpegProcess.__aenter__", side_effect=FileNotFoundError("ffmpeg missing")
            ),
        ):
            with pytest.raises(api_v1.HTTPException):
                await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

        assert not any(e[0] == "booth.transcription.stopped" for e in emitted_events)
        assert not any(e[0] == "booth.transcription.started" for e in emitted_events)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_aborts_if_db_commit_fails(event):
    """If db.commit() raises an exception before start_transcription_worker, worker is not started."""
    start_mock = AsyncMock()
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))
    db.commit = AsyncMock(side_effect=RuntimeError("DB Commit failed"))

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", start_mock),
    ):
        with pytest.raises(RuntimeError, match="DB Commit failed"):
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    start_mock.assert_not_called()


@pytest.mark.anyio
async def test_shared_transcription_config_resolution_matches_admin_pattern(event):
    """api_transcription_start and api_v1.start_transcription resolve identical config and invoke start_transcription_worker with matching arguments."""
    import portal.routers.api as api_admin

    event.transcription_api_enabled = True
    event.encrypted_openai_api_key = encrypt_val("sk-shared-test-key")
    event.encrypted_deepgram_api_key = None
    event.encrypted_nvidia_api_key = None
    event.encrypted_elevenlabs_api_key = None
    event.encrypted_atlascloud_api_key = None

    db_booth = SimpleNamespace(
        id=42,
        transcription_enabled=True,
        transcription_provider="openai",
        transcription_model="whisper-1",
        language_code="fr",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )

    mock_booth = MagicMock()
    start_result = TranscriptionStartResult(status="started", provider="openai", model="whisper-1")

    # 1. Call api_v1.start_transcription and capture worker arguments
    db_v1 = _db(_result(first=event), _result(first=db_booth))
    v1_mock = AsyncMock(return_value=start_result)
    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "start_transcription_worker", v1_mock),
    ):
        res_v1 = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "fr", db=db_v1, token=object())
    assert res_v1["status"] == "started"
    v1_mock.assert_awaited_once()
    v1_args, v1_kwargs = v1_mock.await_args

    # 2. Call api_admin.api_transcription_start and capture worker arguments
    admin_mock = AsyncMock(return_value=start_result)
    mock_request = MagicMock()

    class DummySessionContext:
        async def __aenter__(self):
            session = MagicMock()
            session.scalar = AsyncMock(return_value=db_booth)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with (
        patch.object(api_admin, "get_session", return_value=DummySessionContext()),
        patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
        patch.object(api_admin, "_verify_resource_access", AsyncMock()),
        patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
        patch.object(api_admin.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_admin, "start_transcription_worker", admin_mock),
    ):
        res_admin = await api_admin.api_transcription_start(EVENT_SLUG, ROOM_ID, "fr", request=mock_request)
    assert res_admin["status"] == "started"
    admin_mock.assert_awaited_once()
    admin_args, admin_kwargs = admin_mock.await_args

    # Compare arguments received by start_transcription_worker from both routes
    # (event_slug, language_code, booth_id, broadcast_callback, provider, model_size, config)
    assert v1_args[0] == admin_args[0] == EVENT_SLUG
    assert v1_args[1] == admin_args[1] == "fr"
    assert v1_args[2] == admin_args[2] == make_booth_id(EVENT_SLUG, ROOM_ID, "fr")
    assert v1_args[4] == admin_args[4] == "openai"
    assert v1_args[5] == admin_args[5] == "whisper-1"
    assert v1_args[6].api_key == admin_args[6].api_key == "sk-shared-test-key"
    assert v1_kwargs.get("room_id") == admin_kwargs.get("room_id") == ROOM_ID
    assert v1_kwargs.get("restart_on_key_change") == admin_kwargs.get("restart_on_key_change") is True
    assert v1_kwargs.get("wait_for_ready") == admin_kwargs.get("wait_for_ready") is True


@pytest.mark.anyio
async def test_different_configuration_parameters_not_ignored():
    """Genuinely different provider, model, API key, or language returns False and is not ignored."""
    base_session = SimpleNamespace(
        provider_name="openai",
        model_size="whisper-1",
        config=ProviderConfig(api_key="key-alpha"),
        language_code="en",
        transcription_language=None,
    )

    # 1. Different provider
    assert (
        _worker_config_matches(
            base_session,
            "deepgram",
            "whisper-1",
            ProviderConfig(api_key="key-alpha"),
            language_code="en",
        )
        is False
    )

    # 2. Different model
    assert (
        _worker_config_matches(
            base_session,
            "openai",
            "gpt-4o-realtime",
            ProviderConfig(api_key="key-alpha"),
            language_code="en",
        )
        is False
    )

    # 3. Different API key
    assert (
        _worker_config_matches(
            base_session,
            "openai",
            "whisper-1",
            ProviderConfig(api_key="key-beta"),
            language_code="en",
        )
        is False
    )

    # 4. Different language
    assert (
        _worker_config_matches(
            base_session,
            "openai",
            "whisper-1",
            ProviderConfig(api_key="key-alpha"),
            language_code="es",
        )
        is False
    )


@pytest.mark.anyio
async def test_floor_caller_booth_id_isolation():
    """Floor callers and interpreter booth callers never collide on booth_id."""

    event_slug = "pycon2026"
    room_id = 42

    floor_booth_id = make_booth_id(event_slug, room_id, "floor")
    assert floor_booth_id == "pycon2026-42-floor"

    for lang in ["en", "es", "fr", "de", "zh", "ja"]:
        interpreter_booth_id = make_booth_id(event_slug, room_id, lang)
        assert interpreter_booth_id != floor_booth_id
        assert not interpreter_booth_id.endswith("-floor")


@pytest.mark.anyio
async def test_start_worker_stopping_wait_bounded_and_retains_registry():
    """When a session is stuck in STOPPING, a second start call returns failed within bound and retains old session."""

    booth_id = "test-stopping-timeout-booth"

    async def slow_wait():
        await asyncio.sleep(0.5)

    stuck_stopping_session = SimpleNamespace(
        provider_name="local",
        model_size="tiny",
        language_code="en",
        transcription_language=None,
        session_id="stuck-session-123",
        state=State.STOPPING,
        task=None,
        config=ProviderConfig(api_key=None),
        stop=MagicMock(),
        wait_until_stopped=slow_wait,
    )
    active_workers[booth_id] = stuck_stopping_session

    try:
        with patch.object(settings, "transcription_startup_timeout_secs", 0.05):
            result = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
            )

        assert result.status == "failed"
        assert "Timed out waiting for existing worker to stop" in (result.error or "")
        # Registry still holds the old session, and did not evict it or spawn a replacement
        assert active_workers.get(booth_id) is stuck_stopping_session
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_worker_config_matches_api_key_and_language_edge_cases():
    """Verify _worker_config_matches handles None vs empty API keys, mixed case, and None vs empty languages safely."""

    session = SimpleNamespace(
        provider_name="openai",
        model_size="whisper-1",
        language_code="en",
        transcription_language=None,
        config=ProviderConfig(api_key=None),
    )

    # 1. API key None vs None -> True
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="en") is True
    )
    # 2. API key None vs "" -> True
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key=""), language_code="en") is True
    )

    session_empty_key = SimpleNamespace(
        provider_name="openai",
        model_size="whisper-1",
        language_code="en",
        transcription_language=None,
        config=ProviderConfig(api_key=""),
    )
    # 3. API key "" vs None -> True
    assert (
        _worker_config_matches(
            session_empty_key, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="en"
        )
        is True
    )
    # 4. API key "" vs "" -> True
    assert (
        _worker_config_matches(session_empty_key, "openai", "whisper-1", ProviderConfig(api_key=""), language_code="en")
        is True
    )
    # 5. API key None vs "secret" -> False
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key="secret"), language_code="en")
        is False
    )

    # 6. Language mixed-case and whitespace normalization
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="EN") is True
    )
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="  en  ")
        is True
    )
    assert (
        _worker_config_matches(
            session, "openai", "whisper-1", ProviderConfig(api_key=None), transcription_language="EN"
        )
        is True
    )
    # 7. Language None vs ""
    session_no_lang = SimpleNamespace(
        provider_name="openai",
        model_size="whisper-1",
        language_code="",
        transcription_language=None,
        config=ProviderConfig(api_key=None),
    )
    assert (
        _worker_config_matches(session_no_lang, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="")
        is True
    )
    assert (
        _worker_config_matches(session_no_lang, "openai", "whisper-1", ProviderConfig(api_key=None), language_code=None)
        is True
    )
    # 8. Genuinely different language
    assert (
        _worker_config_matches(session, "openai", "whisper-1", ProviderConfig(api_key=None), language_code="fr")
        is False
    )


@pytest.mark.anyio
async def test_worker_running_published_together_with_ready_event():
    """State.RUNNING and ready_event are set back-to-back before webhook enqueue; concurrent starts see ready."""

    booth_id = "test-webhook-readiness-booth"
    webhook_started = asyncio.Event()
    release_webhook = asyncio.Event()
    emitted_events = []

    async def blocking_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))
        if event_name == "booth.transcription.started":
            webhook_started.set()
            await release_webhook.wait()

    async def dummy_aenter(self):
        self.process = MagicMock(pid=1234)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    session = None
    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        patch("portal.webhooks.worker.enqueue_webhook", side_effect=blocking_enqueue),
    ):
        try:
            res1 = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
                wait_for_ready=False,
            )
            assert res1.status == "started"
            session = active_workers[booth_id]

            await asyncio.wait_for(webhook_started.wait(), timeout=1.0)
            assert session.state == State.RUNNING
            assert session.ready_event.is_set()

            res2 = await asyncio.wait_for(
                start_transcription_worker(
                    "pycon2026",
                    "en",
                    booth_id,
                    MagicMock(),
                    "local",
                    "tiny",
                    ProviderConfig(api_key=None),
                    wait_for_ready=True,
                ),
                timeout=1.0,
            )
            assert res2.status == "already_running"

            release_webhook.set()
            await asyncio.sleep(0.01)

            started_emitted = [e for e in emitted_events if e[0] == "booth.transcription.started"]
            assert len(started_emitted) == 1
        finally:
            release_webhook.set()
            await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_webhook_pairing_across_retries():
    """Verify started webhook is emitted once, not re-emitted on retry, and exactly one stopped is emitted."""

    booth_id = "test-webhook-pairing-booth"
    emitted_events = []

    async def mock_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))
        return True

    stream_attempts = 0

    class CrashOnceProvider:
        async def run_stream(self, process, language, model, config, callback, booth, room, **kwargs):
            nonlocal stream_attempts
            stream_attempts += 1
            if stream_attempts == 1:
                raise RuntimeError("First stream attempt crashed")
            # Second attempt stays alive until cancelled
            while True:
                await asyncio.sleep(0.01)

    session = TranscriptionWorkerSession(
        event_slug="pycon2026",
        language_code="en",
        booth_id=booth_id,
        broadcast_callback=MagicMock(),
        provider_name="local",
        model_size="tiny",
        config=ProviderConfig(api_key=None),
    )
    session.provider = CrashOnceProvider()

    async def dummy_aenter(self):
        self.process = MagicMock(pid=1111)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    try:
        with (
            patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=mock_enqueue),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
            patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        ):
            session.start()
            # Wait for ready_event (first attempt spawned ffmpeg and emitted started)
            await session.ready_event.wait()
            assert session.started_webhook_emitted is True

            # Wait briefly for retry loop to execute attempt 2
            for _ in range(50):
                if stream_attempts >= 2:
                    break
                await asyncio.sleep(0.05)
            assert stream_attempts >= 2

            # Confirm started was emitted exactly once despite stream retry
            assert [e[0] for e in emitted_events].count("booth.transcription.started") == 1

            # Stop the session
            session.stop()
            await session.wait_until_stopped()

            # Confirm stopped was emitted exactly once
            assert [e[0] for e in emitted_events].count("booth.transcription.stopped") == 1
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_worker_spawn_failure_wakes_waiter_immediately():
    """When FFmpegProcess fails to spawn, waiter is woken immediately (<1s), registry is clean, and no stopped webhook."""

    booth_id = "test-spawn-failure-booth"
    emitted_events = []

    async def mock_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))
        return True

    async def failing_aenter(self):
        raise FileNotFoundError("ffmpeg binary not found on PATH")

    start_time = time.monotonic()
    try:
        with (
            patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=mock_enqueue),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", failing_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            result = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )
        elapsed = time.monotonic() - start_time

        # (1) returns status="failed" with start_error set
        assert result.status == "failed"
        assert "ffmpeg binary not found on PATH" in (result.error or "")

        # (2) elapsed is well under the 5.0s timeout (< 1.0s)
        assert elapsed < 1.0

        # (3) registry is clean
        assert booth_id not in active_workers

        # (4) no stopped webhook is emitted
        assert not any(e[0] == "booth.transcription.stopped" for e in emitted_events)
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_stopping_timeout_returns_503(event):
    """When start_transcription_worker fails due to stop timeout, route returns 503 with Retry-After header."""
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )

    db = _db(_result(first=event), _result(first=db_booth))
    mock_failed_by_code = TranscriptionStartResult(
        status="failed",
        provider="local",
        model="tiny",
        error="Timed out waiting for existing worker to stop",
        error_code=ERROR_CODE_STOPPING_TIMEOUT,
    )

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch("portal.routers.api_v1.start_transcription_worker", return_value=mock_failed_by_code),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Transcription worker is still stopping; retry shortly"
        assert exc_info.value.headers is not None
        expected_retry_after = str(math.ceil(api_v1.settings.transcription_startup_timeout_secs))
        assert exc_info.value.headers.get("Retry-After") == expected_retry_after


@pytest.mark.anyio
async def test_worker_run_loop_honors_stop_event_if_cancellation_swallowed():
    """If FfmpegProcess.__aenter__ swallows CancelledError, _run_loop honors stop_event and exits without endless retry."""

    entered = asyncio.Event()

    async def swallowing_aenter(self):
        entered.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # Swallowing CancelledError simulates a context manager that catches CancelledError
            return MagicMock()

    session = TranscriptionWorkerSession(
        "pycon2026",
        "en",
        "pycon2026-1-en",
        MagicMock(),
        "local",
        "tiny",
        ProviderConfig(api_key=None),
    )

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", swallowing_aenter),
        patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
    ):
        session.start()
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        session.stop()
        await asyncio.wait_for(session.wait_until_stopped(), timeout=2.0)

    assert session.state == State.STOPPED
    assert session.task is not None and session.task.done()


@pytest.mark.anyio
async def test_started_webhook_enqueue_failure_does_not_abort_worker():
    """enqueue_webhook raises RuntimeError -> worker still reaches RUNNING, ready_event stays set,
    start_transcription_worker returns 'started', worker not restarted, and no 'stopped' webhook is emitted."""

    booth_id = "test-webhook-failure-isolation-booth"
    emitted_events = []
    ffmpeg_enter_count = 0

    async def failing_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))
        if event_name == "booth.transcription.started":
            raise RuntimeError("webhook backend unreachable")

    async def counting_aenter(self):
        nonlocal ffmpeg_enter_count
        ffmpeg_enter_count += 1
        self.process = MagicMock(pid=9999)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def counting_aexit(self, exc_type, exc_val, exc_tb):
        return None

    dummy_provider = MagicMock()
    dummy_provider.sample_rate = 16000

    async def dummy_run_stream(*args, **kwargs):
        # Keep worker alive until stopped
        while True:
            await asyncio.sleep(0.05)

    dummy_provider.run_stream = dummy_run_stream

    session = None
    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", counting_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", counting_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": dummy_provider}),
        patch("portal.webhooks.worker.enqueue_webhook", side_effect=failing_enqueue),
    ):
        try:
            res = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )

            assert res.status == "started"
            session = active_workers.get(booth_id)
            assert session is not None
            assert session.state == State.RUNNING
            assert session.ready_event.is_set() is True
            assert session.started_webhook_emitted is False
            assert ffmpeg_enter_count == 1

            # Give a brief moment to ensure no background restart happens
            await asyncio.sleep(0.1)
            assert ffmpeg_enter_count == 1
            assert session.state == State.RUNNING

            # Stop worker and confirm no stopped webhook is emitted
            await stop_transcription_worker(booth_id)
            assert session.state == State.STOPPED
            stopped_webhooks = [e for e in emitted_events if e[0] == "booth.transcription.stopped"]
            assert len(stopped_webhooks) == 0
        finally:
            await _cleanup_test_worker(booth_id)


# ── PR #1110 Comprehensive Stabilization Regression Tests ────────────────────


@pytest.mark.anyio
async def test_start_missing_db_booth_returns_404(event):
    """When DBBooth is None, start_transcription returns 404, distinct from disabled returning 200."""
    db = _db(_result(first=event), _result(first=None))
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Booth not found"


def test_transcription_startup_timeout_setting_validation():
    """Settings enforces positive numeric values for startup and shutdown timeouts."""
    # transcription_startup_timeout_secs validation
    with pytest.raises(ValidationError):
        Settings(transcription_startup_timeout_secs=0.0)

    with pytest.raises(ValidationError):
        Settings(transcription_startup_timeout_secs=-2.5)

    with pytest.raises(ValidationError):
        Settings(transcription_startup_timeout_secs="invalid")

    valid_startup = Settings(transcription_startup_timeout_secs=3.5)
    assert valid_startup.transcription_startup_timeout_secs == 3.5

    # transcription_shutdown_timeout_secs validation
    with pytest.raises(ValidationError):
        Settings(transcription_shutdown_timeout_secs=0.0)

    with pytest.raises(ValidationError):
        Settings(transcription_shutdown_timeout_secs=-5.0)

    with pytest.raises(ValidationError):
        Settings(transcription_shutdown_timeout_secs="invalid")

    valid_shutdown = Settings(transcription_shutdown_timeout_secs=12.0)
    assert valid_shutdown.transcription_shutdown_timeout_secs == 12.0


@pytest.mark.anyio
async def test_provider_initialization_fails_before_readiness():
    """When provider initialization fails before readiness, start_transcription_worker returns failed
    without entering infinite retries and cleans up registry."""
    booth_id = "test-provider-init-fail-booth"

    class FailingInitProvider:
        sample_rate = 16000

        async def initialize(self):
            raise RuntimeError("Provider credentials rejected by remote API")

        async def run_stream(self, *args, **kwargs):
            while True:
                await asyncio.sleep(0.01)

    async def dummy_aenter(self):
        self.process = MagicMock(pid=7777)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": FailingInitProvider()}),
    ):
        try:
            res = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )
            assert res.status == "failed"
            assert res.error_code == ERROR_CODE_SPAWN_FAILED
            assert "Provider credentials rejected by remote API" in (res.error or "")
            assert booth_id not in active_workers
        finally:
            await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_stopped_webhook_failure_does_not_prevent_cleanup_and_model_decrement():
    """Failure while enqueueing booth.transcription.stopped webhook must not prevent model-reference decrement
    or registry cleanup."""

    booth_id = "test-stopped-webhook-failure-booth"
    model = "tiny"
    initial_refs = _active_booths_per_model.get(model, 0)

    async def failing_enqueue(event_name, payload):
        if event_name == "booth.transcription.stopped":
            raise RuntimeError("Webhook endpoint unreachable")
        return True

    async def dummy_aenter(self):
        self.process = MagicMock(pid=6666)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    dummy_provider = MagicMock()
    dummy_provider.sample_rate = 16000

    async def dummy_stream(*args, **kwargs):
        while True:
            await asyncio.sleep(0.01)

    dummy_provider.run_stream = dummy_stream

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": dummy_provider}),
        patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=failing_enqueue),
    ):
        try:
            res = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                model,
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )
            assert res.status == "started"
            assert _active_booths_per_model.get(model, 0) == initial_refs + 1
            assert booth_id in active_workers

            # Stop worker - stopped webhook will raise RuntimeError, but cleanup must still finish
            stopped = await stop_transcription_worker(booth_id)
            assert stopped is True
            assert booth_id not in active_workers
            assert _active_booths_per_model.get(model, 0) == initial_refs
        finally:
            await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_local_model_ref_decremented_exactly_once():
    """Repeated stop requests or multiple teardown steps must decrement local model ref exactly once."""

    booth_id = "test-single-decrement-booth"
    model = "tiny"
    initial_refs = _active_booths_per_model.get(model, 0)

    async def dummy_aenter(self):
        self.process = MagicMock(pid=6667)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    dummy_provider = MagicMock()
    dummy_provider.sample_rate = 16000

    async def dummy_stream(*args, **kwargs):
        while True:
            await asyncio.sleep(0.01)

    dummy_provider.run_stream = dummy_stream

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": dummy_provider}),
    ):
        try:
            res = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                model,
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )
            assert res.status == "started"
            assert _active_booths_per_model.get(model, 0) == initial_refs + 1

            session = active_workers.get(booth_id)
            assert session is not None

            # First stop
            await stop_transcription_worker(booth_id)
            assert _active_booths_per_model.get(model, 0) == initial_refs

            # Second stop on same session must be a no-op and not decrement below initial_refs
            session.stop()
            await session.wait_until_stopped()
            assert _active_booths_per_model.get(model, 0) == initial_refs
        finally:
            await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_old_worker_cleanup_cannot_remove_replacement_session():
    """An old session completing teardown cannot pop a newer replacement session from active_workers."""

    booth_id = "test-replacement-ownership-booth"
    model = "tiny"
    paused_at_cleanup = asyncio.Event()
    allow_cleanup_to_proceed = asyncio.Event()

    async def pausing_enqueue(event_type: str, payload: dict) -> bool:
        if event_type == "booth.transcription.stopped":
            paused_at_cleanup.set()
            await allow_cleanup_to_proceed.wait()
        return True

    dummy_process = MagicMock()
    dummy_process.stdout = AsyncMock()
    dummy_process.stdout.read = AsyncMock(return_value=b"")

    async def dummy_aenter(self):
        return dummy_process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    dummy_provider = MagicMock()
    dummy_provider.sample_rate = 16000

    async def dummy_stream(*args, **kwargs):
        while True:
            await asyncio.sleep(0.01)

    dummy_provider.run_stream = dummy_stream

    session_new = TranscriptionWorkerSession(
        "pycon2026",
        "en",
        booth_id,
        MagicMock(),
        "local",
        model,
        ProviderConfig(api_key=None),
    )
    session_new.state = State.RUNNING

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": dummy_provider}),
        patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=pausing_enqueue),
    ):
        res = await start_transcription_worker(
            "pycon2026",
            "en",
            booth_id,
            MagicMock(),
            "local",
            model,
            ProviderConfig(api_key=None),
            wait_for_ready=True,
        )
        assert res.status == "started"
        session_old = active_workers[booth_id]

        # Stop session_old, triggering real teardown in _run_loop
        session_old.stop()

        # Wait until session_old enters cleanup and pauses at the stopped webhook
        await asyncio.wait_for(paused_at_cleanup.wait(), timeout=2.0)

        # While paused before registry removal, register session_new under active_workers_lock
        async with active_workers_lock:
            active_workers[booth_id] = session_new

        # Release the pause so session_old executes its real registry removal block
        allow_cleanup_to_proceed.set()
        await session_old.wait_until_stopped()

        # Invariant: session_old did NOT remove session_new from active_workers
        assert active_workers.get(booth_id) is session_new
        assert session_new.state == State.RUNNING

        # Clean up session_new
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_shutdown_timeout_preserves_registry_ownership():
    """When stop_transcription_worker times out, worker remains in active_workers and returns False."""

    booth_id = "test-shutdown-timeout-retention-booth"

    async def stuck_task_coro():
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    stuck_task = asyncio.create_task(stuck_task_coro())

    async def stuck_wait():
        await asyncio.shield(stuck_task)

    stuck_session = SimpleNamespace(
        session_id="stuck-123",
        state=State.STOPPING,
        stop=MagicMock(),
        wait_until_stopped=stuck_wait,
        task=stuck_task,
    )
    active_workers[booth_id] = stuck_session

    try:
        stopped = await stop_transcription_worker(booth_id, timeout=0.02)
        assert stopped is False
        assert active_workers.get(booth_id) is stuck_session
        assert stuck_session.task.done() is False
        assert stuck_session.task.cancelled() is False
    finally:
        active_workers.pop(booth_id, None)
        stuck_task.cancel()
        try:
            await stuck_task
        except asyncio.CancelledError:
            pass


@pytest.mark.anyio
async def test_concurrent_stop_requests_are_safe():
    """Concurrent stop requests for the same booth both complete cleanly without errors."""

    booth_id = "test-concurrent-stop-booth"

    async def dummy_aenter(self):
        self.process = MagicMock(pid=6668)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    async def dummy_aexit(self, exc_type, exc_val, exc_tb):
        pass

    dummy_provider = MagicMock()
    dummy_provider.sample_rate = 16000

    async def dummy_stream(*args, **kwargs):
        while True:
            await asyncio.sleep(0.01)

    dummy_provider.run_stream = dummy_stream

    with (
        patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
        patch("portal.transcription.worker.FfmpegProcess.__aexit__", dummy_aexit),
        patch("portal.transcription.worker.PROVIDERS", {"local": dummy_provider}),
    ):
        try:
            res = await start_transcription_worker(
                "pycon2026",
                "en",
                booth_id,
                MagicMock(),
                "local",
                "tiny",
                ProviderConfig(api_key=None),
                wait_for_ready=True,
            )
            assert res.status == "started"

            stop1 = asyncio.create_task(stop_transcription_worker(booth_id))
            stop2 = asyncio.create_task(stop_transcription_worker(booth_id))

            res1, res2 = await asyncio.gather(stop1, stop2)
            assert res1 is True
            assert res2 is True
            assert booth_id not in active_workers
        finally:
            await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_start_versus_booth_deletion():
    """When revalidate_active reports false (booth deleted in DB/memory), start_transcription_worker aborts."""
    booth_id = "test-revalidate-deleted-booth"

    res = await start_transcription_worker(
        "pycon2026",
        "en",
        booth_id,
        MagicMock(),
        "local",
        "tiny",
        ProviderConfig(api_key=None),
        revalidate_active=lambda: False,
    )
    assert res.status == "failed"
    assert res.error_code == "booth_deleted"
    assert booth_id not in active_workers


@pytest.mark.anyio
async def test_admin_start_booth_exists_only_in_db_succeeds(event):
    """Admin api_transcription_start for a booth that exists only in DB (not in memory) succeeds."""
    import portal.routers.api as api_admin

    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    db_booth = SimpleNamespace(
        id=77,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )

    class DummySessionContext:
        async def __aenter__(self):
            session = MagicMock()
            session.scalar = AsyncMock(return_value=db_booth)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def dummy_aenter(self):
        self.process = MagicMock(pid=5678)
        self.stderr_task = MagicMock(done=MagicMock(return_value=True))
        return self.process

    mock_request = MagicMock()

    try:
        # Booth does NOT exist in memory (get_booth_sync returns None)
        with (
            patch.object(api_admin.booths, "get_booth_sync", return_value=None),
            patch.object(api_admin, "get_session", return_value=DummySessionContext()),
            patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
            patch.object(api_admin, "_verify_resource_access", AsyncMock()),
            patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", dummy_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock(sample_rate=16000)}),
        ):
            response = await api_admin.api_transcription_start(EVENT_SLUG, ROOM_ID, "en", request=mock_request)
            assert response["status"] == "started"
            assert response["provider"] == "local"
            assert booth_id in active_workers
    finally:
        await _cleanup_test_worker(booth_id)


@pytest.mark.anyio
async def test_admin_start_versus_booth_deletion(event):
    """When booth is deleted from DB before registration, admin api_transcription_start returns 404 and registers no worker."""
    import portal.routers.api as api_admin

    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        id=77,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )

    first_query = True

    class RaceSessionContext:
        async def __aenter__(self):
            nonlocal first_query
            session = MagicMock()
            if first_query:
                first_query = False
                session.scalar = AsyncMock(return_value=db_booth)
            else:
                # Revalidation query in _revalidate_active: delete has committed
                session.scalar = AsyncMock(return_value=None)
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_request = MagicMock()

    # Booth is still in memory (mock_booth), but deleted in DB during the race
    with (
        patch.object(api_admin, "get_session", return_value=RaceSessionContext()),
        patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
        patch.object(api_admin, "_verify_resource_access", AsyncMock()),
        patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
        patch.object(api_admin.booths, "get_booth_sync", return_value=mock_booth),
    ):
        with pytest.raises(api_admin.HTTPException) as exc_info:
            await api_admin.api_transcription_start(EVENT_SLUG, ROOM_ID, "en", request=mock_request)
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Booth is no longer active"
        assert booth_id not in active_workers


@pytest.mark.anyio
async def test_booth_deletion_rollback_does_not_stop_worker(event):
    """If DB deletion fails or rolls back, stop_transcription_worker is not called."""
    booth = SimpleNamespace(id=55, room_id=ROOM_ID, language_code="en", event_id=event.id)
    db = _db(_result(first=event), _result(first=booth))
    db.commit = AsyncMock(side_effect=RuntimeError("Database write conflict during deletion"))

    stop_called = False

    async def mock_stop(bid):
        nonlocal stop_called
        stop_called = True

    with (
        patch("portal.routers.api_v1.revoke_booths_for_deleted_entity", AsyncMock()),
        patch("portal.database.delete_booth", AsyncMock()),
        patch("portal.routers.api_v1.stop_transcription_worker", mock_stop),
    ):
        with pytest.raises(RuntimeError, match="Database write conflict during deletion"):
            await api_v1.delete_booth_endpoint(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert stop_called is False


def test_floor_language_matching_all_edge_cases():
    """Verify _languages_match and _effective_language cover all edge cases deterministically."""

    # 1. Normal booth
    assert _effective_language("en", None) == "en"
    assert _effective_language("en", "en") == "en"

    # 2. Explicit floor transcription language
    assert _effective_language("floor", "fr") == "fr"
    assert _effective_language("floor", "FR") == "fr"

    # 3. Floor request without explicit transcription language
    assert _effective_language("floor", None) == ""
    assert _effective_language("floor", "") == ""
    assert _effective_language("floor", "floor") == ""

    # 4. Normal booth with explicit transcription language override
    assert _effective_language("es", "en") == "en"

    # 5. Case normalization
    assert _effective_language("  EN  ", "  DE  ") == "de"
    assert _effective_language("  EN  ", None) == "en"

    # Matching tests with sessions
    def _make_session(lang_code, trans_lang):
        return TranscriptionWorkerSession(
            "event",
            lang_code,
            "bid",
            MagicMock(),
            "local",
            "tiny",
            ProviderConfig(api_key=None),
            transcription_language=trans_lang,
        )

    # Session floor with en vs incoming floor without explicit language -> should NOT match!
    floor_en_session = _make_session("floor", "en")
    assert _languages_match(floor_en_session, None, "floor") is False
    assert _languages_match(floor_en_session, "en", "floor") is True

    # Existing floor session vs non-floor request -> should NOT match
    assert _languages_match(floor_en_session, None, "es") is False

    # Normal session en vs incoming EN -> matches
    normal_en_session = _make_session("en", None)
    assert _languages_match(normal_en_session, None, "EN") is True
    assert _languages_match(normal_en_session, None, "  en  ") is True
    assert _languages_match(normal_en_session, None, "fr") is False


@pytest.mark.anyio
async def test_stop_route_timed_out_stop_returns_503_api_v1(event):
    """When stop_transcription_worker returns False, API v1 stop returns 503 with Retry-After."""
    room = SimpleNamespace(id=ROOM_ID, event_id=event.id, name="Test Room")
    db = _db(_result(first=event), _result(first=room))

    with patch("portal.routers.api_v1.stop_transcription_worker", AsyncMock(return_value=False)):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.stop_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert exc_info.value.status_code == 503
        assert exc_info.value.headers == {"Retry-After": "2"}
        assert exc_info.value.detail == "Transcription worker is still stopping; retry shortly"

    with patch("portal.routers.api_v1.stop_transcription_worker", AsyncMock(return_value=True)):
        res = await api_v1.stop_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert res["status"] == "stopped"


@pytest.mark.anyio
async def test_stop_route_timed_out_stop_returns_503_admin(event):
    """When stop_transcription_worker returns False, admin stop returns 503 with Retry-After."""
    import portal.routers.api as api_admin

    mock_request = MagicMock()
    with (
        patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
        patch.object(api_admin, "_verify_resource_access", AsyncMock()),
        patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
        patch.object(api_admin, "_require_live_interpreter_to_stop", AsyncMock()),
        patch("portal.routers.api.stop_transcription_worker", AsyncMock(return_value=False)),
    ):
        with pytest.raises(api_admin.HTTPException) as exc_info:
            await api_admin.api_transcription_stop(mock_request, EVENT_SLUG, ROOM_ID, "en")
        assert exc_info.value.status_code == 503
        assert exc_info.value.headers == {"Retry-After": "2"}
        assert exc_info.value.detail == "Transcription worker is still stopping; retry shortly"

    with (
        patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
        patch.object(api_admin, "_verify_resource_access", AsyncMock()),
        patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
        patch.object(api_admin, "_require_live_interpreter_to_stop", AsyncMock()),
        patch("portal.routers.api.stop_transcription_worker", AsyncMock(return_value=True)),
    ):
        res = await api_admin.api_transcription_stop(mock_request, EVENT_SLUG, ROOM_ID, "en")
        assert res["status"] == "stopped"


@pytest.mark.anyio
async def test_start_revalidation_timeout_fails_closed_and_releases_lock(event, monkeypatch):
    """When revalidation hangs, startup times out, returns 503, registers nothing, and releases the lock."""
    from portal.config import settings

    monkeypatch.setattr(settings, "transcription_startup_timeout_secs", 0.05)

    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        id=99,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    class HangingSessionContext:
        async def __aenter__(self):
            session = MagicMock()

            async def hanging_scalar(*args, **kwargs):
                await asyncio.sleep(10.0)
                return 1

            session.scalar = hanging_scalar
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch.object(api_v1, "get_session", return_value=HangingSessionContext()),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Timed out revalidating booth status; retry shortly"
        assert booth_id not in active_workers

    # Verify active_workers_lock was not leaked: lock can be acquired immediately
    assert active_workers_lock.locked() is False
    async with active_workers_lock:
        assert booth_id not in active_workers


@pytest.mark.anyio
async def test_admin_start_revalidation_timeout_returns_503(event, monkeypatch):
    """When revalidation hangs, admin start times out and returns 503."""
    import portal.routers.api as api_admin
    from portal.config import settings

    monkeypatch.setattr(settings, "transcription_startup_timeout_secs", 0.05)

    booth_id = make_booth_id(EVENT_SLUG, ROOM_ID, "en")
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        id=99,
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        language_code="en",
        room_id=ROOM_ID,
        event_id=event.id,
        event=event,
    )

    first_query = True

    class HangingRaceSessionContext:
        async def __aenter__(self):
            nonlocal first_query
            session = MagicMock()
            if first_query:
                first_query = False
                session.scalar = AsyncMock(return_value=db_booth)
            else:

                async def hanging_scalar(*args, **kwargs):
                    await asyncio.sleep(10.0)
                    return 1

                session.scalar = hanging_scalar
            return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_request = MagicMock()

    with (
        patch.object(api_admin, "get_session", return_value=HangingRaceSessionContext()),
        patch.object(api_admin, "get_event_by_slug", AsyncMock(return_value=event)),
        patch.object(api_admin, "_verify_resource_access", AsyncMock()),
        patch.object(api_admin, "_require_same_origin_for_cookies", MagicMock()),
        patch.object(api_admin.booths, "get_booth_sync", return_value=mock_booth),
    ):
        with pytest.raises(api_admin.HTTPException) as exc_info:
            await api_admin.api_transcription_start(EVENT_SLUG, ROOM_ID, "en", request=mock_request)
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Timed out revalidating booth status; retry shortly"
        assert booth_id not in active_workers

    assert active_workers_lock.locked() is False
