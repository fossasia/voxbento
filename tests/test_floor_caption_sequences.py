from __future__ import annotations

import asyncio
import json
import struct
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError

import portal.database as db
import portal.tasks as tasks
import portal.transcription.aggregator as aggregation
import portal.transcription.worker as workers
from portal.models import Event, Room, RoomTranslationLanguage
from portal.transcription.aggregator import CaptionAggregator
from portal.transcription.providers.base import ProviderConfig
from portal.translations.worker import TranslationWorker
from portal.websockets.manager import broadcast_transcription, tts_manager


class FrameCollector:
    def __init__(self):
        self.frames = []

    async def send_bytes(self, frame):
        self.frames.append(frame)


async def drain_pipeline():
    """Wait for caption persistence and translation tasks before checking their output."""
    while tasks._tasks:
        await asyncio.gather(*list(tasks._tasks))


@pytest.fixture
async def sequence_db(tmp_path):
    """Isolate each stream's persistent counter and finish its background work on teardown."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'sequence.db').as_posix()}"
    db.configure(url)
    await db.init_db()
    try:
        yield url
    finally:
        await drain_pipeline()
        await db.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["jitsi_bot", "program_ingest"])
@pytest.mark.parametrize("recovery", ["retry", "replacement"])
async def test_recovered_floor_keeps_sequence_identity(source, recovery, monkeypatch, sequence_db):
    """Real provider retries/replacement keep captions and translated PCM in one sequence."""
    async with db.get_session() as session:
        event = Event(slug="sequence-demo", display_name="Sequence demo")
        session.add(event)
        await session.flush()
        room = Room(
            event_id=event.id,
            display_name="Hall",
            floor_source=source,
            floor_language_code="en",
            floor_translation_enabled=True,
            floor_translation_provider="local",
            floor_translation_model="test",
        )
        session.add(room)
        await session.flush()
        session.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=True))
    booth_id = f"{event.slug}-{room.id}-floor"
    collector = FrameCollector()
    tts_manager.add(collector, room.id, "fr", booth_id)
    before_done, after_done = asyncio.Event(), asyncio.Event()
    calls = 0
    captions = []

    async def callback(booth, payload):
        captions.append(payload)
        await broadcast_transcription(booth, payload)

    current_process = None

    class FakeProcess:
        def __init__(self, phase):
            self.phase = phase
            self.returncode = None
            self.eof = asyncio.Event()
            self.stdout = self

        async def read(self, size):
            await self.eof.wait()
            return b""

    class ProviderSocket:
        def __init__(self, process):
            self.process = process
            self.number = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, message):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            """Emit a bounded provider transcript stream, then signal EOF or await replacement."""
            await drain_pipeline()
            limit = 2 if self.process.phase == "before" else 3
            if self.number == limit:
                if self.process.phase == "before":
                    before_done.set()
                    if recovery == "retry":
                        self.process.returncode = 0
                        self.process.eof.set()
                        raise StopAsyncIteration
                else:
                    after_done.set()
                await asyncio.Event().wait()
            self.number += 1
            text = f"{self.process.phase} recovery sentence {self.number}"
            return json.dumps({"channel": {"alternatives": [{"transcript": text}]}, "is_final": True})

    class FakeFfmpeg:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            """Give the replacement worker an independent simulated media input."""
            nonlocal calls, current_process
            calls += 1
            current_process = FakeProcess("before" if calls == 1 else "after")
            return current_process

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(workers, "FfmpegProcess", FakeFfmpeg)
    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: ProviderSocket(current_process))
    monkeypatch.setattr(
        TranslationWorker, "_call_llm", AsyncMock(side_effect=lambda *args, **kwargs: "French " + args[3])
    )
    monkeypatch.setattr("portal.tts.worker.synthesize", AsyncMock(return_value=b"\x01\x00" * 240))

    async def start():
        await workers.start_transcription_worker(
            event.slug,
            "floor",
            booth_id,
            callback,
            "deepgram",
            "nova-2",
            ProviderConfig("test-dummy-key"),
            transcription_language="en",
            room_id=room.id,
        )

    try:
        await start()
        await asyncio.wait_for(before_done.wait(), 10)
        if recovery == "replacement":
            await workers.stop_transcription_worker(booth_id)
            await start()
        await asyncio.wait_for(after_done.wait(), 10)
        parsed = []
        for frame in collector.frames:
            assert frame[0] == 1
            header_end = 5 + struct.unpack(">I", frame[1:5])[0]
            parsed.append((json.loads(frame[5:header_end]), frame[header_end:]))
        headers = [header for header, _ in parsed]
        assert [pcm for header, pcm in parsed if not header["audio_pending"]] == [b"\x01\x00" * 240] * 5
        assert [pcm for header, pcm in parsed if header["audio_pending"]] == [b""] * 5
        audio_headers = [header for header in headers if not header["audio_pending"]]
        sequences = [header["seq"] for header in audio_headers]
        assert len(headers) == 10
        assert len(audio_headers) == 5
        assert len({h["segment_id"] for h in audio_headers}) == 5
        assert sequences == [1, 2, 3, 4, 5], "A recovered live stream must not reuse the listener's consumed sequences"
        assert [caption["seq"] for caption in captions] == sequences
        assert all(header["seq"] == captions[index // 2]["seq"] for index, header in enumerate(headers))
        assert all(header["segment_id"] == captions[index // 2]["segment_id"] for index, header in enumerate(headers))
        assert all(header["error"] is None for header in headers)
        assert ("sync_offset_ms" in headers[-1]) == (source == "program_ingest")
    finally:
        await workers.stop_transcription_worker(booth_id)
        await drain_pipeline()
        tts_manager.remove(collector, room.id, "fr", booth_id)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["jitsi_bot", "program_ingest"])
async def test_floor_sequence_survives_database_reopen_and_source_switch(source, sequence_db):
    """Continue the stored sequence across a new DB connection and a change of floor source."""
    async with db.get_session() as session:
        event = Event(slug="durable-sequence", display_name="Durable sequence")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Hall", floor_source=source, floor_caption_seq=40)
        other = Room(event_id=event.id, display_name="Other hall", floor_source=source)
        session.add_all([room, other])
        await session.flush()
    booth_id = f"{event.slug}-{room.id}-floor"
    callback = AsyncMock()
    await CaptionAggregator(callback, room.id).handle_final(booth_id, "Before restart")
    await drain_pipeline()
    await db.dispose()
    db.configure(sequence_db)
    await CaptionAggregator(callback, room.id).handle_final(booth_id, "After restart")
    await drain_pipeline()
    async with db.get_session() as session:
        saved = await session.get(Room, room.id)
        saved.floor_source = "program_ingest" if source == "jitsi_bot" else "jitsi_bot"
    await CaptionAggregator(callback, room.id).handle_final(booth_id, "After source change")
    await drain_pipeline()
    assert [call.args[1]["seq"] for call in callback.await_args_list] == [41, 42, 43]
    other_callback = AsyncMock()
    await CaptionAggregator(other_callback, other.id).handle_final(f"{event.slug}-{other.id}-floor", "Other room")
    assert other_callback.await_args.args[1]["seq"] == 1


@pytest.mark.anyio
async def test_concurrent_floor_aggregators_allocate_distinct_sequences(sequence_db):
    """Deliver concurrent final captions in their allocated sequence order."""
    async with db.get_session() as session:
        event = Event(slug="concurrent-sequence", display_name="Concurrent sequence")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Hall", floor_source="jitsi_bot")
        session.add(room)
        await session.flush()
    callback = AsyncMock()
    await asyncio.gather(
        *(
            CaptionAggregator(callback, room.id).handle_final(f"{event.slug}-{room.id}-floor", f"Sentence {number}")
            for number in range(5)
        )
    )
    assert [call.args[1]["seq"] for call in callback.await_args_list] == [1, 2, 3, 4, 5]


@pytest.mark.anyio
async def test_floor_allocation_and_delivery_share_a_room_lock(sequence_db, monkeypatch):
    """A stalled publisher blocks its room's next allocation, but not a different room."""
    async with db.get_session() as session:
        event = Event(slug="ordered-floor", display_name="Ordered floor")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Hall")
        other = Room(event_id=event.id, display_name="Other hall")
        session.add_all([room, other])
        await session.flush()
    booth_id = f"{event.slug}-{room.id}-floor"
    other_booth_id = f"{event.slug}-{other.id}-floor"
    delivered = []
    paused, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_allocated = asyncio.Event()

    async def callback(booth, payload):
        """Delay the first room's initial publication to expose an overtaking caption."""
        if booth == booth_id and payload["seq"] == 1:
            paused.set()
            await release.wait()
        delivered.append((booth, payload["seq"]))

    first = CaptionAggregator(callback, room.id)
    second = CaptionAggregator(callback, room.id)
    original_allocate = second._allocate_floor_sequence

    async def observe_allocation(state):
        """Record entry into the second allocation rather than relying on a timed sleep."""
        second_allocated.set()
        return await original_allocate(state)

    monkeypatch.setattr(second, "_allocate_floor_sequence", observe_allocation)

    async def publish_second():
        second_started.set()
        await second.handle_final(booth_id, "Second")

    first_task = asyncio.create_task(first.handle_final(booth_id, "First"))
    second_task = None
    try:
        await asyncio.wait_for(paused.wait(), 5)
        second_task = asyncio.create_task(publish_second())
        await asyncio.wait_for(second_started.wait(), 5)
        await asyncio.wait_for(CaptionAggregator(callback, other.id).handle_final(other_booth_id, "Other room"), 5)
        assert not second_allocated.is_set()
        assert delivered == [(other_booth_id, 1)]
    finally:
        release.set()
        await asyncio.gather(first_task, *([second_task] if second_task is not None else []))
    assert [seq for booth, seq in delivered if booth == booth_id] == [1, 2]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["lookup", "update", "commit"])
async def test_counter_outage_delivers_original_text_and_recovers(sequence_db, monkeypatch, failure):
    """Keep UUID captions available during DB failures without inventing TTS sequences."""
    async with db.get_session() as session:
        event = Event(slug="outage-floor", display_name="Outage floor")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Hall", floor_caption_seq=20)
        session.add(room)
        await session.flush()
    booth_id = f"{event.slug}-{room.id}-floor"
    callback = AsyncMock()
    aggregator = CaptionAggregator(callback, room.id)
    await aggregator.handle_final(booth_id, "Connected")
    await drain_pipeline()
    original_get_session = aggregation.get_session

    @asynccontextmanager
    async def failing_session():
        """Roll back a real transaction at the requested counter-allocation boundary."""
        async with original_get_session() as session:
            original_scalar = session.scalar
            calls = 0

            async def scalar(statement):
                """Fail either the SELECT or UPDATE while retaining normal transaction cleanup."""
                nonlocal calls
                calls += 1
                if (failure == "lookup" and calls == 1) or (failure == "update" and calls == 2):
                    raise OperationalError("counter query", {}, ConnectionError("Simulated database outage"))
                return await original_scalar(statement)

            session.scalar = scalar
            yield session
            if failure == "commit":
                raise OperationalError("commit", {}, ConnectionError("Simulated commit failure"))

    with monkeypatch.context() as context:
        context.setattr(aggregation, "get_session", failing_session)
        await aggregator.handle_final(booth_id, "Still readable")
    fallback = callback.await_args.args[1]
    assert fallback["text"] == "Still readable"
    assert "seq" not in fallback
    assert fallback["segment_id"]
    assert not tasks._tasks  # No storage/translation task may emit unsequenced audio.
    await aggregator.handle_final(booth_id, "Recovered")
    payloads = [call.args[1] for call in callback.await_args_list]
    assert [payload.get("seq") for payload in payloads] == [21, None, 22]
    assert len({payload["segment_id"] for payload in payloads}) == 3


@pytest.mark.anyio
async def test_partial_caption_survives_database_outage(sequence_db, monkeypatch):
    """An optional timing lookup must not stop partial text from reaching listeners."""
    original_get_session = aggregation.get_session

    @asynccontextmanager
    async def unavailable_timing():
        async with original_get_session() as session:
            session.get = AsyncMock(side_effect=OperationalError("timing query", {}, ConnectionError("DB offline")))
            yield session

    monkeypatch.setattr(aggregation, "get_session", unavailable_timing)
    callback = AsyncMock()
    await CaptionAggregator(callback, room_id=1).handle_partial("outage-1-floor", "Live partial")
    payload = callback.await_args.args[1]
    assert payload["status"] == "partial" and payload["text"] == "Live partial"
    assert payload["segment_id"] and "sync_offset_ms" not in payload


@pytest.mark.anyio
@pytest.mark.parametrize("deletion", ["lookup", "update"])
async def test_deleted_floor_does_not_publish_a_late_final(sequence_db, monkeypatch, deletion):
    """Discard pending text if the room disappears before or during counter allocation."""
    async with db.get_session() as session:
        event = Event(slug="deleted-floor", display_name="Deleted floor")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Hall")
        session.add(room)
        await session.flush()
    callback = AsyncMock()
    aggregator = CaptionAggregator(callback, room.id)
    booth_id = f"{event.slug}-{room.id}-floor"
    await aggregator.handle_partial(booth_id, "Pending text")
    callback.reset_mock()
    original_get_session = aggregation.get_session

    @asynccontextmanager
    async def deleting_session():
        """Delete the real room at lookup or immediately before the returning UPDATE."""
        async with original_get_session() as session:
            original_scalar = session.scalar
            calls = 0

            async def scalar(statement):
                """Make the target SQL operation observe a room that has just disappeared."""
                nonlocal calls
                calls += 1
                if (deletion == "lookup" and calls == 1) or (deletion == "update" and calls == 2):
                    await session.execute(delete(Room).where(Room.id == room.id))
                return await original_scalar(statement)

            session.scalar = scalar
            yield session

    with monkeypatch.context() as context:
        context.setattr(aggregation, "get_session", deleting_session)
        await aggregator.handle_final(booth_id, "Late final")
    callback.assert_not_awaited()
    assert not tasks._tasks
    assert aggregator.states[booth_id].current_utterance == ""
    async with db.get_session() as session:
        assert await session.get(Room, room.id) is None
