"""Booths survive a dropped socket (TD-11) and a portal restart (TD-03)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect

from portal import booth_recovery
from portal.booth_state import BoothRegistry
from portal.models import BoothLiveState

BOOTH = "pycon2026-1-en"
LANG = "English"
CHANNEL = f"{BOOTH}-audio"
ANA = "user:1"
BEN = "user:2"


async def join(registry, name, session_key, *, participant_id=None, connection_id=None, reclaim=False):
    participant, state = await registry.join_participant(
        booth_id=BOOTH,
        display_name=name,
        role="interpreter",
        language=LANG,
        channel_id=CHANNEL,
        participant_id=participant_id,
        joined_by=session_key,
        connection_id=connection_id or f"{name}-socket",
        reclaim=reclaim,
    )
    return participant, state


async def go_live(registry, participant):
    await registry.set_broadcast_unlocked(BOOTH, True, LANG, CHANNEL)
    await registry.update_participant_state(
        BOOTH, participant.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )


def person(state, participant_id):
    return next(p for p in state["participants"] if p["participant_id"] == participant_id)


# ── Reconnect grace (registry) ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_dropped_active_interpreter_keeps_slot_and_live_flags():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    ben, _ = await join(registry, "Ben", BEN)
    await go_live(registry, ana)

    state = await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket")

    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["connected"] is False
    assert state["ingest_status"] == "connected"
    assert state["broadcast_unlocked"] is True
    assert "connection_id" not in person(state, ana.participant_id)
    assert ben.participant_id != state["active_interpreter_id"]


@pytest.mark.anyio
async def test_same_session_reclaims_its_participant_on_reconnect():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    await join(registry, "Ben", BEN)
    await go_live(registry, ana)
    await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket")

    again, state = await join(registry, "Ana", ANA, participant_id=ana.participant_id, connection_id="Ana-socket-2")

    assert again.participant_id == ana.participant_id
    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["connected"] is True
    assert person(state, ana.participant_id)["ingest_connected"] is True
    assert len(state["participants"]) == 2


@pytest.mark.anyio
async def test_other_session_naming_a_participant_gets_a_fresh_one():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket")

    intruder, state = await join(registry, "Ben", BEN, participant_id=ana.participant_id)

    assert intruder.participant_id != ana.participant_id
    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["connected"] is False


@pytest.mark.anyio
async def test_participant_who_does_not_return_expires_and_hands_on_the_slot():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    ben, _ = await join(registry, "Ben", BEN)
    await go_live(registry, ana)
    await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket")

    state = await registry.expire_participant(BOOTH, ana.participant_id, "Ana-socket")

    assert [p["participant_id"] for p in state["participants"]] == [ben.participant_id]
    assert state["active_interpreter_id"] == ben.participant_id
    assert state["ingest_status"] == "disconnected"


@pytest.mark.anyio
async def test_old_socket_cannot_disconnect_expire_or_remove_a_reclaimed_participant():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket")
    await join(registry, "Ana", ANA, participant_id=ana.participant_id, connection_id="Ana-socket-2")

    assert await registry.expire_participant(BOOTH, ana.participant_id, "Ana-socket") is None
    assert await registry.disconnect_participant(BOOTH, ana.participant_id, "Ana-socket") is None
    state = await registry.leave_participant(BOOTH, ana.participant_id, LANG, CHANNEL, connection_id="Ana-socket")
    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["connected"] is True


@pytest.mark.anyio
async def test_disconnected_interpreter_is_not_handed_the_active_slot():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    ben, _ = await join(registry, "Ben", BEN)
    await registry.disconnect_participant(BOOTH, ben.participant_id, "Ben-socket")

    state = await registry.leave_participant(BOOTH, ana.participant_id, LANG, CHANNEL)
    assert state["active_interpreter_id"] is None

    # Ben takes the free slot when he comes back.
    _, state = await join(registry, "Ben", BEN, participant_id=ben.participant_id, connection_id="Ben-socket-2")
    assert state["active_interpreter_id"] == ben.participant_id


# ── Live record ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mic_request_lapses_when_its_requester_disconnects():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    ben, _ = await join(registry, "Ben", BEN)
    await go_live(registry, ana)
    await registry.initiate_handoff(BOOTH, ben.participant_id, LANG, CHANNEL)

    state = await registry.disconnect_participant(BOOTH, ben.participant_id, "Ben-socket")
    assert state["handoff_state"] == "idle"

    with pytest.raises(ValueError):
        await registry.accept_handoff(BOOTH, ana.participant_id, LANG, CHANNEL)
    state = await registry.get_booth(BOOTH)
    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["mic_active"] is True
    assert state["ingest_status"] == "connected"


@pytest.mark.anyio
async def test_mic_is_never_handed_to_a_disconnected_interpreter():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    ben, _ = await join(registry, "Ben", BEN)
    await go_live(registry, ana)
    await registry.initiate_handoff(BOOTH, ben.participant_id, LANG, CHANNEL)
    # A record that lost its connection some other way, e.g. a placeholder restored after a restart.
    registry.get_booth_sync(BOOTH).participants[ben.participant_id].connected = False

    with pytest.raises(ValueError, match="disconnected"):
        await registry.accept_handoff(BOOTH, ana.participant_id, LANG, CHANNEL)
    state = await registry.get_booth(BOOTH)
    assert state["active_interpreter_id"] == ana.participant_id
    assert person(state, ana.participant_id)["mic_active"] is True


@pytest.mark.anyio
async def test_revocation_removes_participants_whose_socket_had_dropped(monkeypatch):
    from portal.websockets import manager as manager_module

    registry = BoothRegistry()
    monkeypatch.setattr(manager_module, "booths", registry)
    broadcasts = []

    async def record(booth_id, state):
        broadcasts.append((booth_id, state))

    monkeypatch.setattr(manager_module, "broadcast_booth_state", record)
    ana, _ = await registry.join_participant(
        BOOTH, "Ana", "interpreter", LANG, CHANNEL, user_id=7, connection_id="Ana-socket"
    )
    ben, _ = await registry.join_participant(
        BOOTH, "Ben", "interpreter", LANG, CHANNEL, invite_public_id="inv-ben", connection_id="Ben-socket"
    )
    cleo, _ = await registry.join_participant(
        BOOTH, "Cleo", "interpreter", LANG, CHANNEL, user_id=8, connection_id="Cleo-socket"
    )
    await go_live(registry, ana)
    for participant, socket in ((ana, "Ana-socket"), (ben, "Ben-socket")):
        await registry.disconnect_participant(BOOTH, participant.participant_id, socket)

    connections = manager_module.ConnectionManager()
    await connections.close_user_sessions(7)
    await connections.close_token_sessions("inv-ben", booth_id=BOOTH)

    state = await registry.get_booth(BOOTH)
    assert [p["participant_id"] for p in state["participants"]] == [cleo.participant_id]
    # The revoked active interpreter's slot passes on at once, not after the grace period.
    assert state["active_interpreter_id"] == cleo.participant_id
    assert len(broadcasts) == 2
    # A connected participant is left to the socket closing path.
    assert await registry.remove_disconnected(user_id=8) == []


@pytest.mark.anyio
async def test_live_record_holds_go_live_and_the_active_interpreter():
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    booth = registry.get_booth_sync(BOOTH)
    assert booth.live_record()["active_session_key"] == ANA

    await go_live(registry, ana)
    record = booth.live_record()
    assert record["broadcast_unlocked"] is True
    assert record["active_participant_id"] == ana.participant_id
    assert record["active_mic_active"] is True
    assert record["active_ingest_connected"] is True

    await registry.leave_participant(BOOTH, ana.participant_id, LANG, CHANNEL)
    await registry.set_broadcast_unlocked(BOOTH, False, LANG, CHANNEL)
    assert booth.live_record() is None


@pytest.mark.anyio
async def test_live_record_leaves_out_an_active_interpreter_nobody_could_reclaim():
    registry = BoothRegistry()
    await join(registry, "Ana", None)
    await registry.set_broadcast_unlocked(BOOTH, True, LANG, CHANNEL)

    record = registry.get_booth_sync(BOOTH).live_record()

    assert record["broadcast_unlocked"] is True
    assert record["active_participant_id"] is None


# ── Restore ──────────────────────────────────────────────────────────────────


async def _live_record_of_running_booth() -> tuple[dict, str]:
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    await go_live(registry, ana)
    return registry.get_booth_sync(BOOTH).live_record(), ana.participant_id


@pytest.mark.anyio
async def test_restore_brings_back_go_live_and_holds_the_slot_for_the_same_session():
    record, ana_id = await _live_record_of_running_booth()
    registry = BoothRegistry()

    placeholders = await registry.restore([record])

    assert placeholders == [(BOOTH, ana_id)]
    state = await registry.get_booth(BOOTH)
    assert state["broadcast_unlocked"] is True
    assert state["active_interpreter_id"] == ana_id
    assert state["ingest_status"] == "connected"
    assert person(state, ana_id)["connected"] is False

    # Another interpreter reconnecting first stays passive.
    ben, state = await join(registry, "Ben", BEN, participant_id="whatever")
    assert state["active_interpreter_id"] == ana_id

    again, state = await join(registry, "Ana", ANA, participant_id=ana_id, connection_id="new-socket")
    assert again.participant_id == ana_id
    assert state["active_interpreter_id"] == ana_id
    assert person(state, ana_id)["connected"] is True
    assert ben.participant_id != ana_id


@pytest.mark.anyio
async def test_restored_interpreter_who_does_not_return_expires():
    record, ana_id = await _live_record_of_running_booth()
    registry = BoothRegistry()
    await registry.restore([record])
    ben, _ = await join(registry, "Ben", BEN)

    state = await registry.expire_participant(BOOTH, ana_id, None)

    assert state["active_interpreter_id"] == ben.participant_id
    assert state["broadcast_unlocked"] is True


# ── Persistence ──────────────────────────────────────────────────────────────


@pytest.fixture
def database(tmp_path):
    import anyio

    from portal.database import configure, dispose, init_db

    configure(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    anyio.run(init_db)
    yield
    anyio.run(dispose)


async def _rows() -> list[BoothLiveState]:
    from portal.database import get_session

    async with get_session() as session:
        return list((await session.execute(select(BoothLiveState))).scalars())


@pytest.mark.anyio
async def test_saved_state_round_trips_through_the_database(database):
    registry = BoothRegistry()
    ana, _ = await join(registry, "Ana", ANA)
    await go_live(registry, ana)

    assert await booth_recovery._Saver(registry).save()
    [row] = await _rows()
    assert row.booth_id == BOOTH
    assert row.active_session_key == ANA

    restarted = BoothRegistry()
    placeholders = await booth_recovery.restore(restarted, max_age_seconds=600)
    assert placeholders == [(BOOTH, ana.participant_id)]
    assert (await restarted.get_booth(BOOTH))["broadcast_unlocked"] is True


@pytest.mark.anyio
async def test_booth_with_nothing_live_has_its_row_deleted(database):
    registry = BoothRegistry()
    saver = booth_recovery._Saver(registry)
    ana, _ = await join(registry, "Ana", ANA)
    await go_live(registry, ana)
    await saver.save()

    await registry.leave_participant(BOOTH, ana.participant_id, LANG, CHANNEL)
    await registry.set_broadcast_unlocked(BOOTH, False, LANG, CHANNEL)
    await saver.save()

    assert await _rows() == []


@pytest.mark.anyio
async def test_state_older_than_the_limit_is_not_restored(database):
    record, _ = await _live_record_of_running_booth()
    long_ago = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    await booth_recovery.save_changes({BOOTH: record}, long_ago)

    registry = BoothRegistry()
    assert await booth_recovery.restore(registry, max_age_seconds=600) == []
    assert await registry.get_booth(BOOTH) is None
    assert await _rows() == []


@pytest.mark.anyio
async def test_failed_save_is_retried(database, monkeypatch):
    registry = BoothRegistry()
    saver = booth_recovery._Saver(registry)
    ana, _ = await join(registry, "Ana", ANA)

    async def broken(changes, now):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(booth_recovery, "save_changes", broken)
    assert await saver.save() is False
    monkeypatch.undo()

    assert await saver.save() is True
    [row] = await _rows()
    assert row.active_participant_id == ana.participant_id


@pytest.mark.anyio
async def test_restore_without_a_database_starts_empty(monkeypatch):
    async def broken(max_age_seconds, now):
        raise OperationalError("SELECT", {}, Exception("no such table"))

    monkeypatch.setattr(booth_recovery, "load_records", broken)
    registry = BoothRegistry()
    assert await booth_recovery.restore(registry, max_age_seconds=600) == []


@pytest.mark.anyio
async def test_run_saves_changes_and_saves_again_at_shutdown(database, monkeypatch):
    monkeypatch.setattr(booth_recovery, "DEBOUNCE_SECS", 0)
    registry = BoothRegistry()
    task = asyncio.create_task(booth_recovery.run(registry))
    ana, _ = await join(registry, "Ana", ANA)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if await _rows():
            break
    assert [row.active_participant_id for row in await _rows()] == [ana.participant_id]

    await registry.set_broadcast_unlocked(BOOTH, True, LANG, CHANNEL)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    [row] = await _rows()
    assert row.broadcast_unlocked is True


@pytest.mark.anyio
async def test_save_cancelled_by_shutdown_is_written_by_the_final_save(database, monkeypatch):
    monkeypatch.setattr(booth_recovery, "DEBOUNCE_SECS", 0)
    real_save = booth_recovery.save_changes
    suspended = asyncio.Event()
    calls = 0

    async def slow_first_save(changes, now):
        nonlocal calls
        calls += 1
        if calls == 1:
            suspended.set()
            await asyncio.sleep(3600)
        await real_save(changes, now)

    monkeypatch.setattr(booth_recovery, "save_changes", slow_first_save)
    registry = BoothRegistry()
    task = asyncio.create_task(booth_recovery.run(registry))
    ana, _ = await join(registry, "Ana", ANA)
    await asyncio.wait_for(suspended.wait(), 2)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [row.active_participant_id for row in await _rows()] == [ana.participant_id]


# ── WebSocket ────────────────────────────────────────────────────────────────


@pytest.fixture
def ws_client(database, monkeypatch):
    from fastapi_app import app
    from portal.config import settings

    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "booth_reconnect_grace_seconds", 30.0)
    return TestClient(app)


def _cookie(event_slug: str) -> dict:
    """An interpreter invite session; each one is a different session key."""
    from portal.auth import create_participant_token

    return {
        "session_token": create_participant_token(
            booth_id=1, role="interpreter", event_slug=event_slug, room_id=1, language_code="en"
        )
    }


def _booth(client, event_slug: str) -> str:
    # One event per test: the booth registry is shared by the whole run.
    res = client.post(
        f"/api/events/{event_slug}/booths",
        json={"language_code": "en", "room_id": 1, "language": "English"},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["booth_id"]


def _join(ws, participant_id=None) -> tuple[str, dict]:
    ws.send_text(
        json.dumps(
            {"type": "booth:join", "display_name": "Ana", "role": "interpreter", "language": "English",
             "participant_id": participant_id}
        )
    )
    while True:
        message = json.loads(ws.receive_text())
        if message["type"] == "booth:joined":
            return message["participant_id"], message["state"]


def test_reconnecting_socket_gets_its_place_back(ws_client):
    booth_id = _booth(ws_client, "reconnect")
    ana = _cookie("reconnect")
    with ws_client.websocket_connect(f"/ws/booth/{booth_id}", cookies=ana) as ws:
        ana_id, state = _join(ws)
        assert state["active_interpreter_id"] == ana_id

    with ws_client.websocket_connect(f"/ws/booth/{booth_id}", cookies=_cookie("reconnect")) as ws:
        _, state = _join(ws)
        assert state["active_interpreter_id"] == ana_id
        assert person(state, ana_id)["connected"] is False

    with ws_client.websocket_connect(f"/ws/booth/{booth_id}", cookies=ana) as ws:
        again, state = _join(ws, ana_id)
        assert again == ana_id
        assert state["active_interpreter_id"] == ana_id
        assert person(state, ana_id)["connected"] is True


def test_reclaiming_from_a_live_socket_closes_the_old_one_for_good(ws_client):
    booth_id = _booth(ws_client, "supersede")
    ana = _cookie("supersede")
    with ws_client.websocket_connect(f"/ws/booth/{booth_id}", cookies=ana) as old:
        ana_id, _ = _join(old)
        with ws_client.websocket_connect(f"/ws/booth/{booth_id}", cookies=ana) as new:
            again, state = _join(new, ana_id)
            assert again == ana_id
            assert len(state["participants"]) == 1
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    old.receive_text()
            assert closed.value.code == 4009


def test_revoked_participant_is_removed_without_waiting(ws_client):
    import anyio

    from portal.globals import booths
    from portal.websockets.manager import Session, release_participant

    async def scenario():
        participant, _ = await booths.join_participant(
            booth_id="revoked-1-en", display_name="Ana", role="interpreter", language=LANG,
            channel_id="c", joined_by=ANA, connection_id="sock",
        )
        session = Session(
            booth_id="revoked-1-en", participant_id=participant.participant_id, language=LANG, channel_id="c",
            connection_id="sock",
        )
        await release_participant(session, immediate=True)
        return await booths.get_booth("revoked-1-en")

    state = anyio.run(scenario)
    assert state["participants"] == []
