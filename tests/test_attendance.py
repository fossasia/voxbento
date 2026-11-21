"""Interpreter attendance and interpreting time (#1215)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal import attendance as attendance_module
from portal import database as db_module
from portal import globals as portal_globals
from portal.attendance import (
    AttendanceRow,
    AttendanceTracker,
    Visit,
    close_stale_visits,
    format_duration,
    rows_for_event,
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
    delete_event,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import Event, InterpreterAttendance
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR
from portal.routers import attendance as att_router
from portal.routers.attendance import _load
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
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Kolkata")
    except Exception:
        monkeypatch.setattr(
            att_router,
            "_viewer_tz",
            lambda req: timezone(timedelta(hours=5, minutes=30))
            if "Asia%2FKolkata" in req.cookies.get("vb_tz", "")
            else timezone.utc,
        )
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


# ── Pending visits & closed unflushed regression tests ────────────────────────


@pytest.mark.anyio
async def test_pending_visits_includes_open_and_closed_unflushed(registry, clock):
    alice = await join(registry, "Alice")
    bob = await join(registry, "Bob")
    clock.advance(60)
    await registry.leave_participant(BOOTH, alice.participant_id, LANG, CHANNEL)

    open_visits = registry.attendance.open_visits("conf")
    assert len(open_visits) == 1
    assert open_visits[0].participant_id == bob.participant_id

    pending = registry.attendance.pending_visits("conf")
    assert len(pending) == 2
    pending_pids = {v.participant_id for v in pending}
    assert pending_pids == {alice.participant_id, bob.participant_id}

    # Different event slug should return nothing
    assert registry.attendance.pending_visits("other") == []


def test_rows_for_event_short_visit_closed_before_first_flush(clock):
    tracker = AttendanceTracker(clock=clock)

    visit = Visit(
        id="v-short-1",
        booth_id=BOOTH,
        event_slug="conf",
        room_id=1,
        language_code="de",
        participant_id="p-short-1",
        user_id=None,
        invite_public_id=None,
        display_name="Short Visit Interpreter",
        role=INTERPRETER,
        joined_at=T0,
        left_at=T0 + timedelta(seconds=45),
        interpreting_seconds=45.0,
    )
    tracker._closed.append(visit)

    # Clock moves far ahead (e.g. 10 minutes later)
    now = T0 + timedelta(minutes=10)
    rows = rows_for_event([], tracker.pending_visits("conf"), now)

    assert len(rows) == 1
    assert rows[0].id == "v-short-1"
    assert rows[0].left_at == T0 + timedelta(seconds=45)
    assert rows[0].interpreting_seconds == 45.0

    people = summarize(rows, now)
    assert len(people) == 1
    person = people[0]
    assert not person.is_here
    assert person.last_left == T0 + timedelta(seconds=45)
    assert person.visits[0].is_open is False
    assert person.visits[0].present_seconds == 45
    assert person.visits[0].interpreting_seconds == 45


@pytest.mark.anyio
async def test_rows_for_event_previously_flushed_visit_closes_before_next_flush(event_setup, clock):
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(
        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )
    clock.advance(60)
    # First flush: saved while open
    assert await registry.attendance.flush() == 1

    # Continues interpreting for 30s, then leaves
    clock.advance(30)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)

    # Next flush has NOT run yet. Clock advances another 5 minutes.
    clock.advance(300)
    now = clock()

    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    assert len(db_rows) == 1
    # DB row is stale: saved at T0+60s, left_at is None, last_seen_at is T0+60s
    assert db_rows[0].left_at is None
    assert db_rows[0].last_seen_at.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=60)
    assert db_rows[0].interpreting_seconds == 60

    pending = registry.attendance.pending_visits("conf")
    assert len(pending) == 1
    assert pending[0].left_at == T0 + timedelta(seconds=90)

    rows = rows_for_event(db_rows, pending, now)
    # Deduplication: exactly 1 row returned
    assert len(rows) == 1
    # In-memory closed visit overwrites stale DB row and stale last_seen_at
    assert rows[0].left_at == T0 + timedelta(seconds=90)
    assert rows[0].interpreting_seconds == 90

    people = summarize(rows, now)
    assert len(people) == 1
    assert not people[0].is_here
    assert people[0].last_left == T0 + timedelta(seconds=90)
    assert people[0].present_seconds == 90
    assert people[0].interpreting_seconds == 90


@pytest.mark.anyio
async def test_no_visit_lost_or_duplicated_across_flush_lifecycle(event_setup, clock):
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(
        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )

    # 1. Open, unflushed
    clock.advance(60)
    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    rows = rows_for_event(db_rows, registry.attendance.pending_visits("conf"), clock())
    assert len(rows) == 1
    assert rows[0].left_at is None
    assert rows[0].interpreting_seconds == 60

    # 2. Open, flushed
    assert await registry.attendance.flush() == 1
    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    rows = rows_for_event(db_rows, registry.attendance.pending_visits("conf"), clock())
    assert len(rows) == 1
    assert rows[0].left_at is None
    assert rows[0].interpreting_seconds == 60

    # 3. Closed, unflushed
    clock.advance(30)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)
    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    rows = rows_for_event(db_rows, registry.attendance.pending_visits("conf"), clock())
    assert len(rows) == 1
    assert rows[0].left_at == T0 + timedelta(seconds=90)
    assert rows[0].interpreting_seconds == 90

    # 4. Closed, flushed
    assert await registry.attendance.flush() == 1
    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    rows = rows_for_event(db_rows, registry.attendance.pending_visits("conf"), clock())
    assert len(rows) == 1
    assert rows[0].left_at == T0 + timedelta(seconds=90)
    assert rows[0].interpreting_seconds == 90


@pytest.mark.anyio
async def test_load_snapshots_memory_before_db_query(event_setup, clock, monkeypatch):
    """_load() snapshots pending visits before querying DB so a flush mid-query cannot drop visits."""
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    monkeypatch.setattr(portal_globals.booths, "attendance", registry.attendance)

    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(
        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )
    clock.advance(60)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)

    # Before _load: visit is in memory (_closed), DB has 0 rows for this visit
    assert len(registry.attendance._closed) == 1
    async with db_module.get_session() as session:
        db_count = len(list((await session.execute(select(InterpreterAttendance))).scalars()))
    assert db_count == 0

    # Wrap session so that when _load queries InterpreterAttendance, a flush runs
    # right after the DB query executes its snapshot (returning 0 rows):
    @asynccontextmanager
    async def hooked_session():
        async with db_module.get_session() as session:
            orig_execute = session.execute

            async def mock_execute(statement, *args, **kwargs):
                if "interpreter_attendance" in str(statement).lower():
                    # Query DB first (DB returns 0 rows since flush hasn't committed yet)
                    result = await orig_execute(statement, *args, **kwargs)
                    # Flush commits right now, persisting the visit to DB and clearing _closed:
                    await registry.attendance.flush()
                    return result
                return await orig_execute(statement, *args, **kwargs)

            session.execute = mock_execute
            yield session

    monkeypatch.setattr(att_router, "get_session", hooked_session)

    # Call the actual _load() function!
    data = await _load(event_setup["event"])

    # If _load snapshotted memory first: data.rows has 1 row with accurate left_at and interpreting_seconds.
    # If _load queried DB first: data.rows would be 0 (dropped visit).
    assert len(data.rows) == 1
    assert data.rows[0].left_at == T0 + timedelta(seconds=60)
    assert data.rows[0].interpreting_seconds == 60


@pytest.mark.anyio
async def test_flush_preserves_visits_closed_concurrently_during_persistence(event_setup, clock, monkeypatch):
    """A visit closed while flush() is awaiting database persistence remains in _closed for the next flush."""
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))

    alice, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Alice",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    clock.advance(60)
    await registry.leave_participant(booth_id, alice.participant_id, LANG, CHANNEL)
    assert len(registry.attendance._closed) == 1

    real_save_visits = attendance_module.save_visits

    async def save_with_concurrent_close(visits, now):
        # While the first batch is being saved to DB, Bob joins and leaves:
        bob, _ = await registry.join_participant(
            booth_id=booth_id,
            display_name="Bob",
            role=INTERPRETER,
            language=LANG,
            channel_id=CHANNEL,
        )
        clock.advance(30)
        await registry.leave_participant(booth_id, bob.participant_id, LANG, CHANNEL)
        return await real_save_visits(visits, now)

    monkeypatch.setattr(attendance_module, "save_visits", save_with_concurrent_close)

    # First flush saves Alice:
    written = await registry.attendance.flush()
    assert written == 1

    # Alice was saved and cleared from _closed, but Bob (closed during flush) remains in _closed:
    assert len(registry.attendance._closed) == 1
    assert registry.attendance._closed[0].display_name == "Bob"

    # Restore real save_visits:
    monkeypatch.setattr(attendance_module, "save_visits", real_save_visits)

    # Second flush saves Bob:
    written_second = await registry.attendance.flush()
    assert written_second == 1
    assert registry.attendance._closed == []

    # Both visits are saved in database:
    async with get_session() as session:
        db_rows = list((await session.execute(select(InterpreterAttendance))).scalars())
    assert {r.display_name for r in db_rows} == {"Alice", "Bob"}


@pytest.mark.anyio
async def test_closed_unflushed_visit_in_html_view_and_csv_export(event_setup, monkeypatch):
    clock = Clock()
    clock.now = datetime.now(tz=timezone.utc) - timedelta(minutes=15)
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    monkeypatch.setattr(portal_globals.booths, "attendance", registry.attendance)
    booth_id = f"conf-{event_setup['room']}-de"

    # Join, interpret for 3 minutes, then leave. Do NOT flush to DB.
    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(
        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )
    clock.advance(180)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, CHANNEL)

    # Verify visit is in _closed and DB is empty
    assert len(registry.attendance._closed) == 1
    async with get_session() as session:
        db_count = len(list((await session.execute(select(InterpreterAttendance))).scalars()))
    assert db_count == 0

    owner = cookies(event_setup["owner"], "owner@example.com")
    async with client() as http:
        page = await http.get(f"/workspace/events/{event_setup['event']}/attendance/", cookies=owner)
        export = await http.get(f"/workspace/events/{event_setup['event']}/attendance/export", cookies=owner)

    # 1. HTML view verification
    assert page.status_code == 200
    assert "Ana Interpreter" in page.text
    assert "ana@example.com" in page.text
    assert "3m" in page.text
    # Should not show "● In booth now" for a closed visit
    assert "● In booth now" not in page.text

    # 2. CSV export verification
    assert export.status_code == 200
    lines = export.text.lstrip("\ufeff").splitlines()
    assert lines[0].startswith("Interpreter,")
    ana_line = next(line for line in lines if "Ana Interpreter" in line)
    # Check that Left (UTC) is non-empty and duration is 3.0 minutes
    parts = ana_line.split(",")
    # parts: [Interpreter, Detail, Booth, Joined, Left, Minutes in booth, Minutes interpreting]
    assert len(parts) >= 7
    left_utc = parts[4]
    assert left_utc != ""
    assert parts[5] == "3.0"
    assert parts[6] == "3.0"


@pytest.mark.anyio
async def test_load_snapshots_prevent_interpreting_time_extending_past_captured_now(event_setup, clock, monkeypatch):
    """If interpreting state changes during _load's DB query, report duration does not exceed captured now."""
    booth_id = f"conf-{event_setup['room']}-de"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    monkeypatch.setattr(portal_globals.booths, "attendance", registry.attendance)

    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role=INTERPRETER,
        language=LANG,
        channel_id=CHANNEL,
        user_id=event_setup["ana"],
    )
    await registry.update_participant_state(
        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=True, ingest_connected=True
    )
    clock.advance(60)

    # Hook session.execute to advance clock and mutate live visit while _load awaits DB:
    @asynccontextmanager
    async def hooked_session():
        async with db_module.get_session() as session:
            orig_execute = session.execute

            async def mock_execute(statement, *args, **kwargs):
                if "interpreter_attendance" in str(statement).lower():
                    result = await orig_execute(statement, *args, **kwargs)
                    # Advance clock by 30s and stop interpreting mid-query:
                    clock.advance(30)
                    await registry.update_participant_state(
                        booth_id, ana.participant_id, LANG, CHANNEL, mic_active=False, ingest_connected=True
                    )
                    return result
                return await orig_execute(statement, *args, **kwargs)

            session.execute = mock_execute
            yield session

    monkeypatch.setattr(att_router, "get_session", hooked_session)

    data = await _load(event_setup["event"])
    assert len(data.rows) == 1
    # Interpreting seconds should reflect the snapshot before await (60s), not the later 90s:
    assert data.rows[0].interpreting_seconds == 60
    people = summarize(data.rows, data.now, users=data.users, invite_labels=data.invite_labels)
    assert people[0].visits[0].interpreting_seconds == 60
    assert people[0].visits[0].present_seconds == 60


@pytest.mark.anyio
async def test_reused_event_slug_does_not_leak_old_visits_into_new_event(db, clock, monkeypatch):
    """When an event is deleted and its slug reused, visits from the deleted event are not included."""
    slug = f"reuse-{uuid4().hex[:8]}"
    registry = BoothRegistry(attendance=AttendanceTracker(clock=clock))
    monkeypatch.setattr(portal_globals.booths, "attendance", registry.attendance)

    async with get_session() as session:
        ev1 = await create_event(session, slug=slug, display_name="Event One")
        room1 = await create_room(session, event_id=ev1.id, display_name="Room 1")
        booth_id = f"{slug}-{room1.id}-de"

    # Interpreter joins booth in Event 1:
    ana, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name="Ana Former",
        role=INTERPRETER,
        language=LANG,
        channel_id=f"{slug}-1-de",
    )
    clock.advance(60)
    await registry.leave_participant(booth_id, ana.participant_id, LANG, f"{slug}-1-de")

    # Pending visits has the visit from Event 1:
    assert len(registry.attendance.pending_visits(slug)) == 1

    # Delete Event 1:
    async with get_session() as session:
        assert await delete_event(session, ev1.id) is True

    # Memory was purged for deleted event:
    assert registry.attendance.pending_visits(slug) == []

    # Create Event 2 reusing the same slug:
    clock.advance(60)
    async with get_session() as session:
        ev2 = await create_event(session, slug=slug, display_name="Event Two")

    # Load attendance for Event 2:
    data = await _load(ev2.id)
    assert len(data.rows) == 0


@pytest.mark.anyio
async def test_reused_event_slug_in_flight_flush_does_not_save_discarded_visits(db, clock, monkeypatch):
    """An in-flight flush does not write visits into a new event reusing the same slug."""
    slug = f"inflight-{uuid4().hex[:8]}"
    tracker = AttendanceTracker(clock=clock)
    monkeypatch.setattr(portal_globals.booths, "attendance", tracker)

    async with get_session() as session:
        ev1 = await create_event(session, slug=slug, display_name="Event 1")
        room1 = await create_room(session, event_id=ev1.id, display_name="Room 1")

    visit = Visit(
        id=uuid4().hex,
        booth_id=f"{slug}-{room1.id}-de",
        event_slug=slug,
        room_id=room1.id,
        language_code="de",
        participant_id="part-1",
        user_id=None,
        invite_public_id=None,
        display_name="Ana Inflight",
        role="interpreter",
        joined_at=clock(),
        left_at=clock(),
        interpreting_seconds=30.0,
    )
    tracker._closed.append(visit)

    # Hook get_session so that during the Event lookup inside save_visits, ev1 is deleted
    # and ev2 is recreated with the same slug. The event query will find ev2, but visit.discarded
    # must prevent writing the old visit under ev2.
    orig_get_session = attendance_module.get_session

    @asynccontextmanager
    async def hooked_session():
        async with orig_get_session() as session:
            orig_execute = session.execute

            async def mock_execute(statement, *args, **kwargs):
                stmt_str = str(statement).lower()
                if "events.slug in" in stmt_str or "events.slug" in stmt_str:
                    # Concurrently delete ev1 and recreate ev2 with the same slug mid-query:
                    async with orig_get_session() as s2:
                        await delete_event(s2, ev1.id)
                        await create_event(s2, slug=slug, display_name="Event 2")
                return await orig_execute(statement, *args, **kwargs)

            session.execute = mock_execute
            yield session

    monkeypatch.setattr(attendance_module, "get_session", hooked_session)

    await tracker.flush()

    # Verify no visits were saved for ev2:
    async with get_session() as session:
        ev2_rows = (
            await session.execute(
                select(InterpreterAttendance).join(Event).where(Event.slug == slug)
            )
        ).scalars().all()
        assert len(ev2_rows) == 0


@pytest.mark.anyio
async def test_delete_event_rollback_does_not_discard_attendance(db, clock, monkeypatch):
    """If delete_event rolls back, its pending attendance visits are not discarded on a later commit."""
    slug = f"rollback-{uuid4().hex[:8]}"
    tracker = AttendanceTracker(clock=clock)
    monkeypatch.setattr(portal_globals.booths, "attendance", tracker)

    async with get_session() as session:
        ev = await create_event(session, slug=slug, display_name="Event Rollback")
        room = await create_room(session, event_id=ev.id, display_name="Room 1")

    visit = Visit(
        id=uuid4().hex,
        booth_id=f"{slug}-{room.id}-de",
        event_slug=slug,
        room_id=room.id,
        language_code="de",
        participant_id="part-rb",
        user_id=None,
        invite_public_id=None,
        display_name="Ana Rollback",
        role="interpreter",
        joined_at=clock(),
        left_at=clock(),
        interpreting_seconds=45.0,
    )
    tracker._closed.append(visit)
    assert len(tracker.pending_visits(slug)) == 1

    # Call delete_event on a session, then roll back:
    session_factory = db_module._get_session_factory()
    async with session_factory() as session:
        await delete_event(session, ev.id)
        await session.rollback()

        # Same session commits an unrelated change later:
        await create_event(session, slug=f"other-{uuid4().hex[:8]}", display_name="Other")
        await session.commit()

    # The pending visits for the rolled-back event must still be present:
    assert len(tracker.pending_visits(slug)) == 1
    assert tracker.pending_visits(slug)[0].discarded is False

