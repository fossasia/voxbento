"""Tests for AI vocabulary parsing and resolution."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from portal.models import AIVocabularyEntry, Base, DBBooth, Event, Room
from portal.translations.vocabulary import (
    parse_vocabulary_csv,
    resolve_vocabulary_entries,
)


@pytest.fixture
async def db():
    """In-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def test_parse_vocabulary_csv():
    """Test parsing logic, fallback behaviors, and whitespace sanitization."""
    csv_content = (
        "source_term,target_language,target_term,description,case_sensitive,match_type,priority\n"
        "Eventyay,all,Eventyay (Project),The platform name,false,phrase,100\n"
        "-O2,fr,-O2,Optimization flag,true,exact,50"
    )

    entries, warnings = parse_vocabulary_csv(csv_content)

    assert len(warnings) == 0
    assert len(entries) == 2

    # Check first row
    assert entries[0].source_term == "Eventyay"
    assert entries[0].target_term == "Eventyay (Project)"
    assert entries[0].match_type == "phrase"
    assert entries[0].priority == 100

    # Check second row (-O2 shouldn't be stripped by sanitization)
    assert entries[1].source_term == "-O2"
    assert entries[1].target_term == "-O2"
    assert entries[1].case_sensitive is True


def test_parse_vocabulary_csv_warnings():
    """Test invalid or missing columns."""
    csv_content = "source_term,target_language\nEventyay,all"
    entries, warnings = parse_vocabulary_csv(csv_content)
    assert len(entries) == 0
    assert len(warnings) == 1
    assert "Missing required column(s)" in warnings[0]


@pytest.mark.anyio
async def test_resolve_vocabulary_entries(db: AsyncSession):
    """Test resolution of vocabulary based on scope (booth > room > event) and priority."""

    # 1. Setup minimal event hierarchy
    event = Event(slug="test-event", display_name="Test Event")
    db.add(event)
    await db.commit()

    room = Room(event_id=event.id, display_name="Main Room", floor_source_language_code="en")
    db.add(room)
    await db.commit()

    booth = DBBooth(event_id=event.id, room_id=room.id, language_code="es", language_name="Spanish")
    db.add(booth)
    await db.commit()

    # 2. Add vocabulary entries
    # Event-level
    db.add(
        AIVocabularyEntry(
            event_id=event.id, source_term="API", target_term="API (Event)", priority=10, target_language="all"
        )
    )
    # Room-level (should override event-level)
    db.add(
        AIVocabularyEntry(
            event_id=event.id,
            room_id=room.id,
            source_term="API",
            target_term="API (Room)",
            priority=20,
            target_language="all",
        )
    )
    # Booth-level (should override room-level)
    db.add(
        AIVocabularyEntry(
            event_id=event.id,
            room_id=room.id,
            booth_id=booth.id,
            source_term="API",
            target_term="API (Booth)",
            priority=30,
            target_language="all",
        )
    )

    # Unrelated booth/room (should be ignored)
    db.add(
        AIVocabularyEntry(
            event_id=event.id,
            room_id=999,
            source_term="API",
            target_term="API (Other)",
            priority=99,
            target_language="all",
        )
    )

    # High priority event-level (should be included even if transcript doesn't match directly)
    db.add(
        AIVocabularyEntry(
            event_id=event.id, source_term="Voxbento", target_term="Voxbento", priority=100, target_language="all"
        )
    )

    await db.commit()

    # 3. Resolve for booth
    # Provide transcript text that contains "API" but not "Voxbento"
    resolved = await resolve_vocabulary_entries(
        db,
        event_id=event.id,
        room_id=room.id,
        booth_id=booth.id,
        target_language="es",
        transcript_text="We are discussing the new API today.",
    )

    # We expect 2 entries:
    # - "API" from Booth level (overriding room and event level)
    # - "Voxbento" from Event level (force included because priority 100 >= 90)
    assert len(resolved) == 2

    terms = {e.source_term: e.target_term for e in resolved}
    assert "API" in terms
    assert terms["API"] == "API (Booth)"

    assert "Voxbento" in terms
    assert terms["Voxbento"] == "Voxbento"
