from __future__ import annotations

import pytest

from portal.database import get_session
from portal.models import AIVocabularyEntry, Event, Room
from portal.translations.prompts import build_interpretation_messages
from portal.translations.vocabulary import parse_vocabulary_csv, resolve_vocabulary_entries


def test_prompt_builder_combines_persona_style_and_vocabulary():
    entry = AIVocabularyEntry(
        event_id=1,
        source_term="Voxbento",
        target_language="all",
        target_term="Voxbento",
        description="Product name",
    )
    messages = build_interpretation_messages(
        source_language_name="English",
        target_language_name="German",
        text="Welcome to Voxbento",
        persona="A technical conference interpreter.",
        style="Use formal language.",
        vocabulary_entries=[entry],
    )

    assert messages[1] == {"role": "user", "content": "Welcome to Voxbento"}
    system = messages[0]["content"]
    assert "AI interpretation engine for live events" in system
    assert "A technical conference interpreter." in system
    assert "Use formal language." in system
    assert "Voxbento -> Voxbento (Product name)" in system


def test_csv_parser_reports_invalid_and_duplicate_rows():
    content = """source_term,target_language,target_term,case_sensitive,match_type,priority
Voxbento,all,Voxbento,true,exact,100
Voxbento,all,Vox Bento,false,phrase,10
Unsafe,de,=CMD(),false,phrase,0
WebRTC,zz,WebRTC,false,phrase,90
"""
    entries, warnings = parse_vocabulary_csv(content)

    assert [entry.source_term for entry in entries] == ["Voxbento"]
    assert any("duplicate term" in warning for warning in warnings)
    assert any("spreadsheet formula" in warning for warning in warnings)
    assert any("unsupported target_language" in warning for warning in warnings)


@pytest.mark.anyio
async def test_resolver_prefers_room_entries_and_limits_prompt_size():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="ai-prompt", display_name="AI Prompt")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Main Hall")
            session.add(room)
            await session.flush()
            session.add_all(
                [
                    AIVocabularyEntry(
                        event_id=event.id,
                        source_term="Voxbento",
                        target_language="all",
                        target_term="Event Voxbento",
                        priority=100,
                    ),
                    AIVocabularyEntry(
                        event_id=event.id,
                        room_id=room.id,
                        source_term="Voxbento",
                        target_language="all",
                        target_term="Room Voxbento",
                        priority=100,
                    ),
                    AIVocabularyEntry(
                        event_id=event.id,
                        source_term="WebRTC",
                        target_language="de",
                        target_term="WebRTC",
                        priority=0,
                    ),
                    AIVocabularyEntry(
                        event_id=event.id,
                        source_term="MediaMTX",
                        target_language="de",
                        target_term="MediaMTX",
                        priority=0,
                    ),
                ]
            )
            await session.flush()
            entries = await resolve_vocabulary_entries(
                session,
                event_id=event.id,
                room_id=room.id,
                booth_id=None,
                target_language="de",
                transcript_text="Voxbento uses WebRTC",
                max_entries=2,
            )

        assert [(entry.source_term, entry.target_term) for entry in entries] == [
            ("Voxbento", "Room Voxbento"),
            ("WebRTC", "WebRTC"),
        ]
    finally:
        await dispose()
