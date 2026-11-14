"""Interpreter attendance and interpreting time (#1215)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal import globals as portal_globals
from portal.attendance import (
    AttendanceRow,
    AttendanceTracker,
    close_stale_visits,
    format_duration,
    summarize,
)
from portal.auth import create_user_token
from portal.booth_state import BoothRegistry, participant_public_dict
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import InterpreterAttendance
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR
from portal.workspace_routing import workspace_to_admin_path

os.environ["BOOTH_ACCESS_TOKEN"] = ""

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
BOOTH = "conf-1-de"
LANG = "German"
CHANNEL = "conf-1-de-audio"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(clock):
    return BoothRegistry(attendance=AttendanceTracker(clock=clock))


async def join(registry, name, *, user_id=None, invite=None, role=INTERPRETER):
    participant, _ = await registry.join_participant(
        booth_id=BOOTH,
        display_name=name,
        role=role,
        language=LANG,
        channel_id=CHANNEL,
        user_id=user_id,
        invite_public_id=invite,
    )
    return participant


async def set_state(registry, participant, **flags):
    return await registry.update_participant_state(BOOTH, participant.participant_id, LANG, CHANNEL, **flags)


def only_visit(registry, participant):
    visits = [v for v in registry.attendance._open.values() if v.participant_id == participant.participant_id]
    assert len(visits) == 1
    return visits[0]


# ── Tracking on the live path ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_join_and_leave_are_recorded(registry, clock):
    alice = await join(registry, "Alice", user_id=7)
    visit = only_visit(registry, alice)
    assert visit.joined_at == T0
    assert (visit.user_id, visit.event_slug, visit.room_id, visit.language_code) == (7, "conf", 1, "de")

    clock.advance(600)
    await registry.leave_participant(BOOTH, alice.participant_id, LANG, CHANNEL)

    assert registry.attendance.open_visits("conf") == []
    [closed] = registry.attendance._closed
    assert closed.left_at == T0 + timedelta(seconds=600)
    assert closed.interpreting_seconds == 0


@pytest.mark.anyio
async def test_interpreting_needs_active_connected_and_unmuted(registry, clock):
    alice = await join(registry, "Alice")
    visit = only_visit(registry, alice)

    await set_state(registry, alice, ingest_connected=True)  # connected, microphone still muted
    clock.advance(30)
    assert visit.interpreting_total(clock()) == 0

    await set_state(registry, alice, mic_active=True)
    clock.advance(120)
    assert visit.interpreting_total(clock()) == 120

    # A device mute (#1214) reaches the server as mic_active false and stops the count.
    await set_state(registry, alice, mic_active=False)
    clock.advance(60)
    assert visit.interpreting_total(clock()) == 120

    await set_state(registry, alice, mic_active=True)
    clock.advance(30)
    await set_state(registry, alice, ingest_connected=False)
    clock.advance(30)
    assert visit.interpreting_total(clock()) == 150


@pytest.mark.anyio
async def test_backup_interpreter_is_present_but_not_interpreting(registry, clock):
    alice = await join(registry, "Alice")
    bob = await join(registry, "Bob")
    await set_state(registry, alice, mic_active=True, ingest_connected=True)
    clock.advance(300)

    assert only_visit(registry, alice).interpreting_total(clock()) == 300
    assert only_visit(registry, bob).interpreting_total(clock()) == 0


@pytest.mark.anyio
async def test_handoff_moves_interpreting_time_to_the_new_active(registry, clock):
    alice = await join(registry, "Alice")
    bob = await join(registry, "Bob")
    await set_state(registry, alice, mic_active=True, ingest_connected=True)
    clock.advance(100)

    await registry.initiate_handoff(BOOTH, alice.participant_id, LANG, CHANNEL)
    await registry.accept_handoff(BOOTH, bob.participant_id, LANG, CHANNEL)
    await set_state(registry, bob, mic_active=True, ingest_connected=True)
    clock.advance(40)

    assert only_visit(registry, alice).interpreting_total(clock()) == 100
    assert only_visit(registry, bob).interpreting_total(clock()) == 40


@pytest.mark.anyio
async def test_stopping_the_broadcast_stops_interpreting(registry, clock):
    alice = await join(registry, "Alice")
    await registry.set_broadcast_unlocked(BOOTH, True, LANG, CHANNEL)
    await set_state(registry, alice, mic_active=True, ingest_connected=True)
    clock.advance(50)
    await registry.set_broadcast_unlocked(BOOTH, False, LANG, CHANNEL)
    clock.advance(50)

    assert only_visit(registry, alice).interpreting_total(clock()) == 50


@pytest.mark.anyio
async def test_leaving_while_interpreting_closes_the_running_stretch(registry, clock):
    alice = await join(registry, "Alice")
    await set_state(registry, alice, mic_active=True, ingest_connected=True)
    clock.advance(90)
    await registry.leave_participant(BOOTH, alice.participant_id, LANG, CHANNEL)
    clock.advance(500)

    [closed] = registry.attendance._closed
    assert closed.interpreting_total(clock()) == 90


@pytest.mark.anyio
async def test_identity_is_never_sent_to_browsers(registry):
    alice = await join(registry, "Alice", user_id=7, invite="pub123")
    public = participant_public_dict(alice)
    assert "user_id" not in public
    assert "invite_public_id" not in public


@pytest.mark.anyio
async def test_tracking_never_touches_the_database(clock, monkeypatch):
    """The live path only updates memory; saving happens in flush()."""

    async def fail(*_args, **_kwargs):
        raise AssertionError("database written on the live path")

    monkeypatch.setattr("portal.attendance.save_visits", fail)
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    alice = await join(registry, "Alice")
    await set_state(registry, alice, mic_active=True, ingest_connected=True)
    await registry.leave_participant(BOOTH, alice.participant_id, LANG, CHANNEL)


# ── Report ────────────────────────────────────────────────────────────────────


def row(name, start, end, interpreting=0, *, user_id=None, invite=None, room_id=1, code="de"):
    return AttendanceRow(
        id=f"{name}-{start}",
        room_id=room_id,
        language_code=code,
        user_id=user_id,
        invite_public_id=invite,
        display_name=name,
        role=INTERPRETER,
        joined_at=T0 + timedelta(minutes=start),
        left_at=None if end is None else T0 + timedelta(minutes=end),
        interpreting_seconds=interpreting,
    )


def test_summary_groups_reconnects_by_person_and_counts_overlap_once():
    rows = [
        row("Alice", 0, 60, 1800, user_id=1),
        row("alice (tablet)", 30, 90, 600, user_id=1),  # second tab overlapping the first
        row("Alice", 120, 150, 0, user_id=1),
        row("Bob", 10, 20, invite="pub1"),
        row("Bob", 25, None, invite="pub1"),
    ]
    now = T0 + timedelta(minutes=40)
    people = summarize(rows, now, users={1: ("Alice Doe", "alice@example.com")}, invite_labels={"pub1": "Day 1"})

    alice, bob = people
    assert (alice.name, alice.detail) == ("Alice Doe", "alice@example.com")
    assert len(alice.visits) == 3
    assert alice.present_seconds == (90 + 30) * 60  # 0–90 merged, plus 120–150
    assert alice.interpreting_seconds == 2400
    assert alice.interpreting_share == 33
    assert alice.last_left == T0 + timedelta(minutes=150)
    assert not alice.is_here

    assert (bob.name, bob.detail) == ("Bob", "Invite link · Day 1")
    assert bob.is_here and bob.last_left is None
    assert bob.present_seconds == (10 + 15) * 60


def test_two_people_on_one_invite_link_stay_apart():
    people = summarize([row("Ana", 0, 10, invite="p"), row("Ben", 0, 10, invite="p")], T0)
    assert sorted(p.name for p in people) == ["Ana", "Ben"]


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0m"), (45, "45s"), (60, "1m"), (59 * 60, "59m"), (3600, "1h 00m"), (3900, "1h 05m"), (36000, "10h 00m")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


# ── Persistence and page ──────────────────────────────────────────────────────


@pytest.fixture
async def db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def event_setup(db):
    async with get_session() as session:
        event = await create_event(session, slug="conf", display_name="Conf 2026")
        room = await create_room(session, event_id=event.id, display_name="Main hall")
        await create_booth(session, event_id=event.id, room_id=room.id, language_code="de", language_name="German")
        owner = await create_user(session, email="owner@example.com", display_name="Owner", can_host=True)
        coordinator = await create_user(session, email="coord@example.com", display_name="Coord")
        interpreter = await create_user(session, email="ana@example.com", display_name="Ana Interpreter")
        await set_event_membership(session, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
        await set_event_membership(session, user_id=coordinator.id, event_id=event.id, role=ROOM_COORDINATOR)
        ids = {"event": event.id, "room": room.id, "owner": owner.id, "coord": coordinator.id, "ana": interpreter.id}
    return ids


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def cookies(user_id, email):
    return {"user_token": create_user_token(user_id=user_id, email=email)}


@pytest.mark.anyio
async def test_flush_inserts_then_updates_one_row_per_visit(event_setup, clock):
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    ana, _ = await registry.join_participant(
        booth_id=booth_id, display_name="Ana", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True)
    clock.advance(120)
    assert await registry.attendance.flush() == 1

    clock.advance(60)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)
    assert await registry.attendance.flush() == 1
    assert await registry.attendance.flush() == 0  # nothing left to save

    async with get_session() as session:
        rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    assert len(rows) == 1
    saved = rows[0]
    assert saved.event_id == event_setup["event"]
    assert saved.user_id == event_setup["ana"]
    assert saved.interpreting_seconds == 180
    assert saved.left_at.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=180)


@pytest.mark.anyio
async def test_failed_flush_keeps_finished_visits_for_the_next_one(event_setup, clock, monkeypatch):
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    booth_id = f"conf-{event_setup['room']}-de"
    ana, _ = await registry.join_participant(
        booth_id=booth_id, display_name="Ana", role=INTERPRETER, language=LANG, channel_id=CHANNEL
    )
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)

    import portal.attendance as attendance

    real_save = attendance.save_visits

    async def down(*_args):
        raise OSError("database unavailable")

    monkeypatch.setattr(attendance, "save_visits", down)
    assert await registry.attendance.flush() == 0
    assert len(registry.attendance._closed) == 1

    monkeypatch.setattr(attendance, "save_visits", real_save)
    assert await registry.attendance.flush() == 1
    assert registry.attendance._closed == []


@pytest.mark.anyio
async def test_visits_left_open_by_a_restart_are_closed_at_their_last_save(event_setup):
    last_seen = T0 + timedelta(minutes=5)
    async with get_session() as session:
        session.add(
            InterpreterAttendance(
                id="a" * 32, event_id=event_setup["event"], room_id=event_setup["room"], language_code="de",
                display_name="Ana", role=INTERPRETER, joined_at=T0, last_seen_at=last_seen,
            )
        )
    assert await close_stale_visits() == 1
    async with get_session() as session:
        saved = await session.get(InterpreterAttendance, "a" * 32)
    assert saved.left_at.replace(tzinfo=timezone.utc) == last_seen


@pytest.mark.anyio
async def test_owner_sees_attendance_with_live_and_saved_visits(event_setup, monkeypatch):
    clock = Clock()
    clock.now = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    monkeypatch.setattr(portal_globals.booths, "attendance", registry.attendance)
    booth_id = f"conf-{event_setup['room']}-de"

    # A finished, saved visit…
    ana, _ = await registry.join_participant(
        booth_id=booth_id, display_name="typed name", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True)
    clock.advance(300)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)
    await registry.attendance.flush()
    # …and someone in the booth right now, not saved yet.
    await registry.join_participant(
        booth_id=booth_id, display_name="=HYPERLINK(evil)", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        invite_public_id="pubX",
    )

    owner = cookies(event_setup["owner"], "owner@example.com")
    async with client() as http:
        page = await http.get(f"/workspace/events/{event_setup['event']}/attendance/", cookies=owner)
        export = await http.get(f"/workspace/events/{event_setup['event']}/attendance/export", cookies=owner)
        event_page = await http.get(f"/workspace/events/{event_setup['event']}/", cookies=owner)

    assert page.status_code == 200
    assert "Ana Interpreter" in page.text  # account name, not the typed one
    assert "ana@example.com" in page.text
    assert "Main hall · German" in page.text
    assert "In booth now" in page.text
    assert "5m" in page.text

    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "conf-interpreter-attendance.csv" in export.headers["content-disposition"]
    lines = export.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("Interpreter,")
    assert any(line.startswith("Ana Interpreter,ana@example.com,Main hall · German,") and line.endswith(",5.0,5.0") for line in lines)
    assert any(line.startswith("'=HYPERLINK(evil)") for line in lines), "formula cells are neutralized"

    assert f"/workspace/events/{event_setup['event']}/attendance/" in event_page.text


@pytest.mark.anyio
async def test_empty_attendance_explains_what_will_appear(event_setup):
    owner = cookies(event_setup["owner"], "owner@example.com")
    async with client() as http:
        page = await http.get(f"/workspace/events/{event_setup['event']}/attendance/", cookies=owner)
    assert page.status_code == 200
    assert "No one has joined a booth yet" in page.text
    assert "Download CSV" not in page.text


@pytest.mark.anyio
async def test_coordinators_and_strangers_cannot_see_attendance(event_setup):
    coordinator = cookies(event_setup["coord"], "coord@example.com")
    interpreter = cookies(event_setup["ana"], "ana@example.com")
    async with client() as http:
        for who in (coordinator, interpreter):
            for path in ("attendance/", "attendance/export"):
                response = await http.get(f"/admin/events/{event_setup['event']}/{path}", cookies=who)
                assert response.status_code in (302, 303, 403), path
        event_page = await http.get(f"/admin/events/{event_setup['event']}/", cookies=coordinator)
    assert "/attendance/" not in event_page.text


def test_workspace_paths_map_to_attendance_handlers():
    assert workspace_to_admin_path("/workspace/events/4/attendance/") == "/admin/events/4/attendance/"
    assert workspace_to_admin_path("/workspace/events/4/attendance/export") == "/admin/events/4/attendance/export"


@pytest.mark.anyio
async def test_day_tabs_and_booth_filter_narrow_the_page_and_the_csv(event_setup, monkeypatch):
    """Days follow the viewer's time zone (vb_tz cookie); filters also apply to the export."""
    room = event_setup["room"]
    async with get_session() as session:
        await create_booth(session, event_id=event_setup["event"], room_id=room, language_code="fr", language_name="French")
        for ident, name, code, start in (
            ("a" * 32, "Day one German", "de", datetime(2026, 11, 9, 9, tzinfo=timezone.utc)),
            ("b" * 32, "Day two French", "fr", datetime(2026, 11, 10, 9, tzinfo=timezone.utc)),
            # 23:30 UTC on the 10th is already the 11th in Kolkata (UTC+5:30).
            ("c" * 32, "Late German", "de", datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc)),
        ):
            session.add(
                InterpreterAttendance(
                    id=ident, event_id=event_setup["event"], room_id=room, language_code=code, display_name=name,
                    role=INTERPRETER, joined_at=start, left_at=start + timedelta(hours=1),
                    last_seen_at=start + timedelta(hours=1), interpreting_seconds=1800,
                )
            )
    monkeypatch.setattr(portal_globals.booths, "attendance", AttendanceTracker())
    owner = cookies(event_setup["owner"], "owner@example.com")
    base = f"/workspace/events/{event_setup['event']}/attendance/"
    async with client() as http:
        http.cookies.set("vb_tz", "Asia%2FKolkata")  # as the browser writes it
        everything = await http.get(base, cookies=owner)
        day_two = await http.get(f"{base}?day=2026-11-10", cookies=owner)
        late_day = await http.get(f"{base}?day=2026-11-11", cookies=owner)
        german = await http.get(f"{base}?booth={room}-de", cookies=owner)
        bogus = await http.get(f"{base}?day=not-a-day&booth=<script>", cookies=owner)
        export = await http.get(f"{base}export?day=2026-11-10", cookies=owner)

    assert all(name in everything.text for name in ("Day one German", "Day two French", "Late German"))
    assert "?day=2026-11-11" in everything.text, "the late visit is on the 11th in the viewer's time zone"
    assert "Day two French" in day_two.text and "Late German" not in day_two.text
    assert "Late German" in late_day.text and "Day two French" not in late_day.text
    assert "Day one German" in german.text and "Day two French" not in german.text
    assert "export?booth=" in german.text, "the CSV link keeps the filter"
    assert bogus.status_code == 200 and "<script>" not in bogus.text
    assert "2026-11-10" in export.headers["content-disposition"]
    assert "Day two French" in export.text and "Day one German" not in export.text
    assert "Late German" not in export.text, "the late visit belongs to the 11th, not the 10th"


@pytest.mark.anyio
async def test_a_deleted_user_does_not_block_the_flush(event_setup, clock):
    """The visit is kept without the account, and other visits in the batch still save."""
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    await registry.join_participant(
        booth_id=booth_id, display_name="Gone", role=INTERPRETER, language=LANG, channel_id=CHANNEL, user_id=999_999
    )
    await registry.join_participant(
        booth_id=booth_id, display_name="Ana", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    assert await registry.attendance.flush() == 2

    async with get_session() as session:
        rows = {row.display_name: row for row in (await session.execute(select(InterpreterAttendance))).scalars()}
    assert rows["Gone"].user_id is None
    assert rows["Ana"].user_id == event_setup["ana"]


@pytest.mark.anyio
async def test_shutdown_retries_when_a_user_is_deleted_mid_flush(event_setup, clock, monkeypatch):
    """The account is deleted between the user lookup and the write: the write fails on the
    foreign key, and the retry saves that visit without the account and keeps the others."""
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    import portal.attendance as attendance
    from portal.models import User

    real_save = attendance.save_visits
    calls = 0

    async def deleted_during_first_save(visits, now):
        nonlocal calls
        calls += 1
        if calls == 1:
            async with get_session() as session:
                await session.execute(delete(User).where(User.id == event_setup["ana"]))
            raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))
        return await real_save(visits, now)

    monkeypatch.setattr(attendance, "save_visits", deleted_during_first_save)
    monkeypatch.setattr(attendance, "SHUTDOWN_RETRY_DELAY_SECS", 0)
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    await registry.join_participant(
        booth_id=booth_id, display_name="Ana", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.join_participant(
        booth_id=booth_id, display_name="Ben", role=INTERPRETER, language=LANG, channel_id=CHANNEL,
        invite_public_id="pubB",
    )
    task = asyncio.create_task(registry.attendance.run(interval=3600))
    await asyncio.sleep(0.05)  # let it start and wait for the first interval
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 2
    assert registry.attendance._closed == []
    async with get_session() as session:
        saved = {row.display_name: row for row in (await session.execute(select(InterpreterAttendance))).scalars()}
    assert set(saved) == {"Ana", "Ben"}
    assert saved["Ana"].user_id is None, "the deleted account is dropped, the visit is kept"
    assert saved["Ben"].invite_public_id == "pubB"
    assert all(row.left_at is not None for row in saved.values())


@pytest.mark.anyio
async def test_shutdown_during_recovery_still_saves_visits(event_setup, clock, monkeypatch):
    """Cancelling while startup recovery is still running must not skip the final save."""
    import asyncio

    import portal.attendance as attendance

    recovering = asyncio.Event()

    async def slow_recovery():
        recovering.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(attendance, "close_stale_visits", slow_recovery)
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    await registry.join_participant(
        booth_id=f"conf-{event_setup['room']}-de", display_name="Ana", role=INTERPRETER, language=LANG,
        channel_id=CHANNEL,
    )
    task = asyncio.create_task(registry.attendance.run(interval=3600))
    await recovering.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with get_session() as session:
        saved = list((await session.execute(select(InterpreterAttendance))).scalars())
    assert [row.display_name for row in saved] == ["Ana"]
