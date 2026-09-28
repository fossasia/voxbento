from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import portal.globals as pg
from portal.database import get_session
from portal.models import AIVocabularyEntry, Event, Room
from portal.translations.prompts import build_interpretation_messages
from portal.translations.providers.anthropic import AnthropicProvider
from portal.translations.providers.gemini import GeminiProvider
from portal.translations.providers.openai import OpenAIProvider
from portal.translations.vocabulary import (
    VocabularyEntryInput,
    VocabularyOverlapIndex,
    _term_matches,
    parse_vocabulary_csv,
    resolve_vocabulary_entries,
    vocabulary_terms_overlap,
)


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


def test_exact_match_type_requires_word_boundaries():
    def entry(term, match_type, case_sensitive=False):
        return AIVocabularyEntry(
            event_id=1,
            source_term=term,
            target_language="de",
            target_term=term,
            case_sensitive=case_sensitive,
            match_type=match_type,
            priority=0,
        )

    assert _term_matches(entry("US", "exact", case_sensitive=True), "We ship to the US today")
    assert not _term_matches(entry("US", "exact", case_sensitive=True), "Plug in the USB cable")
    assert not _term_matches(entry("us", "exact"), "Stay focused on the business")
    assert _term_matches(entry("us", "exact"), "Come with US now")
    # phrase entries keep matching inside longer words
    assert _term_matches(entry("US", "phrase", case_sensitive=True), "Plug in the USB cable")
    # a high-priority entry is always included, whatever its match type
    always = entry("US", "exact", case_sensitive=True)
    always.priority = 100
    assert _term_matches(always, "nothing relevant here")


def test_overlap_index_matches_the_pairwise_rule():
    import itertools

    def make(term, case_sensitive, language):
        return VocabularyEntryInput(
            source_term=term, target_language=language, target_term="x", case_sensitive=case_sensitive
        )

    combinations = list(itertools.product(["US", "us", "Us", "USA"], [True, False], ["de", "fr"]))
    for first in combinations:
        for second in combinations:
            left, right = make(*first), make(*second)
            index = VocabularyOverlapIndex()
            index.add(left)
            expected = left.target_language == right.target_language and vocabulary_terms_overlap(left, right)
            assert index.conflicts(right) is expected, (first, second)


def test_csv_parser_stays_linear_on_large_uploads():
    # The 2 MB upload cap allows roughly 285k rows; a pairwise scan would not finish.
    rows = "".join(f"term{i},de,ziel{i}\n" for i in range(20000))
    entries, warnings = parse_vocabulary_csv("source_term,target_language,target_term\n" + rows)

    assert len(entries) == 20000
    assert warnings == []


def test_csv_parser_reports_invalid_and_duplicate_rows():
    content = """source_term,target_language,target_term,case_sensitive,match_type,priority
Voxbento,all,Voxbento,false,exact,100
Voxbento,all,Vox Bento,false,phrase,10
US,de,US,true,exact,99
us,de,uns,true,exact,98
Us,de,wir,false,exact,97
Unsafe,de,=CMD(),false,phrase,0
WebRTC,zz,WebRTC,false,phrase,90
"""
    entries, warnings = parse_vocabulary_csv(content)

    assert [entry.source_term for entry in entries] == ["Voxbento", "US", "us"]
    assert any("duplicate term" in warning for warning in warnings)
    assert any("duplicate term 'Us'" in warning for warning in warnings)
    assert any("spreadsheet formula" in warning for warning in warnings)
    assert any("unsupported target_language" in warning for warning in warnings)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider", "response", "system_path"),
    [
        (OpenAIProvider(), {"choices": [{"message": {"content": "Hallo"}}]}, ("messages", 0, "content")),
        (
            GeminiProvider(),
            {"candidates": [{"content": {"parts": [{"text": "Hallo"}]}}]},
            ("systemInstruction", "parts", 0, "text"),
        ),
        (AnthropicProvider(), {"content": [{"text": "Hallo"}]}, ("system",)),
    ],
)
async def test_cloud_providers_use_shared_prompt(provider, response, system_path):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post = AsyncMock(return_value=mock_response)
    pg.shared_http_client = mock_client

    result = await provider.translate(
        provider_name="openai",
        text="Welcome",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model="test-model",
        api_key="test-key",
        persona="Technical interpreter",
        style="Formal",
    )

    assert result == "Hallo"
    payload = mock_client.post.call_args.kwargs["json"]
    prompt = payload
    for key in system_path:
        prompt = prompt[key]
    assert "Technical interpreter" in prompt
    assert "Formal" in prompt


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
                        target_language="de",
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
                        source_term="US",
                        target_language="all",
                        target_term="US",
                        case_sensitive=True,
                        priority=99,
                    ),
                    AIVocabularyEntry(
                        event_id=event.id,
                        source_term="us",
                        target_language="de",
                        target_term="uns",
                        case_sensitive=True,
                        priority=98,
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
                transcript_text="Voxbento brings US and us together over WebRTC",
                max_entries=4,
            )

        assert [(entry.source_term, entry.target_term) for entry in entries] == [
            ("Voxbento", "Room Voxbento"),
            ("US", "US"),
            ("us", "uns"),
            ("WebRTC", "WebRTC"),
        ]
    finally:
        await dispose()
