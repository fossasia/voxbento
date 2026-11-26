"""The floor follows the language actually spoken on it (#1397)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal import floor_language
from portal.auth import create_user_token
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_room_membership,
)
from portal.models import Room, RoomTranslationLanguage, TranscriptSegment
from portal.roles import ROOM_COORDINATOR
from portal.transcription import worker as worker_module
from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider
from portal.translations import worker as translation_module
from portal.translations.worker import TranslationWorker


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(floor_language, "clock", clock)
    return clock


def room(**fields) -> Room:
    values = {"id": 1, "floor_language_code": "en", "floor_language_override": None}
    values.update(fields)
    return Room(**values)


# ── The store ────────────────────────────────────────────────────────────


def test_the_floor_starts_in_the_spoken_language(clock):
    assert floor_language.requested(room()) == "en"
    assert floor_language.current(room()) == "en"


def test_a_change_reaches_the_worker_only_after_the_debounce(clock):
    assert floor_language.current(room()) == "en"
    floor_language.note_change(1)
    clock.now += 0.9
    assert floor_language.current(room(floor_language_override="th")) == "en"
    clock.now += 0.1
    assert floor_language.current(room(floor_language_override="th")) == "th"


def test_a_switch_back_inside_the_window_changes_nothing(clock):
    assert floor_language.current(room()) == "en"
    floor_language.note_change(1)
    clock.now += 0.5
    assert floor_language.current(room(floor_language_override="th")) == "en"
    floor_language.note_change(1)
    clock.now += 5
    assert floor_language.current(room()) == "en"


def test_rapid_back_and_forth_restarts_at_most_once_per_window(clock):
    """10 switches in 5 s: the worker's language changes only once the last one has held."""
    seen = [floor_language.current(room())]
    for flip in range(10):
        floor_language.set_booth_language(1, "ev-1-th", "th" if flip % 2 == 0 else None)
        for _ in range(5):
            clock.now += 0.1
            seen.append(floor_language.current(room()))
    changes = sum(1 for before, after in zip(seen, seen[1:]) if before != after)
    assert changes == 0  # every switch was undone inside its window
    floor_language.set_booth_language(1, "ev-1-th", "th")
    clock.now += 1
    assert floor_language.current(room()) == "th"


def test_a_room_seen_for_the_first_time_gets_its_language_at_once(clock):
    """After a portal restart the stored choice applies without waiting."""
    assert floor_language.current(room(floor_language_override="th")) == "th"


def test_a_booth_on_return_output_wins_over_the_manual_choice(clock):
    manual = room(floor_language_override="de")
    floor_language.set_booth_language(1, "ev-1-th", "th")
    assert floor_language.requested(manual) == "th"
    floor_language.set_booth_language(1, "ev-1-th", None)
    assert floor_language.requested(manual) == "de"


def test_only_the_booth_holding_the_floor_releases_it(clock):
    floor_language.set_booth_language(1, "ev-1-th", "th")
    floor_language.set_booth_language(1, "ev-1-de", "de")  # took over
    changed = floor_language._changed_at[1]
    clock.now += 3
    floor_language.set_booth_language(1, "ev-1-th", None)  # late release of the first booth
    assert floor_language.requested(room()) == "de"
    assert floor_language._changed_at[1] == changed
    floor_language.set_booth_language(1, "ev-1-de", "de")  # repeated claim
    assert floor_language._changed_at[1] == changed
    floor_language.set_booth_language(2, "ev-2-de", None)  # nothing held in that room
    assert 2 not in floor_language._changed_at


def test_choices_are_the_spoken_language_then_the_listener_languages():
    hall = room()
    hall.translation_languages = [
        RoomTranslationLanguage(language_code="de", language_name="German", enabled=True),
        RoomTranslationLanguage(language_code="th", language_name="Thai", enabled=True),
        RoomTranslationLanguage(language_code="fr", language_name="French", enabled=False),
    ]
    booths = [type("Booth", (), {"language_code": code})() for code in ("th", "en", "floor")]
    assert floor_language.choices(hall, booths) == ["en", "th", "de"]


# ── Mission Control ──────────────────────────────────────────────────────


@pytest.fixture
async def world():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        admin = await create_user(s, email="admin@example.com", display_name="Admin")
        admin.is_admin = True
        coordinator = await create_user(s, email="coord@example.com", display_name="Coord")
        stranger = await create_user(s, email="other@example.com", display_name="Other")
        event = await create_event(s, slug="summit", display_name="Summit")
        hall = await create_room(s, event_id=event.id, display_name="Main Hall")
        hall.floor_language_code = "en"
        hall.floor_transcription_enabled = True
        hall.floor_transcription_provider = "local"
        s.add(RoomTranslationLanguage(room_id=hall.id, language_code="de", language_name="German", enabled=True))
        await create_booth(s, event_id=event.id, room_id=hall.id, language_code="th", language_name="Thai")
        await set_room_membership(s, user_id=coordinator.id, room_id=hall.id, role=ROOM_COORDINATOR)
        ids = {"event": event.id, "room": hall.id}
        ids["admin"] = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
        ids["coordinator"] = {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)}
        ids["stranger"] = {"user_token": create_user_token(user_id=stranger.id, email=stranger.email)}
    yield ids
    await dispose()


def client(cookies) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


async def stored_override(room_id: int) -> str | None:
    async with get_session() as s:
        return (await s.get(Room, room_id)).floor_language_override


@pytest.mark.anyio
async def test_coordinator_picks_the_floor_language(world, auto_csrf, clock):
    url = f"/api/rooms/{world['room']}/floor-language"
    async with client(world["coordinator"]) as c:
        response = await c.post(url, json={"language_code": "th"})
        assert response.status_code == 200, response.text
        assert response.json() == {"language_code": "th"}
        assert await stored_override(world["room"]) == "th"
        assert floor_language._changed_at[world["room"]] == clock.now

        # Picking the spoken language clears the choice.
        clock.now += 5
        assert (await c.post(url, json={"language_code": "en"})).json() == {"language_code": "en"}
        assert await stored_override(world["room"]) is None
        assert floor_language._changed_at[world["room"]] == clock.now

        # The same choice again does not start a new window.
        clock.now += 5
        assert (await c.post(url, json={"language_code": "en"})).status_code == 200
        assert floor_language._changed_at[world["room"]] == clock.now - 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"language_code": "fr"}, 400),  # not a language of this room
        ({"language_code": "floor"}, 400),
        ({"language_code": ""}, 400),
        ({"language_code": 7}, 422),
        ({"language_code": "th", "extra": 1}, 422),
        ({}, 422),
        (["th"], 422),
    ],
)
async def test_bad_floor_language_requests_change_nothing(world, auto_csrf, body, status):
    async with client(world["admin"]) as c:
        response = await c.post(f"/api/rooms/{world['room']}/floor-language", json=body)
    assert response.status_code == status
    assert await stored_override(world["room"]) is None
    assert world["room"] not in floor_language._changed_at


@pytest.mark.anyio
async def test_floor_language_needs_room_access_and_csrf(world, auto_csrf):
    async with client(world["stranger"]) as c:
        response = await c.post(f"/api/rooms/{world['room']}/floor-language", json={"language_code": "th"})
    assert response.status_code == 403
    async with client(world["admin"]) as c:
        response = await c.post(f"/api/rooms/{world['room'] + 99}/floor-language", json={"language_code": "th"})
    assert response.status_code == 404
    assert await stored_override(world["room"]) is None


@pytest.mark.anyio
async def test_floor_language_refuses_a_missing_csrf_token(world):
    async with client(world["admin"]) as c:
        response = await c.post(f"/api/rooms/{world['room']}/floor-language", json={"language_code": "th"})
    assert response.status_code == 403
    assert await stored_override(world["room"]) is None


async def mission_control_rooms(cookies) -> list[dict]:
    async with client(cookies) as c:
        page = await c.get("/mission-control/summit/")
    assert page.status_code == 200
    return json.loads(html.unescape(re.search(r"data-config='([^']*)'", page.text).group(1)))["rooms"]


@pytest.mark.anyio
async def test_mission_control_shows_the_floor_language_picker(world):
    [hall] = await mission_control_rooms(world["coordinator"])
    assert hall["floor_language"] == {
        "value": "en",
        "manual": "en",
        "choices": [
            {"code": "en", "name": "English"},
            {"code": "th", "name": "Thai"},
            {"code": "de", "name": "German"},
        ],
        "held_by": None,
    }

    # A two-way booth on return output holds the floor; the manual choice comes back after.
    async with get_session() as s:
        (await s.get(Room, world["room"])).floor_language_override = "de"
    floor_language.set_booth_language(world["room"], f"summit-{world['room']}-th", "th")
    [hall] = await mission_control_rooms(world["coordinator"])
    assert hall["floor_language"]["value"] == "th"
    assert hall["floor_language"]["manual"] == "de"
    assert hall["floor_language"]["held_by"] == {"code": "th", "name": "Thai"}
    floor_language.set_booth_language(world["room"], f"summit-{world['room']}-th", None)

    # A choice whose language was later taken out of the room is still shown.
    async with get_session() as s:
        (await s.get(Room, world["room"])).floor_language_override = "fr"
    [hall] = await mission_control_rooms(world["admin"])
    assert hall["floor_language"]["value"] == "fr"
    assert hall["floor_language"]["choices"][-1] == {"code": "fr", "name": "French"}

    async with get_session() as s:
        (await s.get(Room, world["room"])).floor_transcription_enabled = False
    [hall] = await mission_control_rooms(world["admin"])
    assert hall["floor_language"] is None


# ── Captions carry the language they were transcribed in ─────────────────


class SpanishProvider(TranscriptionProvider):
    """Returns one Spanish sentence per chunk and records the language it was asked for."""

    def __init__(self):
        self.languages = []

    async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
        self.languages.append(language_code)
        return "Hola a todos y bienvenidos a la sesión."


class FakeStdout:
    def __init__(self):
        self.chunks = [b"\0" * 96000]

    async def readexactly(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        raise asyncio.IncompleteReadError(b"", n)


class FakeProcess:
    returncode = None

    def __init__(self):
        self.stdout = FakeStdout()


@contextlib.asynccontextmanager
async def fake_ffmpeg(*_args):
    yield FakeProcess()


@pytest.mark.anyio
async def test_floor_captions_and_segments_carry_the_worker_language(world, monkeypatch):
    provider = SpanishProvider()
    monkeypatch.setitem(worker_module.PROVIDERS, "fake", provider)
    monkeypatch.setattr(worker_module, "FfmpegProcess", fake_ffmpeg)
    translate = AsyncMock()
    monkeypatch.setattr(TranslationWorker, "handle_translation", translate)
    sent = []

    async def broadcast(booth_id, payload):
        sent.append(payload)

    booth_id = f"summit-{world['room']}-floor"
    session = worker_module.TranscriptionWorkerSession(
        "summit", "floor", booth_id, broadcast, "fake", "tiny", ProviderConfig(api_key=None), "es", world["room"]
    )
    session.start()
    for _ in range(200):
        if translate.await_count:
            break
        await asyncio.sleep(0.01)
    session.stop()
    await session.wait_until_stopped()

    assert provider.languages[0] == "es"
    final = next(p for p in sent if isinstance(p, dict) and p.get("status") == "final")
    assert final["source_language"] == "es"
    assert final["text"] == "Hola a todos y bienvenidos a la sesión."
    async with get_session() as s:
        segment = await s.scalar(select(TranscriptSegment))
    assert (segment.language_code, segment.source_language_code) == ("floor", "es")


@pytest.mark.anyio
async def test_partial_captions_carry_the_worker_language():
    from portal.transcription.aggregator import CaptionAggregator, CaptionSource, caption_source

    sent = []

    async def broadcast(booth_id, payload):
        sent.append(payload)

    async def run():
        caption_source.set(CaptionSource("th"))
        aggregator = CaptionAggregator(broadcast)
        await aggregator.handle_partial("booth-1", "Sawasdee")
        await aggregator.handle_interim("booth-1", "khrap")
        await aggregator.handle_chunk("booth-1", "Sawasdee khrap")

    await asyncio.create_task(run())
    assert [payload["source_language"] for payload in sent] == ["th", "th", "th"]

    # Outside a worker (no language known) the payloads stay as they were.
    sent.clear()
    aggregator = CaptionAggregator(broadcast)
    await aggregator.handle_partial("booth-1", "Hello")
    await aggregator.handle_final("booth-1", "Hello there my friends.")
    assert len(sent) == 2 and not any("source_language" in payload for payload in sent)


# ── Translation translates from the segment's language ───────────────────


@pytest.mark.anyio
async def test_targets_in_the_spoken_language_are_not_translated_or_voiced(world, monkeypatch):
    async with get_session() as s:
        hall = await s.get(Room, world["room"])
        hall.floor_translation_enabled = True
        hall.floor_translation_provider = "local"
        hall.floor_translation_model = "test"
        s.add(RoomTranslationLanguage(room_id=hall.id, language_code="th", language_name="Thai", enabled=True))
        segment = TranscriptSegment(
            room_id=hall.id, language_code="floor", source_language_code="th", text="Sawasdee khrap"
        )
        s.add(segment)
        await s.flush()
        segment_id = segment.id

    calls = []

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        calls.append((kwargs["target_lang_code"], kwargs["source_language_code"], source_lang_name))
        return "Hallo"

    worker = TranslationWorker(AsyncMock())
    synthesize = AsyncMock(return_value=b"audio")
    offered = []
    monkeypatch.setattr(translation_module.youtube_captions, "wants", lambda room_id, lang: lang == "th")
    monkeypatch.setattr(translation_module.youtube_captions, "offer", lambda *args: offered.append(args))
    with (
        patch.object(worker, "_call_llm", new=fake_call_llm),
        patch("portal.tts.worker.synthesize", new=synthesize),
        patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock) as bundle,
    ):
        await worker.handle_translation(world["room"], segment_id, "Sawasdee khrap", "summit-1-floor", "uuid", 1)

    assert calls == [("de", "th", "Thai")]
    assert [call.args[2] for call in synthesize.await_args_list] == ["de"]
    th = next(call for call in bundle.await_args_list if call.args[1] == "th")
    assert th.args[3] == b"" and th.args[7] == "Sawasdee khrap"
    # A YouTube track in the spoken language gets the original words.
    assert [args for args in offered if args[1] == "th"] == [(world["room"], "th", "Sawasdee khrap", None)]
