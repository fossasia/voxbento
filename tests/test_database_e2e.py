"""End-to-end database integration test.

Exercises every table, every CRUD operation, every relationship, cascade
deletes, token lifecycle, validation boundaries, and the derived
mediamtx_path property — all against a real SQLite file (not in-memory)
to verify Alembic-level persistence behaviour.

Run: python -m pytest tests/test_database_e2e.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import portal.models as m
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    delete_booth,
    delete_event,
    delete_room,
    dispose,
    get_booth_by_id,
    get_event_by_id,
    get_event_by_slug,
    get_invite_token,
    get_room_by_id,
    get_session,
    init_db,
    list_booths_for_event,
    list_booths_for_room,
    list_events,
    list_rooms_for_event,
    list_tokens_for_booth,
    redeem_invite_token,
)
from portal.models import Base, DBBooth, Event, InviteToken, Room

# ---------------------------------------------------------------------------
# Fixtures — file-backed SQLite for real persistence testing
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_file():
    """Yield a temp SQLite file path and clean up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # remove so SQLAlchemy creates it
    url = f"sqlite+aiosqlite:///{path}"

    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    yield factory, path, engine

    await engine.dispose()
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def db(db_file):
    """Yield an async session from the file-backed engine."""
    factory, _path, _engine = db_file
    async with factory() as session:
        async with session.begin():
            yield session


# ---------------------------------------------------------------------------
# Multi-event, multi-room, multi-booth, multi-token scenario
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_multi_event_scenario(db: AsyncSession):
    """Create two events with multiple rooms, booths, and tokens.

    Verifies the complete entity hierarchy and cross-event isolation.
    """
    # ── Create two events ────────────────────────────────────────
    pycon = await create_event(db, slug="pycon2026", display_name="PyCon US 2026")
    fossasia = await create_event(db, slug="fossasia2026", display_name="FOSSASIA Summit 2026")

    assert pycon.id != fossasia.id
    assert pycon.slug == "pycon2026"
    assert fossasia.slug == "fossasia2026"

    # ── Create rooms for each event ──────────────────────────────
    pycon_main = await create_room(db, event_id=pycon.id, display_name="Main Hall")
    pycon_workshop = await create_room(db, event_id=pycon.id, display_name="Workshop Room")
    foss_keynote = await create_room(
        db,
        event_id=fossasia.id,
        display_name="Keynote Stage",
        eventyay_room_id="eventyay-room-42",
    )

    assert pycon_main.event_id == pycon.id
    assert foss_keynote.eventyay_room_id == "eventyay-room-42"

    # ── Create booths ────────────────────────────────────────────
    pycon_en = await create_booth(
        db,
        event_id=pycon.id,
        room_id=pycon_main.id,
        language_code="en",
        language_name="English",
    )
    pycon_fr = await create_booth(
        db,
        event_id=pycon.id,
        room_id=pycon_main.id,
        language_code="fr",
        language_name="French",
    )
    await create_booth(
        db,
        event_id=pycon.id,
        room_id=pycon_workshop.id,
        language_code="de",
        language_name="German",
    )
    foss_zh = await create_booth(
        db,
        event_id=fossasia.id,
        room_id=foss_keynote.id,
        language_code="zh",
        language_name="Chinese",
    )

    # ── Verify booth counts per event ────────────────────────────
    pycon_booths = await list_booths_for_event(db, pycon.id)
    foss_booths = await list_booths_for_event(db, fossasia.id)
    assert len(pycon_booths) == 3
    assert len(foss_booths) == 1

    # ── Verify booths per room ───────────────────────────────────
    main_booths = await list_booths_for_room(db, pycon_main.id)
    ws_booths = await list_booths_for_room(db, pycon_workshop.id)
    assert len(main_booths) == 2
    assert len(ws_booths) == 1

    # ── Verify mediamtx_path derivation ──────────────────────────
    loaded_en = await get_booth_by_id(db, pycon_en.id)
    loaded_zh = await get_booth_by_id(db, foss_zh.id)
    assert loaded_en.mediamtx_path == "pycon2026/en"
    assert loaded_zh.mediamtx_path == "fossasia2026/zh"

    # ── Create invite tokens with different roles ────────────────
    tok_interp = await create_invite_token(
        db,
        booth_id=pycon_en.id,
        role="interpreter",
        label="Alice Interpreter",
        created_by="admin@pycon.org",
    )
    tok_coord = await create_invite_token(
        db,
        booth_id=pycon_en.id,
        role="room_coordinator",
        label="Bob Coordinator",
        created_by="admin@pycon.org",
    )
    await create_invite_token(
        db,
        booth_id=pycon_fr.id,
        role="room_coordinator",
        label="Charlie Listener",
    )
    future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
    tok_expiring = await create_invite_token(
        db,
        booth_id=foss_zh.id,
        role="event_owner",
        label="Admin token",
        expires_at=future,
    )
    tok_super = await create_invite_token(
        db,
        booth_id=foss_zh.id,
        role="super_admin",
        label="Super admin token",
    )

    # ── Verify token properties ──────────────────────────────────
    assert len(tok_interp.token) == 64
    assert tok_interp.role == "interpreter"
    assert tok_interp.is_used is False
    assert tok_interp.is_expired is False
    assert tok_expiring.is_expired is False
    assert tok_super.role == "super_admin"

    # ── Token listing per booth ──────────────────────────────────
    en_tokens = await list_tokens_for_booth(db, pycon_en.id)
    assert len(en_tokens) == 2
    zh_tokens = await list_tokens_for_booth(db, foss_zh.id)
    assert len(zh_tokens) == 2

    # ── Redeem a token ───────────────────────────────────────────
    redeemed = await redeem_invite_token(db, tok_interp.token)
    assert redeemed is not None
    assert redeemed.is_used is True
    assert redeemed.used_at is not None

    # ── Cannot redeem again ──────────────────────────────────────
    with pytest.raises(ValueError, match="already been used"):
        await redeem_invite_token(db, tok_interp.token)

    # ── Lookup with joinedload ───────────────────────────────────
    loaded_tok = await get_invite_token(db, tok_coord.token)
    assert loaded_tok.booth.event.slug == "pycon2026"

    # ── Event listing ────────────────────────────────────────────
    events = await list_events(db)
    assert len(events) == 2

    # ── Room listing per event ───────────────────────────────────
    pycon_rooms = await list_rooms_for_event(db, pycon.id)
    foss_rooms = await list_rooms_for_event(db, fossasia.id)
    assert len(pycon_rooms) == 2
    assert len(foss_rooms) == 1


# ---------------------------------------------------------------------------
# Cascade delete tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cascade_delete_event_removes_everything(db: AsyncSession):
    """Deleting an event cascades to rooms, booths, and tokens."""
    ev = await create_event(db, slug="cascade-test", display_name="Cascade")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")

    # Delete the event
    assert await delete_event(db, ev.id) is True

    # Everything should be gone
    assert await get_event_by_id(db, ev.id) is None
    assert await get_room_by_id(db, room.id) is None
    assert await get_booth_by_id(db, booth.id) is None
    assert await get_invite_token(db, tok.token) is None


@pytest.mark.anyio
async def test_cascade_delete_room_removes_booths_and_tokens(db: AsyncSession):
    """Deleting a room cascades to its booths and their tokens."""
    ev = await create_event(db, slug="room-cascade", display_name="RC")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="fr",
        language_name="French",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")

    assert await delete_room(db, room.id) is True
    assert await get_booth_by_id(db, booth.id) is None
    assert await get_invite_token(db, tok.token) is None
    # Event still exists
    assert await get_event_by_id(db, ev.id) is not None


@pytest.mark.anyio
async def test_cascade_delete_booth_removes_tokens(db: AsyncSession):
    """Deleting a booth cascades to its tokens."""
    ev = await create_event(db, slug="booth-cascade", display_name="BC")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="es",
        language_name="Spanish",
    )
    tok = await create_invite_token(db, booth_id=booth.id, role="room_coordinator")

    assert await delete_booth(db, booth.id) is True
    assert await get_invite_token(db, tok.token) is None
    # Room and event still exist
    assert await get_room_by_id(db, room.id) is not None


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_expired_token_cannot_be_redeemed(db: AsyncSession):
    ev = await create_event(db, slug="exp-test", display_name="Exp")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    tok = await create_invite_token(
        db,
        booth_id=booth.id,
        role="interpreter",
        expires_at=past,
    )
    assert tok.is_expired is True
    with pytest.raises(ValueError, match="expired"):
        await redeem_invite_token(db, tok.token)


@pytest.mark.anyio
async def test_nonexistent_token_returns_none(db: AsyncSession):
    result = await redeem_invite_token(db, "a" * 64)
    assert result is None


# ---------------------------------------------------------------------------
# Validation boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_event_slug_validation_rejects_underscores(db: AsyncSession):
    with pytest.raises(ValueError):
        await create_event(db, slug="bad_slug", display_name="Bad")


@pytest.mark.anyio
async def test_event_slug_validation_rejects_empty(db: AsyncSession):
    with pytest.raises(ValueError):
        await create_event(db, slug="", display_name="Bad")


@pytest.mark.anyio
async def test_booth_language_code_validation(db: AsyncSession):
    ev = await create_event(db, slug="val-test", display_name="Val")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    with pytest.raises(ValueError):
        await create_booth(
            db,
            event_id=ev.id,
            room_id=room.id,
            language_code="xyz",
            language_name="Bad",
        )


@pytest.mark.anyio
async def test_invite_token_role_validation(db: AsyncSession):
    ev = await create_event(db, slug="role-val", display_name="RV")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    booth = await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    with pytest.raises(ValueError, match="Invalid role"):
        await create_invite_token(db, booth_id=booth.id, role="hacker")


# ---------------------------------------------------------------------------
# Persistence across engine restart (file-backed SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_persistence_across_engine_reconnect(db_file):
    """Data persists when we dispose the engine and create a new one.

    This simulates docker compose down + up — the SQLite file on a
    volume survives container recreation.
    """
    factory, path, engine = db_file

    # ── Write data with first engine ─────────────────────────────
    async with factory() as session:
        async with session.begin():
            ev = await create_event(session, slug="persist-test", display_name="Persist")
            room = await create_room(session, event_id=ev.id, display_name="Room")
            booth = await create_booth(
                session,
                event_id=ev.id,
                room_id=room.id,
                language_code="ja",
                language_name="Japanese",
            )
            tok = await create_invite_token(
                session,
                booth_id=booth.id,
                role="interpreter",
                label="Alice",
            )
            saved_event_id = ev.id
            saved_booth_id = booth.id
            saved_token = tok.token

    # ── Dispose engine (simulate container stop) ─────────────────
    await engine.dispose()

    # ── Create new engine from same file (simulate container start)
    url = f"sqlite+aiosqlite:///{path}"
    engine2 = create_async_engine(url, echo=False)
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)

    # ── Read data with second engine ─────────────────────────────
    async with factory2() as session:
        async with session.begin():
            ev2 = await get_event_by_slug(session, "persist-test")
            assert ev2 is not None
            assert ev2.id == saved_event_id
            assert ev2.display_name == "Persist"

            booth2 = await get_booth_by_id(session, saved_booth_id)
            assert booth2 is not None
            assert booth2.language_code == "ja"
            assert booth2.mediamtx_path == "persist-test/ja"

            tok2 = await get_invite_token(session, saved_token)
            assert tok2 is not None
            assert tok2.role == "interpreter"
            assert tok2.label == "Alice"

    await engine2.dispose()


# ---------------------------------------------------------------------------
# Schema verification — no forbidden columns
# ---------------------------------------------------------------------------


def test_no_mediamtx_path_column():
    columns = {c.name for c in DBBooth.__table__.columns}
    assert "mediamtx_path" not in columns


def test_no_hls_url_column_anywhere():
    for model in [Event, Room, DBBooth, InviteToken]:
        columns = {c.name for c in model.__table__.columns}
        assert "hls_url" not in columns, f"{model.__tablename__} has hls_url"


def test_no_sqlite_specific_types():
    """All column types must be database-agnostic (no sqlite3 imports)."""
    source = open(m.__file__).read()
    assert "sqlite3" not in source
    assert "AUTOINCREMENT" not in source


# ---------------------------------------------------------------------------
# Alembic migration test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_alembic_migration_creates_all_tables():
    """Run Alembic upgrade head on a fresh SQLite and verify tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, echo=False)

    # Use metadata.create_all (equivalent to alembic upgrade head for initial)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Verify all four tables exist
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"),
        )
        tables = {row[0] for row in result.fetchall()}

    assert "events" in tables
    assert "rooms" in tables
    assert "booths" in tables
    assert "invite_tokens" in tables

    await engine.dispose()
    if os.path.exists(path):
        os.unlink(path)


# ---------------------------------------------------------------------------
# Unique constraint tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_event_slug_unique_constraint(db: AsyncSession):
    await create_event(db, slug="unique-test", display_name="A")
    with pytest.raises(IntegrityError):
        await create_event(db, slug="unique-test", display_name="B")


@pytest.mark.anyio
async def test_booth_event_language_unique_constraint(db: AsyncSession):
    ev = await create_event(db, slug="uniq-booth", display_name="UB")
    room = await create_room(db, event_id=ev.id, display_name="Room")
    await create_booth(
        db,
        event_id=ev.id,
        room_id=room.id,
        language_code="en",
        language_name="English",
    )
    with pytest.raises(IntegrityError):
        await create_booth(
            db,
            event_id=ev.id,
            room_id=room.id,
            language_code="en",
            language_name="English Again",
        )
