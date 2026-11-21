"""YouTube captions, plan 006 part 2 (#1087): the owner's choice, YouTube set-up and the delivery lifecycle.

Reuses the Google mock from ``tests/test_youtube_account.py``. Caption POSTs never leave the process:
the sending engine's transport is replaced, and most tests only check which target the engine holds.
"""

from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest
from sqlalchemy import update

from portal import youtube_account as yt
from portal import youtube_caption_sync as sync
from portal import youtube_captions as yc
from portal.database import get_session
from portal.models import Event, Room, RoomTranslationLanguage
from tests.test_youtube_account import (  # noqa: F401 - yt_env is a fixture
    CAPTIONS_URL,
    LATER_ID,
    UPCOMING_ID,
    broadcast_url,
    connect,
    event_url,
    stored_room,
    yt_env,
)

pytestmark = pytest.mark.usefixtures("auto_csrf")

HTTPS_CAPTIONS_URL = "https://" + CAPTIONS_URL[len("http://") :]


@pytest.fixture(autouse=True)
def clean_caption_state(monkeypatch):
    for store in (yc.targets, yc.health, yc.clock_offset_ms, yc._queues, yc._tasks, yc._last_seq):
        store.clear()
    sync._running.clear()
    sync._retry_at.clear()
    monkeypatch.setattr(yc, "_transport", httpx.MockTransport(lambda request: httpx.Response(200)))
    yield
    for task in list(yc._tasks.values()):
        task.cancel()
    for store in (yc.targets, yc.health, yc.clock_offset_ms, yc._queues, yc._tasks, yc._last_seq):
        store.clear()
    sync._running.clear()
    sync._retry_at.clear()


def captions_url(env, room):
    return f"/workspace/events/{env['event'].id}/rooms/{room.id}/youtube/captions"


def status_url(env, room):
    return f"/workspace/events/{env['event'].id}/rooms/{room.id}/program-ingest"


async def set_room(room_id: int, **values) -> None:
    async with get_session() as session:
        await session.execute(update(Room).where(Room.id == room_id).values(**values))


async def add_language(room_id: int, code: str, name: str, enabled: bool = True) -> None:
    async with get_session() as session:
        session.add(RoomTranslationLanguage(room_id=room_id, language_code=code, language_name=name, enabled=enabled))


@pytest.fixture
async def captions_env(yt_env):  # noqa: F811 - the imported fixture
    """Connected, both rooms with floor captions and French translation, room 0 linked to a ready broadcast."""
    await connect(yt_env)
    for room in yt_env["rooms"]:
        await set_room(
            room.id,
            floor_transcription_enabled=True,
            floor_transcription_provider="deepgram",
            floor_translation_enabled=True,
        )
        await add_language(room.id, "fr", "French")
    response = await yt_env["client"].put(broadcast_url(yt_env, yt_env["rooms"][0]), json={"broadcast": UPCOMING_ID})
    assert response.status_code == 200, response.text
    return yt_env


async def turn_on(env, room, language="floor"):
    return await env["client"].put(captions_url(env, room), json={"language": language})


def puts(env):
    return env["google"]["puts"]


# ── The owner's choice ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_turning_captions_on_sets_up_the_broadcast_and_starts_delivery(captions_env):
    room = captions_env["rooms"][0]
    response = await turn_on(captions_env, room)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["language"] == "floor"
    assert body["state"] == "waiting"
    assert CAPTIONS_URL not in response.text and "SECRET" not in response.text
    assert (await stored_room(room.id)).youtube_captions_language == "floor"
    assert yc.targets[room.id].language == "floor"
    # Posted over https although the API returned http.
    assert yc.targets[room.id].url == HTTPS_CAPTIONS_URL
    [put] = puts(captions_env)
    assert put["params"]["part"] == "id,snippet,contentDetails"
    sent = put["body"]
    assert sent["id"] == UPCOMING_ID
    assert sent["contentDetails"]["closedCaptionsType"] == "closedCaptionsHttpPost"
    # The whole part goes back: nothing the broadcast had is dropped.
    assert sent["contentDetails"]["latencyPreference"] == "low"
    assert sent["contentDetails"]["boundStreamId"]
    assert "enableClosedCaptions" not in sent["contentDetails"]
    assert sent["snippet"] == {"title": f"Talk {UPCOMING_ID}", "scheduledStartTime": "2026-11-02T09:00:00Z"}


@pytest.mark.anyio
async def test_a_broadcast_already_set_up_is_not_updated_again(captions_env):
    room = captions_env["rooms"][0]
    assert (await turn_on(captions_env, room)).status_code == 200
    assert (await turn_on(captions_env, room, "fr")).status_code == 200
    assert len(puts(captions_env)) == 1
    assert yc.targets[room.id].language == "fr"


@pytest.mark.anyio
async def test_a_translation_can_be_chosen_and_off_stops_delivery(captions_env):
    room = captions_env["rooms"][0]
    assert (await turn_on(captions_env, room, "fr")).status_code == 200
    assert yc.wants(room.id, "fr")
    response = await turn_on(captions_env, room, None)
    assert response.status_code == 200
    assert response.json()["state"] == "off"
    assert room.id not in yc.targets
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("change", "language", "expected"),
    [
        ({}, "de", "That language is not enabled"),
        ({"floor_translation_enabled": False}, "fr", "Floor translation is off"),
        ({"floor_transcription_enabled": False}, "floor", "Turn on floor transcription"),
    ],
)
async def test_a_language_that_would_never_post_is_refused(captions_env, change, language, expected):
    room = captions_env["rooms"][0]
    if change:
        await set_room(room.id, **change)
    response = await turn_on(captions_env, room, language)
    assert response.status_code == 422
    assert expected in response.json()["detail"]
    assert room.id not in yc.targets
    assert puts(captions_env) == []
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
async def test_a_disabled_translation_language_is_refused(captions_env):
    room = captions_env["rooms"][0]
    await add_language(room.id, "es", "Spanish", enabled=False)
    response = await turn_on(captions_env, room, "es")
    assert response.status_code == 422


@pytest.mark.anyio
async def test_captions_need_a_linked_broadcast(captions_env):
    room = captions_env["rooms"][1]  # not linked
    response = await turn_on(captions_env, room)
    assert response.status_code == 409
    assert "broadcast" in response.json()["detail"]
    assert room.id not in yc.targets


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("broadcast", "message"),
    [
        (
            {"contentDetails": {"latencyPreference": "ultraLow", "closedCaptionsType": "closedCaptionsHttpPost"}},
            "Ultra-low",
        ),
        ({"status": {"lifeCycleStatus": "complete"}}, "has ended"),
        ({"status": {"lifeCycleStatus": "live"}}, "before the broadcast starts"),
    ],
)
async def test_broadcasts_that_cannot_carry_posted_captions_are_explained(captions_env, broadcast, message):
    room = captions_env["rooms"][0]
    item = next(b for b in captions_env["google"]["broadcasts"]["upcoming"] if b["id"] == UPCOMING_ID)
    for part, values in broadcast.items():
        item[part] = {**item[part], **values}
    response = await turn_on(captions_env, room)
    assert response.status_code == 409
    assert message in response.json()["detail"]
    assert puts(captions_env) == []
    assert room.id not in yc.targets
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
async def test_a_live_broadcast_already_posting_captions_is_accepted_without_an_update(captions_env):
    room = captions_env["rooms"][0]
    item = next(b for b in captions_env["google"]["broadcasts"]["upcoming"] if b["id"] == UPCOMING_ID)
    item["status"] = {"lifeCycleStatus": "live"}
    item["contentDetails"] = {**item["contentDetails"], "closedCaptionsType": "closedCaptionsHttpPost"}
    assert (await turn_on(captions_env, room)).status_code == 200
    assert puts(captions_env) == []
    assert yc.targets[room.id].language == "floor"


@pytest.mark.anyio
async def test_youtube_refusing_the_update_gives_the_studio_instructions(captions_env, caplog):
    room = captions_env["rooms"][0]
    captions_env["google"]["put_error"] = "invalidContentDetails"
    response = await turn_on(captions_env, room)
    assert response.status_code == 409
    assert "POST captions to URL" in response.json()["detail"]
    assert room.id not in yc.targets
    assert CAPTIONS_URL not in caplog.text and "SECRET" not in caplog.text


@pytest.mark.anyio
async def test_caption_changes_need_owner_access_and_a_csrf_token(captions_env):
    client, room = captions_env["client"], captions_env["rooms"][0]
    forged = await client.put(
        captions_url(captions_env, room), json={"language": "floor"}, headers={"X-CSRF-Token": "x"}
    )
    assert forged.status_code == 403
    client.cookies.set("user_token", captions_env["stranger_token"])
    assert (await turn_on(captions_env, room)).status_code in (403, 404)
    assert room.id not in yc.targets
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
async def test_status_shows_the_choice_and_its_languages_without_the_url(captions_env):
    client, room = captions_env["client"], captions_env["rooms"][0]
    youtube = (await client.get(status_url(captions_env, room))).json()["youtube"]
    assert youtube["captions"]["language"] is None
    assert youtube["captions"]["state"] == "off"
    assert [c["value"] for c in youtube["captions"]["choices"]] == ["floor", "fr"]
    await turn_on(captions_env, room)
    response = await client.get(status_url(captions_env, room))
    assert response.json()["youtube"]["captions"]["state"] == "waiting"
    assert "closedcaption" not in response.text and "SECRET" not in response.text


# ── Lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_unlinking_the_broadcast_stops_captions_and_clears_the_choice(captions_env):
    client, room = captions_env["client"], captions_env["rooms"][0]
    await turn_on(captions_env, room)
    assert (await client.delete(broadcast_url(captions_env, room))).status_code == 200
    assert room.id not in yc.targets
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
async def test_disconnecting_stops_every_room_and_leaves_other_events_alone(captions_env):
    client, room = captions_env["client"], captions_env["rooms"][0]
    await turn_on(captions_env, room)
    # A room of another event keeps posting.
    yc.targets[9999] = yc.CaptionTarget(language="floor", url="https://upload.youtube.com/closedcaption?cid=other")
    assert (await client.delete(event_url(captions_env))).status_code == 200
    assert room.id not in yc.targets
    assert 9999 in yc.targets
    assert (await stored_room(room.id)).youtube_captions_language is None


@pytest.mark.anyio
async def test_linking_another_broadcast_replaces_the_target(captions_env, monkeypatch):
    client, room = captions_env["client"], captions_env["rooms"][0]
    await turn_on(captions_env, room)
    stopped = []
    real_stop = sync.stop

    async def recording_stop(room_id, reason=""):
        stopped.append(room_id)
        await real_stop(room_id, reason)

    monkeypatch.setattr(sync, "stop", recording_stop)
    response = await client.put(broadcast_url(captions_env, room), json={"broadcast": LATER_ID})
    assert response.status_code == 200
    # The old target was torn down before the new broadcast was set up.
    assert stopped == [room.id]
    await sync.reconcile_room(room.id)
    assert sync._running[room.id].broadcast_id == LATER_ID
    assert yc.targets[room.id].language == "floor"


@pytest.mark.anyio
async def test_a_saved_choice_resumes_after_restart_without_a_page_or_a_caption(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    # Simulate a restart: the in-memory target and its URL are gone, the saved choice is not.
    await sync.shutdown()
    yc.health.clear()
    assert room.id not in yc.targets
    await sync.reconcile_once(now=1000.0)
    assert yc.targets[room.id].language == "floor"
    assert yc.targets[room.id].url == HTTPS_CAPTIONS_URL
    assert len(puts(captions_env)) == 1  # already set up at YouTube: no second update


@pytest.mark.anyio
async def test_reconcile_stops_a_room_whose_event_was_deleted(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    other = captions_env["rooms"][1]
    yc.targets[other.id] = yc.CaptionTarget(language="floor", url="https://upload.youtube.com/closedcaption?cid=x")
    async with get_session() as session:
        await session.execute(update(Room).where(Room.id == other.id).values(youtube_captions_language=None))
        event = await session.get(Event, captions_env["event"].id)
        await session.delete(event)
    await sync.reconcile_once(now=1000.0)
    assert room.id not in yc.targets
    assert other.id not in yc.targets


@pytest.mark.anyio
async def test_a_disabled_translation_stops_delivery_within_one_tick(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room, "fr")
    async with get_session() as session:
        await session.execute(update(RoomTranslationLanguage).values(enabled=False))
    await sync.reconcile_once(now=1000.0)
    assert room.id not in yc.targets
    assert yc.health[room.id].state == "failing"
    assert "not enabled" in yc.health[room.id].reason
    # The saved choice stays, so enabling the language again resumes delivery.
    async with get_session() as session:
        await session.execute(update(RoomTranslationLanguage).values(enabled=True))
    await sync.reconcile_once(now=1001.0)
    assert yc.targets[room.id].language == "fr"


@pytest.mark.anyio
async def test_revoked_access_is_noticed_at_the_next_verification(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    started = sync._running[room.id].verified_at
    yt._tokens.clear()
    captions_env["google"]["refresh_error"] = "invalid_grant"
    # Within the verification window nothing is asked of YouTube.
    calls = len(captions_env["google"]["calls"])
    await sync.reconcile_once(now=started + sync.VERIFY_INTERVAL_SECS - 1)
    assert len(captions_env["google"]["calls"]) == calls
    assert room.id in yc.targets
    await sync.reconcile_once(now=started + sync.VERIFY_INTERVAL_SECS)
    assert room.id not in yc.targets
    assert yc.health[room.id].reason == yt.REAUTH_MESSAGE
    # The connection now records the error, so later ticks do not keep trying.
    calls = len(captions_env["google"]["calls"])
    await sync.reconcile_once(now=started + 10 * sync.VERIFY_INTERVAL_SECS)
    assert len(captions_env["google"]["calls"]) == calls


@pytest.mark.anyio
async def test_an_ended_broadcast_stops_delivery_at_the_next_verification(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    item = next(b for b in captions_env["google"]["broadcasts"]["upcoming"] if b["id"] == UPCOMING_ID)
    item["status"] = {"lifeCycleStatus": "complete"}
    await sync.reconcile_once(now=sync._running[room.id].verified_at + sync.VERIFY_INTERVAL_SECS)
    assert room.id not in yc.targets
    assert yc.health[room.id].reason == yt.ENDED_MESSAGE


@pytest.mark.anyio
async def test_a_refused_start_is_not_retried_every_tick(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    await sync.shutdown()
    item = next(b for b in captions_env["google"]["broadcasts"]["upcoming"] if b["id"] == UPCOMING_ID)
    item["status"] = {"lifeCycleStatus": "complete"}
    await sync.reconcile_once(now=1000.0)
    assert yc.health[room.id].reason == yt.ENDED_MESSAGE
    calls = len(captions_env["google"]["calls"])
    await sync.reconcile_once(now=1000.0 + sync.RECONCILE_INTERVAL_SECS)
    assert len(captions_env["google"]["calls"]) == calls
    item["status"] = {"lifeCycleStatus": "ready"}
    await sync.reconcile_once(now=1000.0 + sync.RETRY_AFTER_REFUSAL_SECS)
    assert yc.targets[room.id].language == "floor"


@pytest.mark.anyio
async def test_shutdown_leaves_no_sender_running(captions_env):
    room = captions_env["rooms"][0]
    await turn_on(captions_env, room)
    yc.offer(room.id, "floor", "hello from the floor")
    task = yc._tasks[room.id]
    await sync.shutdown()
    assert task.done()
    assert yc.targets == {} and yc._tasks == {}


@pytest.mark.anyio
async def test_captions_reach_the_youtube_track_after_turning_on(captions_env, monkeypatch):
    room = captions_env["rooms"][0]
    posted = []

    def youtube(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(yc, "_transport", httpx.MockTransport(youtube))
    await turn_on(captions_env, room)
    yc.offer(room.id, "floor", "Welcome to the keynote")
    async with asyncio.timeout(2):
        while not posted or yc.health[room.id].state != "posting":
            await asyncio.sleep(0.01)
    assert posted[0].url.scheme == "https"
    assert posted[0].content.decode().endswith("Welcome to the keynote\n")


def test_offer_stays_synchronous():
    # The caption hot path never awaits YouTube work.
    assert not inspect.iscoroutinefunction(yc.offer)
