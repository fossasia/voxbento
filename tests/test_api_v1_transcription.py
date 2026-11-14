"""Tests for the /api/v1 room transcription stop and status routes.

Both routes used to call make_booth_id without room_id and raised TypeError on
every request. The status route additionally called a BoothRegistry method that
does not exist, and read transcription state from an attribute nothing sets.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portal.routers.api_v1 as api_v1
from portal.database import create_booth, create_event, create_room
from portal.models import Base, TranscriptSegment, TranscriptTranslation
from portal.transcription.worker import TranscriptionWorkerSession, active_workers

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
    return SimpleNamespace(id=1, slug=EVENT_SLUG, transcription_api_enabled=False)


@pytest.fixture(autouse=True)
def _skip_rbac():
    # Authorisation is covered elsewhere; these tests are about what happens
    # after the caller has been let in.
    with patch.object(api_v1, "_verify_token_rbac", AsyncMock()):
        yield


@pytest.fixture
def running_worker():
    """Register a transcription worker for one booth, as start would."""
    registered = []

    def _register(booth_id):
        active_workers[booth_id] = object()
        registered.append(booth_id)

    yield _register
    for booth_id in registered:
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
    booth_en = "pycon2026-3-en"
    booth_fr = "pycon2026-3-fr"
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    stopping_worker = SimpleNamespace(state=api_v1.State.STOPPING, is_running=False)
    stopped_worker = SimpleNamespace(state=api_v1.State.STOPPED, is_running=False)
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
    booth_en = "pycon2026-3-en"
    booth_fr = "pycon2026-3-fr"
    booth_es = "pycon2026-3-es"
    booths_in_room = [
        SimpleNamespace(language_code="en"),
        SimpleNamespace(language_code="fr"),
        SimpleNamespace(language_code="es"),
    ]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    done_task = MagicMock()
    done_task.done.return_value = True

    starting_worker = SimpleNamespace(state=api_v1.State.STARTING, start_error=None, task=None)
    errored_worker = SimpleNamespace(state=api_v1.State.STARTING, start_error="ffmpeg failed", task=None)
    dead_task_worker = SimpleNamespace(state=api_v1.State.RUNNING, start_error=None, task=done_task)

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
    booth_en = "pycon2026-3-en"
    booths_in_room = [SimpleNamespace(language_code="en")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    live_task = MagicMock()
    live_task.done.return_value = False
    running_worker = SimpleNamespace(
        state=api_v1.State.RUNNING,
        start_error=None,
        task=live_task,
        is_running=True,
    )
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
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

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
        patch("portal.routers.api.get_api_key", return_value="known-key"),
    ):
        response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response == {
        "status": "started",
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
async def test_start_inactive_booth_raises_400(event):
    """If the booth is not active in memory, route raises 400 Bad Request."""
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )
    db = _db(_result(first=event), _result(first=db_booth))

    with patch.object(api_v1.booths, "get_booth_sync", return_value=None):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert exc_info.value.status_code == 400
    assert "Booth not active in memory" in exc_info.value.detail


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
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_v1.router)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(f"/api/v1/events/{EVENT_SLUG}/rooms/{ROOM_ID}/booths/en/transcription/start")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_start_transcription_scope_checker_rejects_missing_scope():
    """OAuth dependency requires sessions:manage scope and rejects tokens without it."""
    from portal.auth import require_oauth_scope
    from portal.models import OAuthToken

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
    from portal.transcription.providers.base import ProviderConfig

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
        state=api_v1.State.RUNNING,
        stop=MagicMock(),
        wait_until_stopped=AsyncMock(),
    )
    active_workers[booth_id] = existing_session

    try:
        with patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth):
            response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

        assert response == {
            "status": "already_running",
            "booth_id": booth_id,
            "provider": "local",
            "model": "tiny",
        }
    finally:
        active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_start_already_running_rotated_api_key_restarts_worker(event):
    """When a worker is running but API key is rotated, worker is restarted with new settings."""
    from portal.transcription.providers.base import ProviderConfig

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
            self.state = api_v1.State.RUNNING
            self.session_id = "old-session-123"

        def stop(self):
            nonlocal stopped_called
            stopped_called = True
            self.state = api_v1.State.STOPPED

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
            patch("portal.transcription.worker.PROVIDERS", {"openai": MagicMock()}),
        ):
            response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

        assert stopped_called is True
        assert response == {
            "status": "started",
            "booth_id": booth_id,
            "provider": "openai",
            "model": "whisper-1",
        }
        new_session = active_workers[booth_id]
        assert new_session is not old_session
        assert new_session.config.api_key == "new-rotated-key"
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
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
        ):
            response = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

        assert response == {
            "status": "started",
            "booth_id": booth_id,
            "provider": "local",
            "model": "tiny",
        }
        assert booth_id in active_workers
        session = active_workers[booth_id]
        assert session.state == api_v1.State.RUNNING

        status_db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))
        status_resp = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=status_db, token=object())
        assert status_resp["statuses"]["en"]["transcription_running"] is True
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
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
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
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import _worker_config_matches

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
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
        ):
            db1 = _db(_result(first=event), _result(first=db_booth))
            db2 = _db(_result(first=event), _result(first=db_booth))

            task1 = asyncio.create_task(api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db1, token=object()))
            # Give task1 a tick to enter start_transcription_worker and reach State.STARTING
            await asyncio.sleep(0.01)

            assert booth_id in active_workers
            assert active_workers[booth_id].state == api_v1.State.STARTING

            task2 = asyncio.create_task(api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object()))
            await asyncio.sleep(0.01)

            # Unblock FFmpeg
            ready_gate.set()

            res1, res2 = await asyncio.gather(task1, task2)
            assert res1["status"] == "started"
            assert res2["status"] == "already_running"
            assert res1["booth_id"] == booth_id
            assert res2["booth_id"] == booth_id
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
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
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
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
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
        assert retained_session.state == api_v1.State.STOPPING

        # A subsequent start request sees the worker is still stopping and waits / does not duplicate
        slow_done.set()  # Unblock wait_until_stopped for subsequent attempts
        with (
            patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
            patch("portal.transcription.worker.settings.transcription_startup_timeout_secs", 0.05),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
        ):
            # When wait_until_stopped succeeds, next start cleanly succeeds
            retained_session.state = api_v1.State.STOPPED
            db2 = _db(_result(first=event), _result(first=db_booth))

            async def instant_aenter(self):
                self.process = MagicMock(pid=8888)
                self.stderr_task = MagicMock(done=MagicMock(return_value=True))
                return self.process

            with patch("portal.transcription.worker.FfmpegProcess.__aenter__", instant_aenter):
                resp = await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object())
                assert resp["status"] == "started"
                assert active_workers[booth_id] is not retained_session
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
async def test_admin_and_api_v1_resolve_identical_configuration(event):
    """Admin route and API v1 route resolve identical provider, model, API key, and language."""
    from portal.crypto import encrypt_val
    from portal.transcription.worker import _worker_config_matches

    event.transcription_api_enabled = True
    event.encrypted_openai_api_key = encrypt_val("sk-shared-test-key")
    event.encrypted_deepgram_api_key = None
    event.encrypted_nvidia_api_key = None
    event.encrypted_elevenlabs_api_key = None

    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="openai",
        transcription_model="whisper-1",
        language_code="fr",
        room_id=ROOM_ID,
        event=event,
    )

    # API v1 resolution
    provider_v1, model_v1, config_v1 = api_v1.resolve_transcription_config(db_booth, event=event)
    canonical_lang_v1 = api_v1.validate_language_code("fr")

    # Admin route resolution (from admin.py lines 2419-2428)
    from portal.transcription import ProviderConfig, ProviderEnum, get_api_key

    provider_admin = db_booth.transcription_provider
    model_admin = db_booth.transcription_model
    api_key_admin = get_api_key(event, ProviderEnum(provider_admin))
    config_admin = ProviderConfig(api_key=api_key_admin)
    lang_admin = db_booth.language_code

    # Assert identical resolved values
    assert provider_v1 == provider_admin == "openai"
    assert model_v1 == model_admin == "whisper-1"
    assert config_v1.api_key == config_admin.api_key == "sk-shared-test-key"
    assert canonical_lang_v1 == lang_admin == "fr"

    # Simulate worker started by admin
    admin_session = SimpleNamespace(
        provider_name=provider_admin,
        model_size=model_admin,
        config=config_admin,
        language_code=lang_admin,
        transcription_language=None,
    )

    # API v1 sees admin worker as matching config
    assert (
        _worker_config_matches(
            admin_session,
            provider_v1,
            model_v1,
            config_v1,
            transcription_language=None,
            language_code=canonical_lang_v1,
        )
        is True
    )


@pytest.mark.anyio
async def test_different_configuration_parameters_not_ignored():
    """Genuinely different provider, model, API key, or language returns False and is not ignored."""
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import _worker_config_matches

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
    from portal.booth_identity import make_booth_id

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
    from portal.config import settings
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import start_transcription_worker

    booth_id = "test-stopping-timeout-booth"

    async def slow_wait():
        await asyncio.sleep(0.5)

    stuck_stopping_session = SimpleNamespace(
        provider_name="local",
        model_size="tiny",
        language_code="en",
        transcription_language=None,
        session_id="stuck-session-123",
        state=api_v1.State.STOPPING,
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
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import _worker_config_matches

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
async def test_webhook_pairing_across_retries():
    """Verify started webhook is emitted once, not re-emitted on retry, and exactly one stopped is emitted."""
    from portal.transcription.providers.base import ProviderConfig

    booth_id = "test-webhook-pairing-booth"
    emitted_events = []

    async def mock_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))

    stream_attempts = 0

    class CrashOnceProvider:
        async def run_stream(self, process, language, model, config, callback, booth, room):
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
    import time

    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import start_transcription_worker

    booth_id = "test-spawn-failure-booth"
    emitted_events = []

    async def mock_enqueue(event_name, payload):
        emitted_events.append((event_name, payload))

    async def failing_aenter(self):
        raise FileNotFoundError("ffmpeg binary not found on PATH")

    start_time = time.monotonic()
    try:
        with (
            patch("portal.transcription.worker._wh_worker.enqueue_webhook", side_effect=mock_enqueue),
            patch("portal.transcription.worker.FfmpegProcess.__aenter__", failing_aenter),
            patch("portal.transcription.worker.PROVIDERS", {"local": MagicMock()}),
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
    """When start_transcription_worker fails due to stop timeout, route returns 503 via error_code or message."""
    mock_booth = MagicMock()
    db_booth = SimpleNamespace(
        transcription_enabled=True,
        transcription_provider="local",
        transcription_model="tiny",
        event=event,
    )

    from portal.transcription.worker import ERROR_CODE_STOPPING_TIMEOUT, TranscriptionStartResult

    # 1. Via structured error_code
    db1 = _db(_result(first=event), _result(first=db_booth))
    mock_failed_by_code = TranscriptionStartResult(
        status="failed",
        provider="local",
        model="tiny",
        error="Custom teardown timeout error message",
        error_code=ERROR_CODE_STOPPING_TIMEOUT,
    )

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch("portal.routers.api_v1.start_transcription_worker", return_value=mock_failed_by_code),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db1, token=object())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Failed to start transcription worker"

    # 2. Via backwards-compatible substring fallback when error_code is None
    db2 = _db(_result(first=event), _result(first=db_booth))
    mock_failed_by_str = TranscriptionStartResult(
        status="failed",
        provider="local",
        model="tiny",
        error="Timed out waiting for existing worker to stop",
        error_code=None,
    )

    with (
        patch.object(api_v1.booths, "get_booth_sync", return_value=mock_booth),
        patch("portal.routers.api_v1.start_transcription_worker", return_value=mock_failed_by_str),
    ):
        with pytest.raises(api_v1.HTTPException) as exc_info:
            await api_v1.start_transcription(EVENT_SLUG, ROOM_ID, "en", db=db2, token=object())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Failed to start transcription worker"
