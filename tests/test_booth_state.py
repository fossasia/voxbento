from __future__ import annotations

import pytest

from portal.booth_state import BoothRegistry


async def join(registry, name, role='interpreter'):
    participant, _ = await registry.join_participant(
        booth_id='hall-a-fr',
        display_name=name,
        role=role,
        language='French',
        channel_id='hall-a-fr-audio',
    )
    return participant


async def join_identity(registry, name, role='interpreter', booth_id='pycon2026-en'):
    """Join helper using a booth ID that follows the new identity scheme."""
    participant, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name=name,
        role=role,
        language='English',
        channel_id=f'{booth_id}-audio',
    )
    return participant


@pytest.mark.anyio
async def test_first_interpreter_becomes_active():
    registry = BoothRegistry()
    interpreter = await join(registry, 'Interpreter A')

    state = await registry.snapshot('hall-a-fr', 'French', 'hall-a-fr-audio')

    assert state['active_interpreter_id'] == interpreter.participant_id


@pytest.mark.anyio
async def test_active_interpreter_can_pass_relay_and_old_publisher_is_cleared():
    registry = BoothRegistry()
    interpreter_a = await join(registry, 'Interpreter A')
    interpreter_b = await join(registry, 'Interpreter B')
    await registry.update_participant_state(
        'hall-a-fr',
        interpreter_a.participant_id,
        'French',
        'hall-a-fr-audio',
        mic_active=True,
        ingest_connected=True,
    )

    state = await registry.set_active_interpreter(
        'hall-a-fr',
        interpreter_a.participant_id,
        interpreter_b.participant_id,
        'French',
        'hall-a-fr-audio',
    )

    participants = {p['participant_id']: p for p in state['participants']}
    assert state['active_interpreter_id'] == interpreter_b.participant_id
    assert state['ingest_status'] == 'disconnected'
    assert participants[interpreter_a.participant_id]['mic_active'] is False
    assert participants[interpreter_a.participant_id]['ingest_connected'] is False


@pytest.mark.anyio
async def test_standby_interpreter_cannot_reassign_another_interpreter():
    registry = BoothRegistry()
    await join(registry, 'Interpreter A')
    interpreter_b = await join(registry, 'Interpreter B')
    interpreter_c = await join(registry, 'Interpreter C')

    with pytest.raises(PermissionError):
        await registry.set_active_interpreter(
            'hall-a-fr',
            interpreter_b.participant_id,
            interpreter_c.participant_id,
            'French',
            'hall-a-fr-audio',
        )


@pytest.mark.anyio
async def test_standby_interpreter_cannot_mark_ingest_connected():
    registry = BoothRegistry()
    await join(registry, 'Interpreter A')
    interpreter_b = await join(registry, 'Interpreter B')

    with pytest.raises(PermissionError):
        await registry.update_participant_state(
            'hall-a-fr',
            interpreter_b.participant_id,
            'French',
            'hall-a-fr-audio',
            mic_active=True,
            ingest_connected=True,
        )


@pytest.mark.anyio
async def test_coordinator_can_assign_active_interpreter():
    registry = BoothRegistry()
    await join(registry, 'Interpreter A')
    interpreter_b = await join(registry, 'Interpreter B')
    coordinator = await join(registry, 'Coordinator', role='coordinator')

    state = await registry.set_active_interpreter(
        'hall-a-fr',
        coordinator.participant_id,
        interpreter_b.participant_id,
        'French',
        'hall-a-fr-audio',
    )

    assert state['active_interpreter_id'] == interpreter_b.participant_id


# ── Booth identity scheme tests ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_create_booth_sets_identity_fields():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='pycon2026',
        language_code='en',
        language='English',
    )

    assert state['booth_id'] == 'pycon2026-en'
    assert state['event_slug'] == 'pycon2026'
    assert state['language_code'] == 'en'
    assert state['instance'] == 'primary'
    assert state['mediamtx_path'] == 'pycon2026/en'
    assert state['channel_id'] == 'pycon2026/en'
    assert state['room_id'] is None


@pytest.mark.anyio
async def test_create_booth_custom_channel_and_instance():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='fossasia2026',
        language_code='fr',
        language='French',
        channel_id='custom-channel',
        instance='backup',
    )

    assert state['booth_id'] == 'fossasia2026-fr'
    assert state['instance'] == 'backup'
    assert state['channel_id'] == 'custom-channel'
    assert state['mediamtx_path'] == 'fossasia2026/fr'


@pytest.mark.anyio
async def test_create_booth_rejects_duplicate():
    registry = BoothRegistry()
    await registry.create_booth(
        event_slug='pycon2026', language_code='en', language='English',
    )

    with pytest.raises(ValueError, match='already exists'):
        await registry.create_booth(
            event_slug='pycon2026', language_code='en', language='English',
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_slug():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            event_slug='--bad--', language_code='en', language='English',
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_language_code():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            event_slug='pycon2026', language_code='xyz', language='English',
        )


@pytest.mark.anyio
async def test_get_or_create_parses_identity_from_booth_id():
    """When a booth is auto-created via snapshot with a valid ID, it should
    have identity fields populated."""
    registry = BoothRegistry()
    state = await registry.snapshot('pycon2026-en', 'English', 'pycon2026-en-audio')

    assert state['event_slug'] == 'pycon2026'
    assert state['language_code'] == 'en'
    assert state['mediamtx_path'] == 'pycon2026/en'


@pytest.mark.anyio
async def test_get_or_create_handles_legacy_booth_id():
    """Legacy free-form booth IDs that don't match the identity scheme
    should still work with empty identity fields."""
    registry = BoothRegistry()
    state = await registry.snapshot('hall-a-fr', 'French', 'hall-a-fr-audio')

    assert state['booth_id'] == 'hall-a-fr'
    # 'fr' is a valid ISO 639-1 code, so it should parse
    assert state['event_slug'] == 'hall-a'
    assert state['language_code'] == 'fr'


@pytest.mark.anyio
async def test_identity_booth_join_and_snapshot():
    """Full flow: create booth via identity, join, verify snapshot."""
    registry = BoothRegistry()
    await registry.create_booth(
        event_slug='pycon2026', language_code='de', language='German',
    )

    participant = await join_identity(
        registry, 'Interpreter A', booth_id='pycon2026-de',
    )
    state = await registry.snapshot('pycon2026-de', 'German', 'pycon2026-de-audio')

    assert state['active_interpreter_id'] == participant.participant_id
    assert state['event_slug'] == 'pycon2026'
    assert state['language_code'] == 'de'
    assert len(state['participants']) == 1


# ── Event and room field tests ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_create_booth_with_room_id():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='pycon2026',
        language_code='en',
        language='English',
        room_id=42,
    )

    assert state['room_id'] == 42
    assert state['event_slug'] == 'pycon2026'
    assert state['channel_id'] == 'pycon2026/en'


@pytest.mark.anyio
async def test_create_booth_room_id_defaults_to_none():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='fossasia2026',
        language_code='fr',
        language='French',
    )

    assert state['room_id'] is None


@pytest.mark.anyio
async def test_channel_id_defaults_to_mediamtx_path():
    """When no explicit channel_id is given, it should equal the MediaMTX path."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='pycon2026',
        language_code='de',
        language='German',
    )

    assert state['channel_id'] == 'pycon2026/de'
    assert state['channel_id'] == state['mediamtx_path']


@pytest.mark.anyio
async def test_channel_id_explicit_overrides_default():
    """An explicit channel_id should override the mediamtx_path default."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='pycon2026',
        language_code='es',
        language='Spanish',
        channel_id='custom-channel',
    )

    assert state['channel_id'] == 'custom-channel'
    assert state['mediamtx_path'] == 'pycon2026/es'


@pytest.mark.anyio
async def test_snapshot_passes_room_id_on_creation():
    """room_id passed via snapshot should be stored on initial booth creation."""
    registry = BoothRegistry()
    state = await registry.snapshot('pycon2026-en', 'English', 'pycon2026/en', room_id=7)

    assert state['room_id'] == 7
    assert state['event_slug'] == 'pycon2026'


@pytest.mark.anyio
async def test_snapshot_room_id_immutable_after_creation():
    """room_id from a later snapshot call must not overwrite the original."""
    registry = BoothRegistry()
    await registry.snapshot('pycon2026-en', 'English', 'pycon2026/en', room_id=7)
    state = await registry.snapshot('pycon2026-en', 'English', 'pycon2026/en', room_id=99)

    assert state['room_id'] == 7


@pytest.mark.anyio
async def test_join_participant_passes_room_id():
    """room_id passed via join_participant creates the booth with that room."""
    registry = BoothRegistry()
    _, state = await registry.join_participant(
        booth_id='pycon2026-en',
        display_name='Interpreter A',
        role='interpreter',
        language='English',
        channel_id='pycon2026/en',
        room_id=15,
    )

    assert state['room_id'] == 15


@pytest.mark.anyio
async def test_legacy_booth_has_no_room_id():
    """Legacy booths auto-created without room_id should default to None."""
    registry = BoothRegistry()
    state = await registry.snapshot('hall-a-fr', 'French', 'hall-a-fr-audio')

    assert state['room_id'] is None


@pytest.mark.anyio
async def test_as_public_dict_includes_room_id():
    """as_public_dict must always include room_id in serialized output."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug='pycon2026',
        language_code='ja',
        language='Japanese',
        room_id=100,
    )

    assert 'room_id' in state
    assert state['room_id'] == 100
