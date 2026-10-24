"""Tests for the /api/v1 room transcription stop and status routes.

Both routes used to call make_booth_id without room_id and raised TypeError on
every request. The status route additionally called a BoothRegistry method that
does not exist, and read transcription state from an attribute nothing sets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portal.routers.api_v1 as api_v1
from portal.database import create_booth, create_event, create_room
from portal.models import Base, TranscriptSegment, TranscriptTranslation
from portal.transcription.worker import active_workers

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
    return db


@pytest.fixture
def event():
    return SimpleNamespace(id=1, slug=EVENT_SLUG)


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
async def test_export_returns_segments_as_json(event):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="First utterance", language_code="en", created_at=now),
        SimpleNamespace(id=2, text="Second utterance", language_code="en", created_at=now + timedelta(seconds=10)),
    ]
    db = _db(_result(first=event), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert len(response) == 2
    assert response[0]["id"] == 1
    assert response[0]["text"] == "First utterance"
    assert response[0]["language_code"] == "en"
    assert response[0]["created_at"] == now.isoformat()
    assert response[1]["id"] == 2
    assert response[1]["text"] == "Second utterance"


@pytest.mark.anyio
async def test_export_returns_empty_list_when_no_segments(event):
    db = _db(_result(first=event), _result(all_=[]))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=db, token=object())

    assert response == []


@pytest.mark.anyio
async def test_export_unknown_event_is_404():
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=None)), token=object())

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_export_vtt_format(event):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Hello world", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="vtt", db=db, token=object())

    assert response.media_type == "text/vtt"
    body = response.body.decode("utf-8")
    assert body.startswith("WEBVTT\n")
    assert "12:00:00.000 --> 12:00:05.000" in body
    assert "Hello world" in body


@pytest.mark.anyio
async def test_export_srt_format(event):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Hello world", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="srt", db=db, token=object())

    assert response.media_type == "text/plain"
    body = response.body.decode("utf-8")
    assert "1\n12:00:00,000 --> 12:00:05,000\nHello world" in body


@pytest.mark.anyio
async def test_export_accept_header_content_negotiation(event):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Header test", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", accept="text/vtt", db=db, token=object())

    assert response.media_type == "text/vtt"
    body = response.body.decode("utf-8")
    assert body.startswith("WEBVTT\n")
    assert "Header test" in body


@pytest.mark.anyio
async def test_export_target_lang_translations(event):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    translations = [
        SimpleNamespace(id=1, text="Hola mundo", language_code="es", created_at=now),
    ]
    db = _db(_result(first=event), _result(all_=translations))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", target_lang="es", db=db, token=object())

    assert len(response) == 1
    assert response[0]["id"] == 1
    assert response[0]["text"] == "Hola mundo"
    assert response[0]["language_code"] == "es"


@pytest.mark.anyio
async def test_export_real_db_queries_and_filters(real_db):
    event = await create_event(real_db, slug=EVENT_SLUG, display_name="PyCon 2026")
    room_a = await create_room(real_db, event_id=event.id, display_name="Hall A")
    room_b = await create_room(real_db, event_id=event.id, display_name="Hall B")

    t1 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 24, 10, 5, 0, tzinfo=timezone.utc)

    seg_a1 = TranscriptSegment(room_id=room_a.id, language_code="en", text="Welcome to room A", created_at=t1)
    seg_a2 = TranscriptSegment(room_id=room_a.id, language_code="en", text="Room A ongoing", created_at=t2)
    seg_a_fr = TranscriptSegment(room_id=room_a.id, language_code="fr", text="Bienvenue", created_at=t1)
    seg_b = TranscriptSegment(room_id=room_b.id, language_code="en", text="Room B text", created_at=t1)

    real_db.add_all([seg_a1, seg_a2, seg_a_fr, seg_b])
    await real_db.flush()

    trans_a1_es = TranscriptTranslation(
        segment_id=seg_a1.id, language_code="es", text="Bienvenido a la sala A", created_at=t1
    )
    real_db.add(trans_a1_es)
    await real_db.flush()

    # 1. Base JSON export: only room_a and language "en"
    result = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", db=real_db, token=object())
    assert [r["text"] for r in result] == ["Welcome to room A", "Room A ongoing"]

    # 2. Filter after T1
    result_after = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", after=t1, db=real_db, token=object())
    assert [r["text"] for r in result_after] == ["Room A ongoing"]

    # 3. Filter before T2
    result_before = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", before=t2, db=real_db, token=object())
    assert [r["text"] for r in result_before] == ["Welcome to room A"]

    # 4. Translations target_lang
    result_trans = await api_v1.export_transcript(
        EVENT_SLUG, room_a.id, "en", target_lang="es", db=real_db, token=object()
    )
    assert len(result_trans) == 1
    assert result_trans[0]["text"] == "Bienvenido a la sala A"
    assert result_trans[0]["language_code"] == "es"

    # 5. VTT format output
    vtt_res = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", format="vtt", db=real_db, token=object())
    vtt_text = vtt_res.body.decode("utf-8")
    assert vtt_text.startswith("WEBVTT\n")
    assert "Welcome to room A" in vtt_text
    assert "Room A ongoing" in vtt_text
