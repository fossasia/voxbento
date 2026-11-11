"""The active interpreter survives a portal restart (#1165)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from portal import booth_store
from portal.booth_identity import make_booth_id
from portal.booth_state import BoothRegistry
from portal.database import get_session
from portal.models import DBBooth, Event, Room, utc_now

BOOTH = "hall-1-fr"


class FakeStore:
    """Stands in for portal.booth_store: records what the registry asks to save."""

    def __init__(self) -> None:
        self.saved: dict[str, str | None] = {}
        self.calls: list[tuple[str, str | None]] = []

    async def save(self, booth_id: str, session_key: str | None) -> None:
        self.calls.append((booth_id, session_key))
        self.saved[booth_id] = session_key


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def join(registry, name, session_key, role="interpreter", participant_id=None):
    participant, _ = await registry.join_participant(
        booth_id=BOOTH,
        display_name=name,
        role=role,
        language="French",
        channel_id=f"{BOOTH}-audio",
        participant_id=participant_id,
        joined_by=session_key,
    )
    return participant


async def settle(registry) -> None:
    """Let every pending database write run."""
    pending = list(registry._pending_persist.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def active_of(registry) -> str | None:
    return registry.get_booth_sync(BOOTH).active_interpreter_id


def restarted(store: FakeStore, clock: Clock, previous_session_key: str | None = "user:1") -> BoothRegistry:
    """A new registry, as after a portal restart, told who was active before it."""
    registry = BoothRegistry(persist_active=store.save, clock=clock)
    if previous_session_key:
        registry.restore_active([(BOOTH, previous_session_key)])
    return registry


# ── storing the active interpreter ───────────────────────────────────────


@pytest.mark.anyio
async def test_deliberate_changes_are_stored_with_the_session_key():
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save)

    first = await join(registry, "A", "user:1")
    await settle(registry)
    assert store.saved[BOOTH] == "user:1"

    second = await join(registry, "B", "user:2")
    await settle(registry)
    assert store.saved[BOOTH] == "user:1"  # B joined as a passive interpreter

    await registry.set_active_interpreter(BOOTH, first.participant_id, second.participant_id, "French", "ch")
    await settle(registry)
    assert store.saved[BOOTH] == "user:2"


@pytest.mark.anyio
async def test_a_handoff_is_stored():
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save)
    first = await join(registry, "A", "user:1")
    second = await join(registry, "B", "user:2")
    await registry.initiate_handoff(BOOTH, first.participant_id, "French", "ch")
    await registry.accept_handoff(BOOTH, second.participant_id, "French", "ch")
    await settle(registry)

    assert store.saved[BOOTH] == "user:2"


@pytest.mark.anyio
async def test_the_participant_id_is_never_stored():
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save)
    participant = await join(registry, "A", "user:1")
    await settle(registry)

    assert participant.participant_id not in store.saved.values()


@pytest.mark.anyio
async def test_someone_leaving_is_stored_only_after_a_delay():
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save, leave_persist_delay=0.05)
    first = await join(registry, "A", "user:1")
    await join(registry, "B", "user:2")
    await settle(registry)

    await registry.leave_participant(BOOTH, first.participant_id, "French", "ch")
    assert store.saved[BOOTH] == "user:1"  # not yet
    await settle(registry)
    assert store.saved[BOOTH] == "user:2"


@pytest.mark.anyio
async def test_a_shutdown_does_not_overwrite_who_was_active():
    """On shutdown every socket closes and each participant leaves, before the process ends."""
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save, leave_persist_delay=30)
    first = await join(registry, "A", "user:1")
    second = await join(registry, "B", "user:2")
    await settle(registry)

    await registry.leave_participant(BOOTH, first.participant_id, "French", "ch")
    await registry.leave_participant(BOOTH, second.participant_id, "French", "ch")
    await asyncio.sleep(0.05)  # shutting down takes a moment, far less than the delay
    for task in registry._pending_persist.values():
        task.cancel()  # the process ends before the delay does
    await asyncio.sleep(0)

    assert store.saved[BOOTH] == "user:1"


@pytest.mark.anyio
async def test_without_a_store_nothing_is_scheduled():
    registry = BoothRegistry()
    await join(registry, "A", "user:1")

    assert registry._pending_persist == {}


@pytest.mark.anyio
async def test_a_failing_store_does_not_break_the_booth():
    async def broken(booth_id, session_key):
        raise RuntimeError("database is down")

    registry = BoothRegistry(persist_active=broken)
    participant = await join(registry, "A", "user:1")
    await settle(registry)

    assert active_of(registry) == participant.participant_id


# ── after a restart ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_same_person_gets_the_active_slot_back():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    returning = await join(registry, "A", "user:1")

    assert active_of(registry) == returning.participant_id
    assert registry._reservations == {}


@pytest.mark.anyio
async def test_somebody_else_joining_first_does_not_take_the_slot():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    other = await join(registry, "B", "user:2")
    assert active_of(registry) is None

    returning = await join(registry, "A", "user:1")
    assert active_of(registry) == returning.participant_id
    assert active_of(registry) != other.participant_id


@pytest.mark.anyio
async def test_the_slot_cannot_be_claimed_by_reusing_a_participant_id():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    claimant = await join(registry, "B", "user:2", participant_id="the-old-participant-id")
    anonymous = await join(registry, "C", None)

    assert active_of(registry) not in (claimant.participant_id, anonymous.participant_id)


@pytest.mark.anyio
async def test_a_role_that_cannot_go_live_cannot_take_the_slot_back():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    observer = await join(registry, "A", "user:1", role="viewer")

    assert active_of(registry) != observer.participant_id


@pytest.mark.anyio
async def test_when_nobody_returns_the_first_interpreter_who_joined_takes_the_slot():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)
    other = await join(registry, "B", "user:2")

    clock.now += 119
    assert await registry.expire_reservations() == []
    assert active_of(registry) is None

    clock.now += 2
    changed = await registry.expire_reservations()
    await settle(registry)

    assert [state["booth_id"] for state in changed] == [BOOTH]
    assert active_of(registry) == other.participant_id
    assert store.saved[BOOTH] == "user:2"
    assert registry._reservations == {}


@pytest.mark.anyio
async def test_when_nobody_joined_at_all_the_stored_interpreter_is_cleared():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    clock.now += 200
    assert await registry.expire_reservations() == []
    await settle(registry)

    assert store.saved[BOOTH] is None


@pytest.mark.anyio
async def test_a_lapsed_reservation_no_longer_blocks_a_join():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)

    clock.now += 500  # the reaper has not ticked yet
    other = await join(registry, "B", "user:2")

    assert active_of(registry) == other.participant_id


@pytest.mark.anyio
async def test_a_coordinator_choosing_someone_ends_the_reservation():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)
    other = await join(registry, "B", "user:2")
    coordinator = await join(registry, "C", "user:3", role="room_coordinator")

    await registry.set_active_interpreter(BOOTH, coordinator.participant_id, other.participant_id, "French", "ch")
    returning = await join(registry, "A", "user:1")

    assert active_of(registry) == other.participant_id
    assert active_of(registry) != returning.participant_id
    assert registry._reservations == {}


@pytest.mark.anyio
async def test_publishing_still_needs_the_active_slot():
    store, clock = FakeStore(), Clock()
    registry = restarted(store, clock)
    other = await join(registry, "B", "user:2")

    with pytest.raises(PermissionError):
        await registry.check_publish_permission(BOOTH, other.participant_id, "French", "ch")

    returning = await join(registry, "A", "user:1")
    await registry.check_publish_permission(BOOTH, returning.participant_id, "French", "ch")


@pytest.mark.anyio
async def test_removing_a_booth_drops_its_persist_lock():
    store = FakeStore()
    registry = BoothRegistry(persist_active=store.save)
    assert make_booth_id("hall", 1, "fr") == BOOTH
    await join(registry, "A", "user:1")
    await settle(registry)
    assert BOOTH in registry._persist_locks

    await registry.remove_booth("hall", 1, "fr")

    assert BOOTH not in registry._persist_locks


# ── the database side ────────────────────────────────────────────────────


@pytest.fixture
async def booth_in_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = Event(slug="persist-event", display_name="Persist Event")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Main Hall")
        session.add(room)
        await session.flush()
        booth = DBBooth(event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        session.add(booth)
        await session.flush()
        booth_id = make_booth_id("persist-event", room.id, "fr")
        booth_pk = booth.id
    yield booth_id, booth_pk
    await dispose()


@pytest.mark.anyio
async def test_save_and_load_round_trip(booth_in_db):
    booth_id, _ = booth_in_db

    await booth_store.save_active(booth_id, "user:5")
    assert await booth_store.load_active() == [(booth_id, "user:5")]

    await booth_store.save_active(booth_id, None)
    assert await booth_store.load_active() == []


@pytest.mark.anyio
async def test_saving_for_a_booth_without_a_row_does_nothing(booth_in_db):
    await booth_store.save_active("persist-event-999-fr", "user:5")
    await booth_store.save_active("persist-event-1-floor", "user:5")
    await booth_store.save_active("not a booth id", "user:5")

    assert await booth_store.load_active() == []


@pytest.mark.anyio
async def test_an_old_entry_is_forgotten_and_cleared(booth_in_db):
    booth_id, booth_pk = booth_in_db
    await booth_store.save_active(booth_id, "user:5")
    async with get_session() as session:
        booth = (await session.execute(select(DBBooth).where(DBBooth.id == booth_pk))).scalar_one()
        booth.active_interpreter_since = utc_now() - booth_store.MAX_AGE - timedelta(minutes=1)

    assert await booth_store.load_active() == []
    async with get_session() as session:
        booth = (await session.execute(select(DBBooth).where(DBBooth.id == booth_pk))).scalar_one()
        assert booth.active_interpreter_session_key is None


@pytest.mark.anyio
async def test_a_deleted_event_does_not_restore_a_reservation(booth_in_db):
    booth_id, _ = booth_in_db
    await booth_store.save_active(booth_id, "user:5")
    async with get_session() as session:
        event = (await session.execute(select(Event).where(Event.slug == "persist-event"))).scalar_one()
        event.deleted_at = utc_now()

    assert await booth_store.load_active() == []


@pytest.mark.anyio
async def test_a_booth_whose_room_belongs_to_another_event_is_not_restored(booth_in_db):
    booth_id, booth_pk = booth_in_db
    await booth_store.save_active(booth_id, "user:5")
    async with get_session() as session:
        other = Event(slug="other-event", display_name="Other Event")
        session.add(other)
        await session.flush()
        booth = (await session.execute(select(DBBooth).where(DBBooth.id == booth_pk))).scalar_one()
        booth.event_id = other.id  # the room still belongs to persist-event

    assert await booth_store.load_active() == []


@pytest.mark.anyio
async def test_a_failing_cleanup_keeps_the_valid_reservations(booth_in_db, monkeypatch):
    booth_id, booth_pk = booth_in_db
    await booth_store.save_active(booth_id, "user:5")
    async with get_session() as session:
        booth = (await session.execute(select(DBBooth).where(DBBooth.id == booth_pk))).scalar_one()
        old = DBBooth(
            event_id=booth.event_id,
            room_id=booth.room_id,
            language_code="de",
            language_name="German",
            active_interpreter_session_key="user:9",
            active_interpreter_since=utc_now() - booth_store.MAX_AGE - timedelta(minutes=1),
        )
        session.add(old)

    real_get_session = booth_store.get_session
    calls = {"count": 0}

    def second_session_fails():
        calls["count"] += 1
        if calls["count"] == 2:  # the cleanup of the old entry
            raise OperationalError("update", {}, Exception("database is locked"))
        return real_get_session()

    monkeypatch.setattr(booth_store, "get_session", second_session_fails)

    assert await booth_store.load_active() == [(booth_id, "user:5")]
    assert calls["count"] == 2


@pytest.mark.anyio
async def test_a_database_error_is_swallowed(booth_in_db, monkeypatch):
    booth_id, _ = booth_in_db

    def broken():
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(booth_store, "get_session", broken)

    await booth_store.save_active(booth_id, "user:5")
    assert await booth_store.load_active() == []


@pytest.mark.anyio
async def test_the_store_feeds_the_registry_across_a_restart(booth_in_db):
    booth_id, _ = booth_in_db
    first_process = BoothRegistry(persist_active=booth_store.save_active)
    participant, _ = await first_process.join_participant(
        booth_id=booth_id,
        display_name="A",
        role="interpreter",
        language="French",
        channel_id="ch",
        joined_by="user:7",
    )
    await settle(first_process)
    assert participant.participant_id

    second_process = BoothRegistry(persist_active=booth_store.save_active)
    second_process.restore_active(await booth_store.load_active())
    other, _ = await second_process.join_participant(
        booth_id=booth_id, display_name="B", role="interpreter", language="French", channel_id="ch", joined_by="user:8"
    )
    returning, _ = await second_process.join_participant(
        booth_id=booth_id, display_name="A", role="interpreter", language="French", channel_id="ch", joined_by="user:7"
    )

    assert second_process.get_booth_sync(booth_id).active_interpreter_id == returning.participant_id
    assert other.participant_id != returning.participant_id


# ── wiring ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_reaper_announces_a_booth_that_got_its_active_interpreter(monkeypatch):
    from portal.websockets import reaper

    state = {"booth_id": BOOTH, "active_interpreter_id": "p1"}
    sent: list[tuple[str, dict]] = []
    answers = [[state], []]

    async def no_sockets_to_reap():
        return 0

    async def expire():
        return answers.pop(0) if answers else []

    async def broadcast(booth_id, payload):
        sent.append((booth_id, payload))

    monkeypatch.setattr(reaper, "reap_stale_sessions", no_sockets_to_reap)
    monkeypatch.setattr(reaper.booths, "expire_reservations", expire)
    monkeypatch.setattr(reaper, "broadcast_booth_state", broadcast)

    task = asyncio.create_task(reaper.session_reaper_loop(0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert sent == [(BOOTH, state)]


@pytest.mark.anyio
async def test_the_reaper_still_expires_reservations_when_reaping_sessions_fails(monkeypatch):
    from portal.websockets import reaper

    state = {"booth_id": BOOTH, "active_interpreter_id": "p1"}
    sent: list[tuple[str, dict]] = []
    answers = [[state], []]

    async def failing_reap():
        raise RuntimeError("database is locked")

    async def expire():
        return answers.pop(0) if answers else []

    async def broadcast(booth_id, payload):
        sent.append((booth_id, payload))

    monkeypatch.setattr(reaper, "reap_stale_sessions", failing_reap)
    monkeypatch.setattr(reaper.booths, "expire_reservations", expire)
    monkeypatch.setattr(reaper, "broadcast_booth_state", broadcast)

    task = asyncio.create_task(reaper.session_reaper_loop(0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert sent == [(BOOTH, state)]


@pytest.mark.anyio
async def test_enabling_persistence_stores_changes_and_reserves_the_last_active_slot(monkeypatch):
    fresh = BoothRegistry()

    async def stored():
        return [(BOOTH, "user:1")]

    monkeypatch.setattr(booth_store, "load_active", stored)

    await booth_store.enable_persistence(fresh)

    assert fresh._persist_active is booth_store.save_active
    assert fresh._reservations[BOOTH][0] == "user:1"
