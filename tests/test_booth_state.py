from __future__ import annotations

import pytest

from portal.booth_state import BoothRegistry


async def join(registry, name, role="interpreter"):
    participant, _ = await registry.join_participant(
        booth_id="hall-a-fr",
        display_name=name,
        role=role,
        language="French",
        channel_id="hall-a-fr-audio",
    )
    return participant


async def join_identity(registry, name, role="interpreter", booth_id="pycon2026-en"):
    """Join helper using a booth ID that follows the new identity scheme."""
    participant, _ = await registry.join_participant(
        booth_id=booth_id,
        display_name=name,
        role=role,
        language="English",
        channel_id=f"{booth_id}-audio",
    )
    return participant


@pytest.mark.anyio
async def test_first_interpreter_becomes_active():
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    state = await registry.snapshot("hall-a-fr", "French", "hall-a-fr-audio")

    assert state["active_interpreter_id"] == interpreter.participant_id


@pytest.mark.anyio
async def test_active_interpreter_can_pass_relay_and_old_publisher_is_cleared():
    registry = BoothRegistry()
    interpreter_a = await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    await registry.update_participant_state(
        "hall-a-fr",
        interpreter_a.participant_id,
        "French",
        "hall-a-fr-audio",
        mic_active=True,
        ingest_connected=True,
    )

    state = await registry.set_active_interpreter(
        "hall-a-fr",
        interpreter_a.participant_id,
        interpreter_b.participant_id,
        "French",
        "hall-a-fr-audio",
    )

    participants = {p["participant_id"]: p for p in state["participants"]}
    assert state["active_interpreter_id"] == interpreter_b.participant_id
    assert state["ingest_status"] == "disconnected"
    assert participants[interpreter_a.participant_id]["mic_active"] is False
    assert participants[interpreter_a.participant_id]["ingest_connected"] is False


@pytest.mark.anyio
async def test_standby_interpreter_cannot_reassign_another_interpreter():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    interpreter_c = await join(registry, "Interpreter C")

    with pytest.raises(PermissionError):
        await registry.set_active_interpreter(
            "hall-a-fr",
            interpreter_b.participant_id,
            interpreter_c.participant_id,
            "French",
            "hall-a-fr-audio",
        )


@pytest.mark.anyio
async def test_standby_interpreter_cannot_mark_ingest_connected():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")

    with pytest.raises(PermissionError):
        await registry.update_participant_state(
            "hall-a-fr",
            interpreter_b.participant_id,
            "French",
            "hall-a-fr-audio",
            mic_active=True,
            ingest_connected=True,
        )


@pytest.mark.anyio
async def test_coordinator_can_assign_active_interpreter():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    state = await registry.set_active_interpreter(
        "hall-a-fr",
        coordinator.participant_id,
        interpreter_b.participant_id,
        "French",
        "hall-a-fr-audio",
    )

    assert state["active_interpreter_id"] == interpreter_b.participant_id


# ── Booth identity scheme tests ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_booth_sets_identity_fields():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="pycon2026",
        language_code="en",
        language="English",
    )

    assert state["booth_id"] == "pycon2026-en"
    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "en"
    assert state["instance"] == "primary"
    assert state["mediamtx_path"] == "pycon2026/en"
    assert state["channel_id"] == "pycon2026/en"
    assert state["room_id"] is None


@pytest.mark.anyio
async def test_create_booth_custom_channel_and_instance():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="fossasia2026",
        language_code="fr",
        language="French",
        channel_id="custom-channel",
        instance="backup",
    )

    assert state["booth_id"] == "fossasia2026-fr"
    assert state["instance"] == "backup"
    assert state["channel_id"] == "custom-channel"
    assert state["mediamtx_path"] == "fossasia2026/fr"


@pytest.mark.anyio
async def test_create_booth_rejects_duplicate():
    registry = BoothRegistry()
    await registry.create_booth(
        event_slug="pycon2026",
        language_code="en",
        language="English",
    )

    with pytest.raises(ValueError, match="already exists"):
        await registry.create_booth(
            event_slug="pycon2026",
            language_code="en",
            language="English",
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_slug():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            event_slug="--bad--",
            language_code="en",
            language="English",
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_language_code():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            event_slug="pycon2026",
            language_code="xyz",
            language="English",
        )


@pytest.mark.anyio
async def test_get_or_create_parses_identity_from_booth_id():
    """When a booth is auto-created via snapshot with a valid ID, it should
    have identity fields populated."""
    registry = BoothRegistry()
    state = await registry.snapshot("pycon2026-en", "English", "pycon2026-en-audio")

    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "en"
    assert state["mediamtx_path"] == "pycon2026/en"


@pytest.mark.anyio
async def test_get_or_create_handles_legacy_booth_id():
    """Legacy free-form booth IDs that don't match the identity scheme
    should still work with empty identity fields."""
    registry = BoothRegistry()
    state = await registry.snapshot("hall-a-fr", "French", "hall-a-fr-audio")

    assert state["booth_id"] == "hall-a-fr"
    # 'fr' is a valid ISO 639-1 code, so it should parse
    assert state["event_slug"] == "hall-a"
    assert state["language_code"] == "fr"


@pytest.mark.anyio
async def test_identity_booth_join_and_snapshot():
    """Full flow: create booth via identity, join, verify snapshot."""
    registry = BoothRegistry()
    await registry.create_booth(
        event_slug="pycon2026",
        language_code="de",
        language="German",
    )

    participant = await join_identity(
        registry,
        "Interpreter A",
        booth_id="pycon2026-de",
    )
    state = await registry.snapshot("pycon2026-de", "German", "pycon2026-de-audio")

    assert state["active_interpreter_id"] == participant.participant_id
    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "de"
    assert len(state["participants"]) == 1


# ── Event and room field tests ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_booth_with_room_id():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="pycon2026",
        language_code="en",
        language="English",
        room_id=42,
    )

    assert state["room_id"] == 42
    assert state["event_slug"] == "pycon2026"
    assert state["channel_id"] == "pycon2026/en"


@pytest.mark.anyio
async def test_create_booth_room_id_defaults_to_none():
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="fossasia2026",
        language_code="fr",
        language="French",
    )

    assert state["room_id"] is None


@pytest.mark.anyio
async def test_channel_id_defaults_to_mediamtx_path():
    """When no explicit channel_id is given, it should equal the MediaMTX path."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="pycon2026",
        language_code="de",
        language="German",
    )

    assert state["channel_id"] == "pycon2026/de"
    assert state["channel_id"] == state["mediamtx_path"]


@pytest.mark.anyio
async def test_channel_id_explicit_overrides_default():
    """An explicit channel_id should override the mediamtx_path default."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="pycon2026",
        language_code="es",
        language="Spanish",
        channel_id="custom-channel",
    )

    assert state["channel_id"] == "custom-channel"
    assert state["mediamtx_path"] == "pycon2026/es"


@pytest.mark.anyio
async def test_snapshot_passes_room_id_on_creation():
    """room_id passed via snapshot should be stored on initial booth creation."""
    registry = BoothRegistry()
    state = await registry.snapshot("pycon2026-en", "English", "pycon2026/en", room_id=7)

    assert state["room_id"] == 7
    assert state["event_slug"] == "pycon2026"


@pytest.mark.anyio
async def test_snapshot_room_id_immutable_after_creation():
    """room_id from a later snapshot call must not overwrite the original."""
    registry = BoothRegistry()
    await registry.snapshot("pycon2026-en", "English", "pycon2026/en", room_id=7)
    state = await registry.snapshot("pycon2026-en", "English", "pycon2026/en", room_id=99)

    assert state["room_id"] == 7


@pytest.mark.anyio
async def test_join_participant_passes_room_id():
    """room_id passed via join_participant creates the booth with that room."""
    registry = BoothRegistry()
    _, state = await registry.join_participant(
        booth_id="pycon2026-en",
        display_name="Interpreter A",
        role="interpreter",
        language="English",
        channel_id="pycon2026/en",
        room_id=15,
    )

    assert state["room_id"] == 15


@pytest.mark.anyio
async def test_legacy_booth_has_no_room_id():
    """Legacy booths auto-created without room_id should default to None."""
    registry = BoothRegistry()
    state = await registry.snapshot("hall-a-fr", "French", "hall-a-fr-audio")

    assert state["room_id"] is None


@pytest.mark.anyio
async def test_as_public_dict_includes_room_id():
    """as_public_dict must always include room_id in serialized output."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        event_slug="pycon2026",
        language_code="ja",
        language="Japanese",
        room_id=100,
    )

    assert "room_id" in state
    assert state["room_id"] == 100


# ── Layer 1: active-interpreter enforcement tests ─────────────────────────────


@pytest.mark.anyio
async def test_coordinator_cannot_set_mic_active_when_not_active():
    """A non-active coordinator must not mark mic active (active-interpreter guard)."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")  # becomes active
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.update_participant_state(
            "hall-a-fr",
            coordinator.participant_id,
            "French",
            "hall-a-fr-audio",
            mic_active=True,
        )


@pytest.mark.anyio
async def test_room_coordinator_cannot_set_ingest_connected_when_not_active():
    """A non-active coordinator must not mark ingest connected (active-interpreter guard)."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")  # becomes active
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.update_participant_state(
            "hall-a-fr",
            coordinator.participant_id,
            "French",
            "hall-a-fr-audio",
            ingest_connected=True,
        )


@pytest.mark.anyio
async def test_check_publish_permission_active_interpreter_passes():
    """Active interpreter passes the publish permission check."""
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    # Should not raise
    await registry.check_publish_permission(
        "hall-a-fr",
        interpreter.participant_id,
        "French",
        "hall-a-fr-audio",
    )


@pytest.mark.anyio
async def test_check_publish_permission_standby_interpreter_rejected():
    """Standby interpreter fails the publish permission check."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.check_publish_permission(
            "hall-a-fr",
            interpreter_b.participant_id,
            "French",
            "hall-a-fr-audio",
        )


@pytest.mark.anyio
async def test_check_publish_permission_active_coordinator_passes():
    """An active coordinator (BOOTH_GO_LIVE permission) passes the publish check."""
    registry = BoothRegistry()
    coordinator = await join(registry, "Coordinator", role="room_coordinator")
    # Coordinator is auto-assigned active on first join
    await registry.check_publish_permission(
        "hall-a-fr",
        coordinator.participant_id,
        "French",
        "hall-a-fr-audio",
    )  # must not raise


@pytest.mark.anyio
async def test_check_publish_permission_standby_coordinator_rejected():
    """A non-active coordinator fails the publish check (active-interpreter guard)."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")  # becomes active
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.check_publish_permission(
            "hall-a-fr",
            coordinator.participant_id,
            "French",
            "hall-a-fr-audio",
        )


@pytest.mark.anyio
async def test_check_publish_permission_unknown_participant():
    """Unknown participant_id raises ValueError."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")

    with pytest.raises(ValueError, match="does not exist"):
        await registry.check_publish_permission(
            "hall-a-fr",
            "unknown-id",
            "French",
            "hall-a-fr-audio",
        )


@pytest.mark.anyio
async def test_active_interpreter_can_turn_off_mic():
    """Active interpreter can set mic_active=False (non-publisher state)."""
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    await registry.update_participant_state(
        "hall-a-fr",
        interpreter.participant_id,
        "French",
        "hall-a-fr-audio",
        mic_active=True,
    )
    state = await registry.update_participant_state(
        "hall-a-fr",
        interpreter.participant_id,
        "French",
        "hall-a-fr-audio",
        mic_active=False,
    )

    participants = {p["participant_id"]: p for p in state["participants"]}
    assert participants[interpreter.participant_id]["mic_active"] is False


# ── Booth listing tests ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_booths_for_event_returns_matching():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="en", language="English")
    await registry.create_booth(event_slug="pycon2026", language_code="fr", language="French")
    await registry.create_booth(event_slug="fossasia", language_code="en", language="English")

    result = await registry.list_booths_for_event("pycon2026")

    assert len(result) == 2
    slugs = {b["language_code"] for b in result}
    assert slugs == {"en", "fr"}


@pytest.mark.anyio
async def test_list_booths_for_event_empty():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="en", language="English")

    result = await registry.list_booths_for_event("nonexistent")

    assert result == []


# ── Namespace isolation (get_booth / get_booth_for_event / validate_booth_event) ──


@pytest.mark.anyio
async def test_get_booth_returns_existing():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth("pycon2026-en")
    assert result is not None
    assert result["booth_id"] == "pycon2026-en"


@pytest.mark.anyio
async def test_get_booth_returns_none_for_missing():
    registry = BoothRegistry()
    result = await registry.get_booth("nonexistent-booth")
    assert result is None


@pytest.mark.anyio
async def test_get_booth_for_event_returns_matching():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="fr", language="French")
    result = await registry.get_booth_for_event("pycon2026", "fr")
    assert result is not None
    assert result["booth_id"] == "pycon2026-fr"
    assert result["event_slug"] == "pycon2026"
    assert result["language_code"] == "fr"


@pytest.mark.anyio
async def test_get_booth_for_event_returns_none_wrong_event():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth_for_event("fossasia", "en")
    assert result is None


@pytest.mark.anyio
async def test_get_booth_for_event_returns_none_wrong_language():
    registry = BoothRegistry()
    await registry.create_booth(event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth_for_event("pycon2026", "de")
    assert result is None


@pytest.mark.anyio
async def test_validate_booth_event_passes_for_correct_event():
    registry = BoothRegistry()
    # Should not raise
    await registry.validate_booth_event("pycon2026-en", "pycon2026")


@pytest.mark.anyio
async def test_validate_booth_event_rejects_wrong_event():
    registry = BoothRegistry()
    with pytest.raises(PermissionError, match="does not belong"):
        await registry.validate_booth_event("pycon2026-en", "fossasia")


@pytest.mark.anyio
async def test_validate_booth_event_rejects_malformed_id():
    registry = BoothRegistry()
    with pytest.raises(PermissionError, match="does not belong"):
        await registry.validate_booth_event("bad", "pycon2026")


@pytest.mark.anyio
async def test_cross_event_isolation_booths_invisible():
    """Booths from event A must not appear in event B listings."""
    registry = BoothRegistry()
    await registry.create_booth(event_slug="eventa", language_code="en", language="English")
    await registry.create_booth(event_slug="eventa", language_code="fr", language="French")
    await registry.create_booth(event_slug="eventb", language_code="de", language="German")

    a_booths = await registry.list_booths_for_event("eventa")
    b_booths = await registry.list_booths_for_event("eventb")

    assert len(a_booths) == 2
    assert len(b_booths) == 1
    assert all(b["event_slug"] == "eventa" for b in a_booths)
    assert all(b["event_slug"] == "eventb" for b in b_booths)

    # get_booth_for_event also isolates
    assert await registry.get_booth_for_event("eventa", "de") is None
    assert await registry.get_booth_for_event("eventb", "en") is None
    assert (await registry.get_booth_for_event("eventb", "de"))["booth_id"] == "eventb-de"


@pytest.mark.anyio
async def test_cross_event_participants_isolated():
    """Participants in event A booth must not appear in event B booth."""
    registry = BoothRegistry()
    await registry.create_booth(event_slug="eventa", language_code="en", language="English")
    await registry.create_booth(event_slug="eventb", language_code="en", language="English")

    await registry.join_participant(
        booth_id="eventa-en",
        display_name="Alice",
        role="interpreter",
        language="English",
        channel_id="eventa/en",
    )

    a_state = await registry.get_booth("eventa-en")
    b_state = await registry.get_booth("eventb-en")
    assert len(a_state["participants"]) == 1
    assert len(b_state["participants"]) == 0
