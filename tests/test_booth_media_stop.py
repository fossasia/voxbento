"""The portal ends a booth's WHIP session itself when access ends (#1285).

Revoking an interpreter closed their WebSocket and removed them from the booth
but left the audio they were sending running in MediaMTX until their own browser
tore it down. A frozen or sleeping tab never does.

These tests mock MediaMTX's control API and drive the real booth registry and
session handlers. Every authorised offer is recorded against its owner, so even
a pending offer and a publisher whose participant record is already gone are
ended, and a backup or a replacement publisher is left alone.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import portal.program_ingest as ingest
from portal.booth_publish import (
    authorize_booth_publish,
    clear_tickets,
    end_booth_publish,
    end_publishers_for,
    issue_ticket,
    publisher_for,
)
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


async def _join(booth_id, channel, name, *, user_id=None, invite_public_id=None, connection="sock"):
    participant, _ = await booths.join_participant(
        booth_id=booth_id,
        display_name=name,
        role=INTERPRETER,
        language=LANG,
        channel_id=channel,
        joined_by=f"user:{user_id}" if user_id else name,
        user_id=user_id,
        invite_public_id=invite_public_id,
        connection_id=f"{name}-{connection}",
    )
    return participant


def _session(booth_id, channel, participant, name, connection="sock") -> Session:
    return Session(
        booth_id=booth_id,
        participant_id=participant.participant_id,
        language=LANG,
        channel_id=channel,
        connection_id=f"{name}-{connection}",
    )


async def _start_publishing(booth_id, path, channel, participant, session_id) -> None:
    """Go through the same authorisation MediaMTX asks the portal for on a WHIP offer."""
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)
    secret = issue_ticket(booth_id, participant.participant_id, path)
    assert await authorize_booth_publish(path, secret, session_id)


def _kicked_ids(mediamtx) -> list[str]:
    return [call.args[1].id for call in mediamtx.kick.await_args_list]


def _recorded_ids(path: str) -> set[str]:
    from portal.booth_publish import _publishers

    return set(_publishers.get(path, {}))


def _kicked(mediamtx) -> list[tuple[str, str, str]]:
    """Each kick as (path, source type, session id) — hashable, so order-independent sets work."""
    return [(call.args[0], call.args[1].type, call.args[1].id) for call in mediamtx.kick.await_args_list]


# -- the authorised session is recorded against its owner --------------------------------------------


@pytest.mark.anyio
async def test_the_portal_records_the_authorised_session_and_its_owner():
    booth_id, path, channel = _booth("remember")
    ana = await _join(booth_id, channel, "Ana", user_id=5, invite_public_id="inv-5")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    owner = publisher_for(path)

    assert owner is not None
    assert (owner.participant_id, owner.user_id, owner.invite_public_id) == (ana.participant_id, 5, "inv-5")


@pytest.mark.anyio
async def test_authorize_publish_passes_the_mediamtx_session_id_through():
    booth_id, path, channel = _booth("passthrough")
    ana = await _join(booth_id, channel, "Ana")
    await booths.set_broadcast_unlocked(booth_id, True, LANG, channel)
    secret = issue_ticket(booth_id, ana.participant_id, path)

    await ingest.authorize_publish(
        MediaAuth(action="publish", path=path, protocol="webrtc", id=ANA_SESSION, token=secret, ip="203.0.113.9")
    )

    assert publisher_for(path).participant_id == ana.participant_id


# -- access ending ends the audio --------------------------------------------------------------------


@pytest.mark.anyio
async def test_revoking_the_publisher_kicks_the_session_they_started(mediamtx):
    booth_id, path, channel = _booth("revoke")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    assert _kicked(mediamtx) == [(path, "webRTCSession", ANA_SESSION)]


@pytest.mark.anyio
async def test_revoking_a_backup_leaves_the_active_interpreters_live_session_alone(mediamtx):
    booth_id, path, channel = _booth("backup")
    ana = await _join(booth_id, channel, "Ana")
    ben = await _join(booth_id, channel, "Ben")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    # Ana is the one publishing; revoking the backup must not end her session, by record or by path.
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=ANA_SESSION)
    )

    await release_participant(_session(booth_id, channel, ben, "Ben"), immediate=True)

    mediamtx.kick.assert_not_awaited()


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
    assert _kicked_ids(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_an_offer_that_never_became_the_live_source_is_still_ended(mediamtx):
    """MediaMTX gives a session its id before the publish hook; a pending offer is recorded and kicked."""
    booth_id, path, channel = _booth("pending")
    ana = await _join(booth_id, channel, "Ana")
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    # media_path still returns None: the offer passed the hook but never became the path source.

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    assert _kicked_ids(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_the_live_session_is_kept_under_the_per_path_cap_and_still_ended(mediamtx):
    """The per-path cap must never drop the live session: a later cluster of offers cannot evict it
    while it is the live source, so a revocation still finds and ends it (#1285, P1)."""
    import uuid

    from portal.booth_publish import MAX_SESSIONS_PER_PATH

    booth_id, path, channel = _booth("evicted")
    ana = await _join(booth_id, channel, "Ana", user_id=12)
    # MediaMTX reports Ana's session live throughout, so the prune can confirm and protect it.
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=ANA_SESSION)
    )
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)  # the live session, recorded first
    # A later cluster of offers that never replace the live one would, unbounded, evict it under the cap.
    for _ in range(MAX_SESSIONS_PER_PATH):
        secret = issue_ticket(booth_id, ana.participant_id, path)
        assert await authorize_booth_publish(path, secret, str(uuid.uuid4()))
    assert ANA_SESSION in _recorded_ids(path)  # the live session survives the prune

    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=True)

    assert ANA_SESSION in _kicked_ids(mediamtx)


@pytest.mark.anyio
async def test_revoking_one_owner_never_kicks_another_owners_unrecorded_live_session(mediamtx):
    """Ben published here, but Ana is the live source with no record of her own. Revoking Ben must not
    claim Ana's unrecorded live session as Ben's — she is still entitled (#1285, P1)."""
    booth_id, path, channel = _booth("crossowner")
    ben = await _join(booth_id, channel, "Ben", user_id=22)
    await _start_publishing(booth_id, path, channel, ben, BEN_SESSION)  # Ben's recorded offer
    # Ana is the live publisher on the path, with no record of her own (evicted, or predating a restart).
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=ANA_SESSION)
    )

    await end_publishers_for(user_id=22, booth_id=booth_id)

    assert ANA_SESSION not in _kicked_ids(mediamtx)  # a different, still-entitled publisher is left alone
    assert _kicked_ids(mediamtx) == [BEN_SESSION]


@pytest.mark.anyio
async def test_the_prune_keeps_every_record_when_the_live_lookup_fails(mediamtx):
    """If MediaMTX cannot be read while pruning, evicting blind could drop the live session, so the
    cap is left soft and every record is kept until the live publisher can be confirmed (#1285)."""
    import uuid

    from portal.booth_publish import MAX_SESSIONS_PER_PATH

    booth_id, path, channel = _booth("lookupfail")
    ana = await _join(booth_id, channel, "Ana", user_id=31)
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    mediamtx.lookup.side_effect = httpx.ConnectError("mediamtx unreachable")
    for _ in range(MAX_SESSIONS_PER_PATH):
        secret = issue_ticket(booth_id, ana.participant_id, path)
        assert await authorize_booth_publish(path, secret, str(uuid.uuid4()))

    assert ANA_SESSION in _recorded_ids(path)  # nothing is evicted while the live publisher is unknown
    assert len(_recorded_ids(path)) == MAX_SESSIONS_PER_PATH + 1


@pytest.mark.anyio
async def test_the_cap_still_evicts_an_old_non_live_offer_but_keeps_the_live_one(monkeypatch, mediamtx):
    """The cap is not disabled: an offer that is old and is not the live source is still evicted, while
    the live session and offers still inside their publish window are kept (#1285)."""
    import uuid

    import portal.booth_publish as bp
    from portal.booth_publish import MAX_SESSIONS_PER_PATH

    clock = {"t": 0.0}
    monkeypatch.setattr(bp, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    booth_id, path, channel = _booth("cap")
    ana = await _join(booth_id, channel, "Ana", user_id=41)
    # Fill the path to the cap with non-live offers recorded at t=0 (no overflow yet, so no prune).
    dead = [str(uuid.uuid4()) for _ in range(MAX_SESSIONS_PER_PATH)]
    await _start_publishing(booth_id, path, channel, ana, dead[0])
    for sid in dead[1:]:
        secret = issue_ticket(booth_id, ana.participant_id, path)
        assert await authorize_booth_publish(path, secret, sid)
    # Long after their publish window, a new live session overflows the cap; MediaMTX reports it live.
    clock["t"] = bp.TICKET_TTL_SECS + 100.0
    mediamtx.lookup.return_value = MediaPath(
        name=path, available=True, source=MediaSource(type="webRTCSession", id=ANA_SESSION)
    )
    secret = issue_ticket(booth_id, ana.participant_id, path)
    assert await authorize_booth_publish(path, secret, ANA_SESSION)

    recorded = _recorded_ids(path)
    assert ANA_SESSION in recorded  # the live session is kept
    assert dead[0] not in recorded  # the oldest, non-live offer was evicted
    assert len(recorded) == MAX_SESSIONS_PER_PATH  # back under the cap


@pytest.mark.anyio
async def test_a_publisher_whose_socket_had_already_dropped_is_kicked_when_their_access_is_removed(mediamtx):
    booth_id, path, channel = _booth("retained")
    ana = await _join(booth_id, channel, "Ana", user_id=7)
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)
    await booths.disconnect_participant(booth_id, ana.participant_id, "Ana-sock")
    mediamtx.kick.assert_not_awaited()  # a dropped socket alone keeps the audio up (#1280)

    await manager.close_user_sessions(7, booth_id=booth_id)

    assert _kicked_ids(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_a_revoked_invite_whose_socket_had_already_dropped_is_kicked(mediamtx):
    booth_id, path, channel = _booth("invite")
    participant = await _join(booth_id, channel, "Guest", invite_public_id="invite-9")
    await _start_publishing(booth_id, path, channel, participant, ANA_SESSION)
    await booths.disconnect_participant(booth_id, participant.participant_id, "Guest-sock")

    await manager.close_token_sessions("invite-9", booth_id=booth_id)

    assert _kicked_ids(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_a_publisher_removed_without_a_reconnect_grace_is_still_ended_by_identity(monkeypatch, mediamtx):
    """A grace-0 drop removes the participant at once, so a later revoke must find them by identity (#1285)."""
    monkeypatch.setattr(settings, "booth_reconnect_grace_seconds", 0.0)
    booth_id, path, channel = _booth("nograceid")
    ana = await _join(booth_id, channel, "Ana", user_id=8)
    await _start_publishing(booth_id, path, channel, ana, ANA_SESSION)

    # Ordinary disconnect with no grace: the participant is gone, but nothing is kicked yet (#1280).
    await release_participant(_session(booth_id, channel, ana, "Ana"), immediate=False)
    mediamtx.kick.assert_not_awaited()
    assert (await booths.get_booth(booth_id))["participants"] == []

    # The account is then deactivated: their recorded session is ended by user id.
    await manager.close_user_sessions(8, booth_id=booth_id)
    assert _kicked_ids(mediamtx) == [ANA_SESSION]


@pytest.mark.anyio
async def test_end_publishers_for_ends_a_users_sessions_across_every_booth(mediamtx):
    """Account deletion is not scoped to one booth, so it ends the user's audio everywhere."""
    sessions = {}
    for index, tag in enumerate(("acctA", "acctB")):
        booth_id, path, channel = _booth(tag)
        ana = await _join(booth_id, channel, "Ana", user_id=9)
        sid = f"9999999{index}-9999-4999-8999-999999999999"
        await _start_publishing(booth_id, path, channel, ana, sid)
        sessions[path] = sid

    await end_publishers_for(user_id=9)

    assert {path for path, _type, _sid in _kicked(mediamtx)} == set(sessions)
    assert set(_kicked_ids(mediamtx)) == set(sessions.values())


# -- the restart fallback, and its guard against kicking a replacement --------------------------------


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

    assert _kicked_ids(mediamtx) == [RESTART_SESSION]


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


@pytest.mark.anyio
async def test_the_fallback_does_not_kick_a_replacement_recorded_during_the_path_lookup(mediamtx):
    """If a backup is authorised while media_path is awaited, the live source is theirs — leave it (#1285)."""
    booth_id, path, channel = _booth("replace")
    ana = await _join(booth_id, channel, "Ana")  # active, but publishing predates this portal (no record)
    ben = await _join(booth_id, channel, "Ben")

    async def record_ben_then_answer(_path):
        # Stand in for a backup that authorises during the suspension of the media_path await.
        await booths.set_active_interpreter(booth_id, ana.participant_id, ben.participant_id, LANG, channel)
        await _start_publishing(booth_id, path, channel, ben, BEN_SESSION)
        return MediaPath(name=path, available=True, source=MediaSource(type="webRTCSession", id=BEN_SESSION))

    mediamtx.lookup.side_effect = record_ben_then_answer

    await end_booth_publish(booth_id, participant_id=ana.participant_id, was_active=True)

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


def _coordinator(booth_id, channel) -> tuple[SimpleNamespace, Session]:
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

    assert _kicked_ids(mediamtx) == [ANA_SESSION]


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
    expected = set()
    for index, code in enumerate(("en", "fr")):
        booth_id = f"{stored_booths.slug}-{stored_booths.room}-{code}"
        path = f"{stored_booths.slug}/{stored_booths.room}/{code}"
        channel = f"{booth_id}-audio"
        participant = await _join(booth_id, channel, f"Pat{code}")
        sid = f"4444444{index}-4444-4444-8444-444444444444"
        await _start_publishing(booth_id, path, channel, participant, sid)
        expected.add((path, "webRTCSession", sid))

    async with get_session() as session:
        await revoke_booths_for_deleted_entity(session, level=level, target_id=ids[level])

    assert set(_kicked(mediamtx)) == expected


@pytest.mark.anyio
async def test_deleting_a_booth_ends_only_that_booths_session(stored_booths, mediamtx):
    sessions = {}
    for index, code in enumerate(("en", "fr")):
        booth_id = f"{stored_booths.slug}-{stored_booths.room}-{code}"
        path = f"{stored_booths.slug}/{stored_booths.room}/{code}"
        channel = f"{booth_id}-audio"
        participant = await _join(booth_id, channel, f"Pat{code}")
        sid = f"5555555{index}-5555-4555-8555-555555555555"
        await _start_publishing(booth_id, path, channel, participant, sid)
        sessions[code] = (path, sid)

    async with get_session() as session:
        french = (await session.scalars(select(DBBooth).where(DBBooth.language_code == "fr"))).one()
        await revoke_booths_for_deleted_entity(session, level="booth", target_id=french.id)

    fr_path, fr_sid = sessions["fr"]
    assert _kicked(mediamtx) == [(fr_path, "webRTCSession", fr_sid)]


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
