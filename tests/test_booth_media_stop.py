"""The portal ends a booth's WHIP session itself when access ends (#1285).

Revoking an interpreter closed their WebSocket and removed them from the booth
but left the audio they were sending running in MediaMTX until their own browser
tore it down. A frozen or sleeping tab never does.

These tests mock MediaMTX's control API and drive the real booth registry and
session handlers.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import portal.program_ingest as ingest
from portal.booth_publish import authorize_booth_publish, clear_tickets, end_booth_publish, issue_ticket
from portal.config import settings
from portal.database import configure, dispose, get_session, init_db
from portal.globals import booths
from portal.models import DBBooth, Event, Room
from portal.program_ingest import MediaAuth, MediaPath, MediaSource
from portal.roles import INTERPRETER, ROOM_COORDINATOR
from portal.websockets.manager import Session, _handle_set_broadcast_unlocked, manager, release_participant
from portal.websockets.revocation import revoke_booths_for_deleted_entity

LANG = "English"
ANA_SESSION = "11111111-1111-4111-8111-111111111111"
BEN_SESSION = "22222222-2222-4222-8222-222222222222"
RESTART_SESSION = "33333333-3333-4333-8333-333333333333"

# The booth registry is process-wide, so every test that stores events needs a slug of its own.
_slugs = itertools.count(1)


@pytest.fixture(autouse=True)
def mediamtx(monkeypatch):
    """Stand in for MediaMTX's control API: nothing is published until a test says so."""
    kick = AsyncMock()
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest, "kick", kick)
    monkeypatch.setattr(ingest, "media_path", lookup)
    clear_tickets()
    yield SimpleNamespace(kick=kick, lookup=lookup)
    clear_tickets()


def _booth(tag: str) -> tuple[str, str, str]:
    """A booth ID, its MediaMTX path and its channel, unique to the test."""
    return f"{tag}-1-en", f"{tag}/1/en", f"{tag}-1-en-audio"


async def _join(booth_id: str, channel: str, name: str, *, user_id: int | None = None, connection: str = "sock"):
    participant, _ = await booths.join_participant(
        booth_id=booth_id,
        display_name=name,
        role=INTERPRETER,
        language=LANG,
        channel_id=channel,
        joined_by=f"user:{user_id}" if user_id else name,
        user_id=user_id,
        connection_id=f"{name}-{connection}",
    )
    return participant


def _session(booth_id: str, channel: str, participant, name: str, connection: str = "sock") -> Session:
    return Session(
        booth_id=booth_id,
        participant_id=participant.participant_id,
        language=LANG,
        channel_id=channel,
        connection_id=f"{name}-{connection}",
    )


async def _start_publishing(booth_id: str, path: str, channel: str, participant, session_id: str) -> None:
    """Go through the same authorisation MediaMTX asks the portal for on a WHIP offer."""
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)
    secret = issue_ticket(booth_id, participant.participant_id, path)
    assert await authorize_booth_publish(path, secret, session_id)


def _kicked(mediamtx) -> list[str]:
    return [call.args[1].id for call in mediamtx.kick.await_args_list]


# -- the session a ticket produces is remembered as the booth's publisher ----------------------------


@pytest.mark.anyio
async def test_the_portal_remembers_which_session_a_publish_ticket_started():
    from portal.booth_publish import publisher_for

    booth_id, path, channel = _booth("remember")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    publisher = publisher_for(path)

    assert publisher is not None
    assert (publisher.participant_id, publisher.session_id) == (ana.participant_id, ANA_SESSION)


@pytest.mark.anyio
async def test_authorize_publish_passes_the_mediamtx_session_id_through():
    from portal.booth_publish import publisher_for

    booth_id, path, channel = _booth("passthrough")
    ana = await _join(booth_id, channel, "Ana")
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)
    secret = issue_ticket(booth_id, ana.participant_id, path)

    await ingest.authorize_publish(
        MediaAuth(action="publish", path=path, protocol="webrtc", id=ANA_SESSION, token=secret, ip="203.0.113.9")
    )

    assert publisher_for(path).session_id == ANA_SESSION


# -- access ending ends the audio --------------------------------------------------------------------


@pytest.mark.anyio
async def test_revoking_the_publisher_kicks_the_session_they_started(mediamtx):
    booth_id, path, channel = _booth("revoke")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    mediamtx.kick.assert_awaited_once_with(path, MediaSource(type="webRTCSession", id=ANA_SESSION))


@pytest.mark.anyio
async def test_revoking_a_backup_who_is_not_publishing_touches_nothing(mediamtx):
    booth_id, path, channel = _booth("backup")
    ana = await _join(booth_id, channel, "Ana")
    ben = await _join(booth_id, channel, "Ben")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    await release_participant(_session(booth_id, channel, ben, "Ben"), immediate=True)

    mediamtx.kick.assert_not_awaited()
    mediamtx.lookup.assert_not_awaited()


@pytest.mark.anyio
async def test_the_revoked_publishers_session_is_kicked_even_though_a_backup_takes_over(mediamtx):
    """Removing the active interpreter promotes the next one, so "is someone entitled now?" proves nothing."""
    booth_id, path, channel = _booth("takeover")
    ana = await _join(booth_id, channel, "Ana")
    ben = await _join(booth_id, channel, "Ben")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    state = await booths.get_booth(booth_id)
    assert state["active_interpreter_id"] == ben.participant_id
    assert _kicked(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_a_publisher_whose_socket_had_already_dropped_is_kicked_when_their_access_is_removed(mediamtx):
    booth_id, path, channel = _booth("retained")
    ana = await _join(booth_id, channel, "Ana", user_id=7)
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    await booths.disconnect_participant(booth_id, ana.participant_id, "Ana-sock")
    mediamtx.kick.assert_not_awaited()  # a dropped socket alone keeps the audio up (#1280)

    await manager.close_user_sessions(7, booth_id=booth_id)

    assert _kicked(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_a_revoked_invite_whose_socket_had_already_dropped_is_kicked(mediamtx):
    booth_id, path, channel = _booth("invite")
    participant, _ = await booths.join_participant(
        booth_id=booth_id,
        display_name="Guest",
        role=INTERPRETER,
        language=LANG,
        channel_id=channel,
        joined_by="invite",
        invite_public_id="invite-9",
        connection_id="Guest-sock",
    )
    await _start_publishing(booth_id, path, channel, participant, ANA_SESSION)
    await booths.disconnect_participant(booth_id, participant.participant_id, "Guest-sock")

    await manager.close_token_sessions("invite-9", booth_id=booth_id)

    assert _kicked(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_after_a_restart_the_revoked_active_interpreters_session_is_found_on_the_path(mediamtx):
    """The publisher record lives in memory, so after a restart the path's own source is used instead."""
    booth_id, path, channel = _booth("restart")
    ana = await _join(booth_id, channel, "Ana")
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=RESTART_SESSION)
    )

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    assert _kicked(mediamtx) == [RESTART_SESSION]


@pytest.mark.anyio
async def test_after_a_restart_a_revoked_backup_still_touches_nothing(mediamtx):
    booth_id, path, channel = _booth("restartbackup")
    await _join(booth_id, channel, "Ana")
    ben = await _join(booth_id, channel, "Ben")
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=RESTART_SESSION)
    )

    await release_participant(_session(booth_id, channel, ben, "Ben"), immediate=True)

    mediamtx.kick.assert_not_awaited()


# -- an ordinary disconnect keeps the audio up (#1280) -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("grace", [30.0, 0.0], ids=["grace", "no-grace"])
async def test_an_ordinary_disconnect_leaves_the_audio_up(monkeypatch, mediamtx, grace):
    monkeypatch.setattr(settings, "booth_reconnect_grace_seconds", grace)
    booth_id, path, channel = _booth(f"drop{int(grace)}")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=False)

    mediamtx.kick.assert_not_awaited()
    mediamtx.lookup.assert_not_awaited()


# -- Mission Control --------------------------------------------------------------------------------


def _coordinator(booth_id: str, channel: str) -> tuple[SimpleNamespace, Session]:
    return (
        SimpleNamespace(send_text=AsyncMock()),
        Session(
            booth_id=booth_id, participant_id=None, language=LANG, channel_id=channel, granted_role=ROOM_COORDINATOR
        ),
    )


@pytest.mark.anyio
async def test_locking_the_booth_ends_the_publishers_session(mediamtx):
    booth_id, path, channel = _booth("lock")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    ws, coordinator = _coordinator(booth_id, channel)

    await _handle_set_broadcast_unlocked(ws, coordinator, {"unlocked": False})

    assert _kicked(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_unlocking_the_booth_touches_no_session(mediamtx):
    booth_id, path, channel = _booth("unlock")
    await _join(booth_id, channel, "Ana")
    ws, coordinator = _coordinator(booth_id, channel)

    await _handle_set_broadcast_unlocked(ws, coordinator, {"unlocked": True})

    mediamtx.kick.assert_not_awaited()
    mediamtx.lookup.assert_not_awaited()


# -- deleting an event, room or booth ----------------------------------------------------------------


@pytest.fixture
async def stored_booths():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = Event(slug=f"gone{next(_slugs)}", display_name="Gone")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Main")
        session.add(room)
        await session.flush()
        for code, name in (("en", "English"), ("fr", "French")):
            session.add(DBBooth(event_id=event.id, room_id=room.id, language_code=code, language_name=name))
        await session.commit()
        ids = SimpleNamespace(event=event.id, room=room.id, slug=event.slug)
    yield ids
    await dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["event", "room"])
async def test_deleting_an_event_or_room_ends_every_interpreter_booths_session(stored_booths, mediamtx, level):
    ids = {"event": stored_booths.event, "room": stored_booths.room}
    published = []
    for index, code in enumerate(("en", "fr")):
        booth_id, path = f"{stored_booths.slug}-{stored_booths.room}-{code}", f"{stored_booths.slug}/{stored_booths.room}/{code}"
        channel = f"{booth_id}-audio"
        participant = await _join(booth_id, channel, f"Pat{code}")
        await _start_publishing(booth_id, path, channel, participant, f"4444444{index}-4444-4444-8444-444444444444")
        published.append(path)

    async with get_session() as session:
        await revoke_booths_for_deleted_entity(session, level=level, target_id=ids[level])

    assert sorted(call.args[0] for call in mediamtx.kick.await_args_list) == sorted(published)


@pytest.mark.anyio
async def test_deleting_a_booth_ends_only_that_booths_session(stored_booths, mediamtx):
    published = {}
    for index, code in enumerate(("en", "fr")):
        booth_id, path = f"{stored_booths.slug}-{stored_booths.room}-{code}", f"{stored_booths.slug}/{stored_booths.room}/{code}"
        channel = f"{booth_id}-audio"
        participant = await _join(booth_id, channel, f"Pat{code}")
        await _start_publishing(booth_id, path, channel, participant, f"5555555{index}-5555-4555-8555-555555555555")
        published[code] = path

    async with get_session() as session:
        french = (await session.scalars(select(DBBooth).where(DBBooth.language_code == "fr"))).one()
        await revoke_booths_for_deleted_entity(session, level="booth", target_id=french.id)

    assert [call.args[0] for call in mediamtx.kick.await_args_list] == [published["fr"]]


# -- the floor is never touched ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_floor_booth_is_never_touched(mediamtx):
    """Floor ownership belongs to the Jitsi bot or program ingest, never to a booth revocation."""
    await end_booth_publish("conf-1-floor")
    await end_booth_publish("conf-1-floor", participant_id="anyone", was_active=True)

    mediamtx.kick.assert_not_awaited()
    mediamtx.lookup.assert_not_awaited()


@pytest.mark.anyio
async def test_a_malformed_booth_id_is_ignored(mediamtx):
    await end_booth_publish("not a booth")

    mediamtx.kick.assert_not_awaited()


# -- MediaMTX being unavailable never blocks a revocation ----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("media down"),
        httpx.ReadTimeout("slow"),
        httpx.HTTPStatusError("nope", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)),
    ],
    ids=["refused", "timeout", "server-error"],
)
async def test_a_failing_mediamtx_does_not_stop_the_revocation(mediamtx, failure):
    booth_id, path, channel = _booth(f"down{type(failure).__name__.lower()}")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    mediamtx.kick.side_effect = failure

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    state = await booths.get_booth(booth_id)
    assert state["participants"] == []


@pytest.mark.anyio
async def test_a_path_with_nothing_publishing_is_not_an_error(mediamtx):
    booth_id, path, channel = _booth("idle")
    ana = await _join(booth_id, channel, "Ana")
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    mediamtx.kick.assert_not_awaited()
