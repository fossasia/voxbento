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
    result.all.return_value = list(all_)
    return result


def _db(*results):
    """Construct a mock AsyncSession executing predefined results sequentially."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


@pytest.fixture
def event():
    """Fixture providing a mock Event namespace."""
    return SimpleNamespace(id=1, slug=EVENT_SLUG)


@pytest.fixture(autouse=True)
def _skip_rbac():
    """Bypass token RBAC validation during endpoint unit tests."""
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
    """Verify stop_transcription derives the room-scoped booth id correctly."""
    stop = AsyncMock()
    with patch.object(api_v1, "stop_transcription_worker", stop):
        response = await api_v1.stop_transcription(
            EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=event)), token=object()
        )

    assert response == {"status": "stopped", "booth_id": "pycon2026-3-en"}
    stop.assert_awaited_once_with("pycon2026-3-en")


@pytest.mark.anyio
async def test_stop_for_an_unknown_event_is_a_404():
    """Verify stop_transcription raises 404 when the event is not found."""
    with pytest.raises(api_v1.HTTPException) as exc_info:
        await api_v1.stop_transcription(EVENT_SLUG, ROOM_ID, "en", db=_db(_result(first=None)), token=object())

    assert exc_info.value.status_code == 404


# ── status ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_status_reports_every_booth_in_the_room(event):
    """Verify get_transcription_status reports all booths configured in the room."""
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["room_id"] == ROOM_ID
    assert set(response["statuses"]) == {"en", "fr"}


@pytest.mark.anyio
async def test_status_reports_transcription_running_from_the_worker_registry(event, running_worker):
    """Verify running status is sourced directly from active_workers registry."""
    running_worker("pycon2026-3-en")
    booths_in_room = [SimpleNamespace(language_code="en"), SimpleNamespace(language_code="fr")]
    db = _db(_result(first=event), _result(all_=booths_in_room))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    # This was always False before: it read an attribute Booth does not have.
    assert response["statuses"]["en"]["transcription_running"] is True
    assert response["statuses"]["fr"]["transcription_running"] is False


@pytest.mark.anyio
async def test_status_does_not_count_a_worker_in_another_room(event, running_worker):
    """Verify worker status is scoped strictly to the requested room."""
    running_worker("pycon2026-4-en")
    db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))

    response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["statuses"]["en"]["transcription_running"] is False


@pytest.mark.anyio
async def test_status_reports_whether_the_booth_is_live_in_memory(event):
    """Verify is_active indicates whether the booth is live in memory."""
    db = _db(_result(first=event), _result(all_=[SimpleNamespace(language_code="en")]))
    live = SimpleNamespace(booth_id="pycon2026-3-en")

    with patch.object(
        api_v1.booths, "get_booth_sync", side_effect=lambda bid: live if bid == "pycon2026-3-en" else None
    ):
        response = await api_v1.get_transcription_status(EVENT_SLUG, ROOM_ID, db=db, token=object())

    assert response["statuses"]["en"]["is_active"] is True


@pytest.mark.anyio
async def test_status_for_a_room_with_no_booths_is_empty(event):
    """Verify get_transcription_status returns empty status map when no booths exist."""
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


@pytest.mark.anyio
async def test_export_vtt_format(event):
    """Test exporting transcript as WebVTT with playback elapsed timestamps."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Hello world", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="vtt", db=db, token=object())

    assert response.media_type == "text/vtt"
    body = response.body.decode("utf-8")
    assert body.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:05.000" in body
    assert "Hello world" in body


@pytest.mark.anyio
async def test_export_vtt_empty_format(event):
    """Test exporting empty transcript as WebVTT terminates header with two newlines."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    db = _db(_result(first=event), _result(first=booth), _result(all_=[]))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="vtt", db=db, token=object())

    assert response.media_type == "text/vtt"
    assert response.body.decode("utf-8") == "WEBVTT\n\n"


@pytest.mark.anyio
async def test_export_srt_format(event):
    """Test exporting transcript as SubRip (SRT) format with playback timestamps."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Hello world", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="srt", db=db, token=object())

    assert response.media_type == "text/plain"
    body = response.body.decode("utf-8")
    assert "1\n00:00:00,000 --> 00:00:05,000\nHello world" in body


@pytest.mark.anyio
async def test_export_subtitle_cues_do_not_overlap(event):
    """Test that consecutive cues shorter than 5s do not overlap in subtitles."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=2)
    segments = [
        SimpleNamespace(id=1, text="Short utterance", language_code="en", created_at=t0),
        SimpleNamespace(id=2, text="Next utterance", language_code="en", created_at=t1),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="vtt", db=db, token=object())
    body = response.body.decode("utf-8")
    assert "00:00:00.000 --> 00:00:02.000" in body
    assert "00:00:02.000 --> 00:00:07.000" in body


@pytest.mark.anyio
async def test_export_accept_header_content_negotiation(event):
    """Test content negotiation via Accept header."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Header test", language_code="en", created_at=now),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", accept="text/vtt", db=db, token=object())

    assert response.media_type == "text/vtt"
    body = response.body.decode("utf-8")
    assert body.startswith("WEBVTT\n")
    assert "Header test" in body


@pytest.mark.anyio
async def test_export_accept_header_srt_media_types(event):
    """Test content negotiation via Accept header for SRT formats."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="SRT test", language_code="en", created_at=now),
    ]
    db1 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res1 = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", accept="text/srt", db=db1, token=object())
    assert res1.media_type == "text/srt"

    db2 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res2 = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="application/x-subrip", db=db2, token=object()
    )
    assert res2.media_type == "application/x-subrip"


@pytest.mark.anyio
async def test_export_real_db_queries_and_filters(real_db):
    """Test real SQLite database integration with filters, translations, and non-UTC offsets."""
    event = await create_event(real_db, slug=EVENT_SLUG, display_name="PyCon 2026")
    room_a = await create_room(real_db, event_id=event.id, display_name="Hall A")
    room_b = await create_room(real_db, event_id=event.id, display_name="Hall B")
    await create_booth(real_db, event_id=event.id, room_id=room_a.id, language_code="en", language_name="English")

    t1 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 24, 10, 5, 0, tzinfo=timezone.utc)

    seg_a1 = TranscriptSegment(room_id=room_a.id, language_code="en", text="Welcome to room A", created_at=t1)
    seg_a2 = TranscriptSegment(room_id=room_a.id, language_code="en", text="Room A ongoing", created_at=t2)
    seg_a_fr = TranscriptSegment(room_id=room_a.id, language_code="fr", text="Bienvenue", created_at=t1)
    seg_b = TranscriptSegment(room_id=room_b.id, language_code="en", text="Room B text", created_at=t1)

    real_db.add_all([seg_a1, seg_a2, seg_a_fr, seg_b])
    await real_db.flush()

    # Translation stored later than speech
    t_stored = datetime(2026, 10, 24, 10, 1, 0, tzinfo=timezone.utc)
    trans_a1_es = TranscriptTranslation(
        segment_id=seg_a1.id, language_code="es", text="Bienvenido a la sala A", created_at=t_stored
    )
    real_db.add(trans_a1_es)
    await real_db.flush()

    # 1. Base JSON export: only room_a and language "en"
    result = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", db=real_db, token=object())
    assert [r["text"] for r in result["segments"]] == ["Welcome to room A", "Room A ongoing"]

    # 2. Filter after T1 with non-UTC offset (+05:30)
    t1_offset = datetime(2026, 10, 24, 15, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    result_after = await api_v1.export_transcript(
        EVENT_SLUG, room_a.id, "en", after=t1_offset, db=real_db, token=object()
    )
    assert [r["text"] for r in result_after["segments"]] == ["Room A ongoing"]

    # 3. Filter before T2
    result_before = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", before=t2, db=real_db, token=object())
    assert [r["text"] for r in result_before["segments"]] == ["Welcome to room A"]

    # 4. Translations target_lang uses source segment timestamp (t1, not t_stored)
    result_trans = await api_v1.export_transcript(
        EVENT_SLUG, room_a.id, "en", target_lang="es", db=real_db, token=object()
    )
    assert len(result_trans["segments"]) == 1
    assert result_trans["segments"][0]["text"] == "Bienvenido a la sala A"
    assert result_trans["segments"][0]["created_at"] == t1.isoformat()

    # 5. VTT format output with playback offset
    vtt_res = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", format="vtt", db=real_db, token=object())
    vtt_text = vtt_res.body.decode("utf-8")
    assert vtt_text.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:05.000" in vtt_text
    assert "Welcome to room A" in vtt_text
    assert "00:05:00.000 --> 00:05:05.000" in vtt_text
    assert "Room A ongoing" in vtt_text

    # 6. Limit parameter caps returned segments
    result_limited = await api_v1.export_transcript(EVENT_SLUG, room_a.id, "en", limit=1, db=real_db, token=object())
    assert len(result_limited["segments"]) == 1
    assert result_limited["segments"][0]["text"] == "Welcome to room A"


@pytest.mark.anyio
async def test_export_vtt_sanitizes_cue_text(event):
    """WebVTT export collapses blank lines, escapes ampersands/angle brackets, and neutralizes arrows."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(
            id=1,
            text="Line 1\n\n\nLine 2 & <tag> --> arrow",
            language_code="en",
            created_at=now,
        ),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="vtt", db=db, token=object())
    body = response.body.decode("utf-8")

    # Blank lines collapsed to single newline
    assert "Line 1\nLine 2" in body
    # Ampersand escaped
    assert "&amp;" in body
    # Angle brackets escaped
    assert "&lt;tag&gt;" in body
    # Arrow marker neutralized
    assert "--> arrow" not in body
    assert "--&gt; arrow" in body


@pytest.mark.anyio
async def test_export_srt_sanitizes_blank_lines_without_html_escaping(event):
    """SRT export collapses blank lines but does not escape ampersands, brackets, or arrows."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(
            id=1,
            text="First line\n\nSecond line & <tag> --> test",
            language_code="en",
            created_at=now,
        ),
    ]
    db = _db(_result(first=event), _result(first=booth), _result(all_=segments))

    response = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", format="srt", db=db, token=object())
    body = response.body.decode("utf-8")

    # Blank lines collapsed to single newline
    assert "First line\nSecond line & <tag> --> test" in body
    # No WebVTT escaping in SRT
    assert "&amp;" not in body
    assert "&lt;" not in body


@pytest.mark.anyio
async def test_export_accept_header_case_insensitive(event):
    """Accept header content negotiation matches media-types case-insensitively."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(id=1, text="Case test", language_code="en", created_at=now),
    ]

    db_vtt = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res_vtt = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="TEXT/VTT; charset=utf-8", db=db_vtt, token=object()
    )
    assert res_vtt.media_type == "text/vtt"
    assert "WEBVTT" in res_vtt.body.decode("utf-8")

    db_srt = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res_srt = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="APPLICATION/X-SUBRIP", db=db_srt, token=object()
    )
    assert res_srt.media_type == "application/x-subrip"


def test_parse_accept_header_helper():
    """Verify _parse_accept_header orders by q-factor, discards q=0, and strips parameters."""
    assert api_v1._parse_accept_header(None) == []
    assert api_v1._parse_accept_header("") == []
    assert api_v1._parse_accept_header("   ") == []

    parsed = api_v1._parse_accept_header("text/vtt;q=0.5, text/srt;q=0.9")
    assert parsed == ["text/srt", "text/vtt"]

    parsed_q0 = api_v1._parse_accept_header("text/vtt;q=0, text/srt;q=0.8, application/json;q=0.0")
    assert parsed_q0 == ["text/srt"]

    parsed_params = api_v1._parse_accept_header("text/vtt; charset=utf-8; q=0.7, application/x-subrip; q=0.8")
    assert parsed_params == ["application/x-subrip", "text/vtt"]

    parsed_ties = api_v1._parse_accept_header("text/srt;q=0.8, application/x-subrip;q=0.8")
    assert parsed_ties == ["text/srt", "application/x-subrip"]


@pytest.mark.anyio
async def test_export_accept_header_qvalue_priority(event):
    """Test Accept header negotiation selects format with highest quality value."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [SimpleNamespace(id=1, text="Q test", language_code="en", created_at=now)]

    # SRT has higher q than VTT
    db1 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res1 = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="text/vtt;q=0.5, text/srt;q=0.9", db=db1, token=object()
    )
    assert res1.media_type == "text/srt"
    assert "00:00:00,000" in res1.body.decode("utf-8")

    # VTT has higher q than SRT
    db2 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res2 = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="text/srt;q=0.5, text/vtt;q=0.9", db=db2, token=object()
    )
    assert res2.media_type == "text/vtt"
    assert "WEBVTT" in res2.body.decode("utf-8")

    # application/json has higher q than text/vtt
    db3 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res3 = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="application/json;q=0.9, text/vtt;q=0.5", db=db3, token=object()
    )
    assert isinstance(res3, dict)
    assert "segments" in res3


@pytest.mark.anyio
async def test_export_accept_header_qzero_unacceptable(event):
    """Test Accept header treats q=0 as unacceptable for format and SRT media type."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [SimpleNamespace(id=1, text="Q0 test", language_code="en", created_at=now)]

    # text/vtt has q=0, falls back to text/srt
    db1 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res1 = await api_v1.export_transcript(
        EVENT_SLUG, ROOM_ID, "en", accept="text/vtt;q=0, text/srt;q=0.8", db=db1, token=object()
    )
    assert res1.media_type == "text/srt"

    # text/vtt has q=0, no other format offered, falls back to JSON
    db2 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res2 = await api_v1.export_transcript(EVENT_SLUG, ROOM_ID, "en", accept="text/vtt;q=0", db=db2, token=object())
    assert isinstance(res2, dict)
    assert "segments" in res2

    # application/x-subrip has q=0, picks text/srt for SRT Content-Type
    db3 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res3 = await api_v1.export_transcript(
        EVENT_SLUG,
        ROOM_ID,
        "en",
        format="srt",
        accept="application/x-subrip;q=0, text/srt;q=0.7",
        db=db3,
        token=object(),
    )
    assert res3.media_type == "text/srt"


@pytest.mark.anyio
async def test_export_explicit_format_preserves_behavior_with_accept(event):
    """Test explicit format query parameter preserves format behavior regardless of Accept."""
    booth = SimpleNamespace(id=10, room_id=ROOM_ID, language_code="en")
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    segments = [SimpleNamespace(id=1, text="Format param test", language_code="en", created_at=now)]

    # Explicit format=srt preserves SRT format even when Accept prefers text/vtt
    db1 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res1 = await api_v1.export_transcript(
        EVENT_SLUG,
        ROOM_ID,
        "en",
        format="srt",
        accept="text/vtt;q=1.0, application/x-subrip;q=0.8",
        db=db1,
        token=object(),
    )
    assert res1.media_type == "application/x-subrip"
    assert "00:00:00,000" in res1.body.decode("utf-8")

    # Explicit format=vtt preserves VTT format even when Accept prefers text/srt
    db2 = _db(_result(first=event), _result(first=booth), _result(all_=segments))
    res2 = await api_v1.export_transcript(
        EVENT_SLUG,
        ROOM_ID,
        "en",
        format="vtt",
        accept="text/srt;q=1.0",
        db=db2,
        token=object(),
    )
    assert res2.media_type == "text/vtt"
    assert "WEBVTT" in res2.body.decode("utf-8")
