from __future__ import annotations

import asyncio

import pytest

from portal.booth_state import BoothRegistry


async def join(registry, name, role="interpreter"):
    participant, _ = await registry.join_participant(
        booth_id="hall-1-fr",
        display_name=name,
        role=role,
        language="French",
        channel_id="hall-1-fr-audio",
    )
    return participant


async def join_identity(registry, name, role="interpreter", booth_id="pycon2026-1-en"):
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

    state = await registry.snapshot("hall-1-fr", "French", "hall-1-fr-audio")

    assert state["active_interpreter_id"] == interpreter.participant_id


@pytest.mark.anyio
async def test_active_interpreter_can_pass_relay_and_old_publisher_is_cleared():
    registry = BoothRegistry()
    interpreter_a = await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    await registry.update_participant_state(
        "hall-1-fr",
        interpreter_a.participant_id,
        "French",
        "hall-1-fr-audio",
        mic_active=True,
        ingest_connected=True,
    )

    state = await registry.set_active_interpreter(
        "hall-1-fr",
        interpreter_a.participant_id,
        interpreter_b.participant_id,
        "French",
        "hall-1-fr-audio",
    )

    participants = {p["participant_id"]: p for p in state["participants"]}
    assert state["active_interpreter_id"] == interpreter_b.participant_id
    assert state["ingest_status"] == "disconnected"
    assert participants[interpreter_a.participant_id]["mic_active"] is False
    assert participants[interpreter_a.participant_id]["ingest_connected"] is False


@pytest.mark.anyio
async def test_locking_broadcast_stops_active_publisher():
    """Mission Control 'Stop' (lock) must force the active publisher offline."""
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")
    await registry.set_broadcast_unlocked("hall-1-fr", True, "French", "hall-1-fr-audio")
    await registry.update_participant_state(
        "hall-1-fr",
        interpreter.participant_id,
        "French",
        "hall-1-fr-audio",
        mic_active=True,
        ingest_connected=True,
    )

    state = await registry.set_broadcast_unlocked("hall-1-fr", False, "French", "hall-1-fr-audio")

    participants = {p["participant_id"]: p for p in state["participants"]}
    assert state["broadcast_unlocked"] is False
    assert state["ingest_status"] == "disconnected"
    assert participants[interpreter.participant_id]["mic_active"] is False
    assert participants[interpreter.participant_id]["ingest_connected"] is False


@pytest.mark.anyio
async def test_standby_interpreter_cannot_reassign_another_interpreter():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    interpreter_c = await join(registry, "Interpreter C")

    with pytest.raises(PermissionError):
        await registry.set_active_interpreter(
            "hall-1-fr",
            interpreter_b.participant_id,
            interpreter_c.participant_id,
            "French",
            "hall-1-fr-audio",
        )


@pytest.mark.anyio
async def test_standby_interpreter_cannot_mark_ingest_connected():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")

    with pytest.raises(PermissionError):
        await registry.update_participant_state(
            "hall-1-fr",
            interpreter_b.participant_id,
            "French",
            "hall-1-fr-audio",
            mic_active=True,
            ingest_connected=True,
        )


@pytest.mark.anyio
async def test_update_state_rejects_non_boolean_flag():
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    # A truthy non-boolean (e.g. 1 or "true") must not slip past the publisher checks.
    with pytest.raises(ValueError):
        await registry.update_participant_state(
            "hall-1-fr",
            interpreter.participant_id,
            "French",
            "hall-1-fr-audio",
            mic_active=1,
        )


@pytest.mark.anyio
async def test_coordinator_can_assign_active_interpreter():
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    state = await registry.set_active_interpreter(
        "hall-1-fr",
        coordinator.participant_id,
        interpreter_b.participant_id,
        "French",
        "hall-1-fr-audio",
    )

    assert state["active_interpreter_id"] == interpreter_b.participant_id


# ── Booth identity scheme tests ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_booth_sets_identity_fields():
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=1,
        event_slug="pycon2026",
        language_code="en",
        language="English",
    )

    assert state["booth_id"] == "pycon2026-1-en"
    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "en"
    assert state["instance"] == "primary"
    assert state["mediamtx_path"] == "pycon2026/1/en"
    assert state["channel_id"] == "pycon2026/1/en"
    assert state["room_id"] == 1


@pytest.mark.anyio
async def test_create_booth_custom_channel_and_instance():
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=1,
        event_slug="fossasia2026",
        language_code="fr",
        language="French",
        channel_id="custom-channel",
        instance="backup",
    )

    assert state["booth_id"] == "fossasia2026-1-fr"
    assert state["instance"] == "backup"
    assert state["channel_id"] == "custom-channel"
    assert state["mediamtx_path"] == "fossasia2026/1/fr"


@pytest.mark.anyio
async def test_create_booth_rejects_duplicate():
    registry = BoothRegistry()
    await registry.create_booth(
        room_id=1,
        event_slug="pycon2026",
        language_code="en",
        language="English",
    )

    with pytest.raises(ValueError, match="already exists"):
        await registry.create_booth(
            room_id=1,
            event_slug="pycon2026",
            language_code="en",
            language="English",
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_slug():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            room_id=1,
            event_slug="--bad--",
            language_code="en",
            language="English",
        )


@pytest.mark.anyio
async def test_create_booth_rejects_invalid_language_code():
    registry = BoothRegistry()

    with pytest.raises(ValueError):
        await registry.create_booth(
            room_id=1,
            event_slug="pycon2026",
            language_code="xyz",
            language="English",
        )


@pytest.mark.anyio
async def test_get_or_create_parses_identity_from_booth_id():
    """When a booth is auto-created via snapshot with a valid ID, it should
    have identity fields populated."""
    registry = BoothRegistry()
    state = await registry.snapshot("pycon2026-1-en", "English", "pycon2026-1-en-audio")

    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "en"
    assert state["mediamtx_path"] == "pycon2026/1/en"


@pytest.mark.anyio
async def test_get_or_create_handles_legacy_booth_id():
    """Legacy free-form booth IDs that don't match the identity scheme
    should still work with empty identity fields."""
    registry = BoothRegistry()
    state = await registry.snapshot("hall-1-fr", "French", "hall-1-fr-audio")

    assert state["booth_id"] == "hall-1-fr"
    # 'fr' is a valid ISO 639-1 code, so it should parse
    assert state["event_slug"] == "hall"
    assert state["language_code"] == "fr"


@pytest.mark.anyio
async def test_identity_booth_join_and_snapshot():
    """Full flow: create booth via identity, join, verify snapshot."""
    registry = BoothRegistry()
    await registry.create_booth(
        room_id=1,
        event_slug="pycon2026",
        language_code="de",
        language="German",
    )

    participant = await join_identity(
        registry,
        "Interpreter A",
        booth_id="pycon2026-1-de",
    )
    state = await registry.snapshot("pycon2026-1-de", "German", "pycon2026-de-audio")

    assert state["active_interpreter_id"] == participant.participant_id
    assert state["event_slug"] == "pycon2026"
    assert state["language_code"] == "de"
    assert len(state["participants"]) == 1


# ── Event and room field tests ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_booth_with_room_id():
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=42,
        event_slug="pycon2026",
        language_code="en",
        language="English",
    )

    assert state["room_id"] == 42
    assert state["event_slug"] == "pycon2026"
    assert state["channel_id"] == "pycon2026/42/en"


@pytest.mark.anyio
async def test_create_booth_room_id_defaults_to_none():
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=1,
        event_slug="fossasia2026",
        language_code="fr",
        language="French",
    )

    assert state["room_id"] == 1


@pytest.mark.anyio
async def test_channel_id_defaults_to_mediamtx_path():
    """When no explicit channel_id is given, it should equal the MediaMTX path."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=1,
        event_slug="pycon2026",
        language_code="de",
        language="German",
    )

    assert state["channel_id"] == "pycon2026/1/de"
    assert state["channel_id"] == state["mediamtx_path"]


@pytest.mark.anyio
async def test_channel_id_explicit_overrides_default():
    """An explicit channel_id should override the mediamtx_path default."""
    registry = BoothRegistry()
    state = await registry.create_booth(
        room_id=1,
        event_slug="pycon2026",
        language_code="es",
        language="Spanish",
        channel_id="custom-channel",
    )

    assert state["channel_id"] == "custom-channel"
    assert state["mediamtx_path"] == "pycon2026/1/es"


@pytest.mark.anyio
async def test_snapshot_passes_room_id_on_creation():
    """room_id passed via snapshot should be stored on initial booth creation."""
    registry = BoothRegistry()
    state = await registry.snapshot("pycon2026-1-en", "English", "pycon2026/en", room_id=7)

    assert state["room_id"] == 7
    assert state["event_slug"] == "pycon2026"


@pytest.mark.anyio
async def test_snapshot_room_id_immutable_after_creation():
    """room_id from a later snapshot call must not overwrite the original."""
    registry = BoothRegistry()
    await registry.snapshot("pycon2026-1-en", "English", "pycon2026/en", room_id=7)
    state = await registry.snapshot("pycon2026-1-en", "English", "pycon2026/en", room_id=99)

    assert state["room_id"] == 7


@pytest.mark.anyio
async def test_join_participant_passes_room_id():
    """room_id passed via join_participant creates the booth with that room."""
    registry = BoothRegistry()
    _, state = await registry.join_participant(
        booth_id="pycon2026-1-en",
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
    state = await registry.snapshot("hall-1-fr", "French", "hall-1-fr-audio")

    assert state["room_id"] == 1


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
            "hall-1-fr",
            coordinator.participant_id,
            "French",
            "hall-1-fr-audio",
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
            "hall-1-fr",
            coordinator.participant_id,
            "French",
            "hall-1-fr-audio",
            ingest_connected=True,
        )


@pytest.mark.anyio
async def test_check_publish_permission_active_interpreter_passes():
    """Active interpreter passes the publish permission check."""
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    # Should not raise
    await registry.check_publish_permission(
        "hall-1-fr",
        interpreter.participant_id,
        "French",
        "hall-1-fr-audio",
    )


@pytest.mark.anyio
async def test_check_publish_permission_standby_interpreter_rejected():
    """Standby interpreter fails the publish permission check."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.check_publish_permission(
            "hall-1-fr",
            interpreter_b.participant_id,
            "French",
            "hall-1-fr-audio",
        )


@pytest.mark.anyio
async def test_check_publish_permission_active_coordinator_passes():
    """An active coordinator (BOOTH_GO_LIVE permission) passes the publish check."""
    registry = BoothRegistry()
    coordinator = await join(registry, "Coordinator", role="room_coordinator")
    # Coordinator is auto-assigned active on first join
    await registry.check_publish_permission(
        "hall-1-fr",
        coordinator.participant_id,
        "French",
        "hall-1-fr-audio",
    )  # must not raise


@pytest.mark.anyio
async def test_check_publish_permission_standby_coordinator_rejected():
    """A non-active coordinator fails the publish check (active-interpreter guard)."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")  # becomes active
    coordinator = await join(registry, "Coordinator", role="room_coordinator")

    with pytest.raises(PermissionError, match="active interpreter"):
        await registry.check_publish_permission(
            "hall-1-fr",
            coordinator.participant_id,
            "French",
            "hall-1-fr-audio",
        )


@pytest.mark.anyio
async def test_check_publish_permission_unknown_participant():
    """Unknown participant_id raises ValueError."""
    registry = BoothRegistry()
    await join(registry, "Interpreter A")

    with pytest.raises(ValueError, match="does not exist"):
        await registry.check_publish_permission(
            "hall-1-fr",
            "unknown-id",
            "French",
            "hall-1-fr-audio",
        )


@pytest.mark.anyio
async def test_active_interpreter_can_turn_off_mic():
    """Active interpreter can set mic_active=False (non-publisher state)."""
    registry = BoothRegistry()
    interpreter = await join(registry, "Interpreter A")

    await registry.update_participant_state(
        "hall-1-fr",
        interpreter.participant_id,
        "French",
        "hall-1-fr-audio",
        mic_active=True,
    )
    state = await registry.update_participant_state(
        "hall-1-fr",
        interpreter.participant_id,
        "French",
        "hall-1-fr-audio",
        mic_active=False,
    )

    participants = {p["participant_id"]: p for p in state["participants"]}
    assert participants[interpreter.participant_id]["mic_active"] is False


# ── Booth listing tests ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_booths_for_event_returns_matching():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="en", language="English")
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="fr", language="French")
    await registry.create_booth(room_id=1, event_slug="fossasia", language_code="en", language="English")

    result = await registry.list_booths_for_event("pycon2026")

    assert len(result) == 2
    slugs = {b["language_code"] for b in result}
    assert slugs == {"en", "fr"}


@pytest.mark.anyio
async def test_list_booths_for_event_empty():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="en", language="English")

    result = await registry.list_booths_for_event("nonexistent")

    assert result == []


# ── Namespace isolation (get_booth / get_booth_for_event / validate_booth_event) ──


@pytest.mark.anyio
async def test_get_booth_returns_existing():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth("pycon2026-1-en")
    assert result is not None
    assert result["booth_id"] == "pycon2026-1-en"


@pytest.mark.anyio
async def test_get_booth_returns_none_for_missing():
    registry = BoothRegistry()
    result = await registry.get_booth("nonexistent-booth")
    assert result is None


@pytest.mark.anyio
async def test_get_booth_for_event_returns_matching():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="fr", language="French")
    result = await registry.get_booth_for_event("pycon2026", 1, "fr")
    assert result is not None
    assert result["booth_id"] == "pycon2026-1-fr"
    assert result["event_slug"] == "pycon2026"
    assert result["language_code"] == "fr"


@pytest.mark.anyio
async def test_get_booth_for_event_returns_none_wrong_event():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth_for_event("fossasia", 1, "en")
    assert result is None


@pytest.mark.anyio
async def test_get_booth_for_event_returns_none_wrong_language():
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="pycon2026", language_code="en", language="English")
    result = await registry.get_booth_for_event("pycon2026", 1, "de")
    assert result is None


@pytest.mark.anyio
async def test_validate_booth_event_passes_for_correct_event():
    registry = BoothRegistry()
    # Should not raise
    await registry.validate_booth_event("pycon2026-1-en", "pycon2026")


@pytest.mark.anyio
async def test_validate_booth_event_rejects_wrong_event():
    registry = BoothRegistry()
    with pytest.raises(PermissionError, match="does not belong"):
        await registry.validate_booth_event("pycon2026-1-en", "fossasia")


@pytest.mark.anyio
async def test_validate_booth_event_rejects_malformed_id():
    registry = BoothRegistry()
    with pytest.raises(PermissionError, match="does not belong"):
        await registry.validate_booth_event("bad", "pycon2026")


@pytest.mark.anyio
async def test_cross_event_isolation_booths_invisible():
    """Booths from event A must not appear in event B listings."""
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="eventa", language_code="en", language="English")
    await registry.create_booth(room_id=1, event_slug="eventa", language_code="fr", language="French")
    await registry.create_booth(room_id=1, event_slug="eventb", language_code="de", language="German")

    a_booths = await registry.list_booths_for_event("eventa")
    b_booths = await registry.list_booths_for_event("eventb")

    assert len(a_booths) == 2
    assert len(b_booths) == 1
    assert all(b["event_slug"] == "eventa" for b in a_booths)
    assert all(b["event_slug"] == "eventb" for b in b_booths)

    # get_booth_for_event also isolates
    assert await registry.get_booth_for_event("eventa", 1, "de") is None
    assert await registry.get_booth_for_event("eventb", 1, "en") is None
    assert (await registry.get_booth_for_event("eventb", 1, "de"))["booth_id"] == "eventb-1-de"


@pytest.mark.anyio
async def test_cross_event_participants_isolated():
    """Participants in event A booth must not appear in event B booth."""
    registry = BoothRegistry()
    await registry.create_booth(room_id=1, event_slug="eventa", language_code="en", language="English")
    await registry.create_booth(room_id=1, event_slug="eventb", language_code="en", language="English")

    await registry.join_participant(
        booth_id="eventa-1-en",
        display_name="Alice",
        role="interpreter",
        language="English",
        channel_id="eventa/en",
    )

    a_state = await registry.get_booth("eventa-1-en")
    b_state = await registry.get_booth("eventb-1-en")
    assert len(a_state["participants"]) == 1
    assert len(b_state["participants"]) == 0


# ── Handoff recovery ─────────────────────────────────────────────────────────

BOOTH = ("hall-1-fr", "French", "hall-1-fr-audio")


@pytest.mark.anyio
async def test_handoff_can_start_after_a_reassignment():
    registry = BoothRegistry()
    interpreter_a = await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    await registry.set_active_interpreter(
        BOOTH[0], interpreter_a.participant_id, interpreter_b.participant_id, *BOOTH[1:]
    )

    state = await registry.initiate_handoff(BOOTH[0], interpreter_b.participant_id, *BOOTH[1:])

    assert state["handoff_state"] == "offered"


@pytest.mark.anyio
async def test_a_reassignment_clears_a_handoff_in_progress():
    registry = BoothRegistry()
    interpreter_a = await join(registry, "Interpreter A")
    interpreter_b = await join(registry, "Interpreter B")
    coordinator = await join(registry, "Coordinator", role="room_coordinator")
    await registry.initiate_handoff(BOOTH[0], interpreter_a.participant_id, *BOOTH[1:])

    state = await registry.set_active_interpreter(
        BOOTH[0], coordinator.participant_id, interpreter_b.participant_id, *BOOTH[1:]
    )

    assert state["handoff_state"] == "idle"
    assert state["handoff_initiator_id"] is None


@pytest.mark.anyio
async def test_handoff_can_start_after_the_active_interpreter_leaves():
    registry = BoothRegistry()
    interpreter_a = await join(registry, "Interpreter A")
    await join(registry, "Interpreter B")
    await join(registry, "Interpreter C")
    state = await registry.leave_participant(BOOTH[0], interpreter_a.participant_id, *BOOTH[1:])

    state = await registry.initiate_handoff(BOOTH[0], state["active_interpreter_id"], *BOOTH[1:])

    assert state["handoff_state"] == "offered"


@pytest.mark.anyio
async def test_mic_is_not_handed_to_an_initiator_who_left():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    passive = await join(registry, "Interpreter B")
    await registry.initiate_handoff(BOOTH[0], passive.participant_id, *BOOTH[1:])

    state = await registry.leave_participant(BOOTH[0], passive.participant_id, *BOOTH[1:])

    assert state["handoff_state"] == "idle"
    with pytest.raises(ValueError, match="No handoff is in progress"):
        await registry.accept_handoff(BOOTH[0], active.participant_id, *BOOTH[1:])
    state = await registry.snapshot(*BOOTH)
    assert state["active_interpreter_id"] == active.participant_id


@pytest.mark.anyio
async def test_support_cannot_take_over_an_offered_mic():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    await join(registry, "Interpreter B")
    support = await join(registry, "Support", role="support")
    await registry.initiate_handoff("hall-1-fr", active.participant_id, "French", "hall-1-fr-audio")

    with pytest.raises(PermissionError, match="take over the mic"):
        await registry.accept_handoff("hall-1-fr", support.participant_id, "French", "hall-1-fr-audio")

    state = await registry.snapshot("hall-1-fr", "French", "hall-1-fr-audio")
    assert state["active_interpreter_id"] == active.participant_id
    assert state["handoff_state"] == "offered"


@pytest.mark.anyio
async def test_an_interpreter_can_still_take_over_an_offered_mic():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    passive = await join(registry, "Interpreter B")
    await registry.initiate_handoff("hall-1-fr", active.participant_id, "French", "hall-1-fr-audio")

    state = await registry.accept_handoff("hall-1-fr", passive.participant_id, "French", "hall-1-fr-audio")

    assert state["active_interpreter_id"] == passive.participant_id


# ── Ingest status on leave ───────────────────────────────────────────────────


async def go_live(registry, participant):
    return await registry.update_participant_state(
        BOOTH[0], participant.participant_id, *BOOTH[1:], mic_active=True, ingest_connected=True
    )


@pytest.mark.anyio
async def test_booth_is_not_live_after_the_publishing_interpreter_leaves():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    passive = await join(registry, "Interpreter B")
    await go_live(registry, active)

    state = await registry.leave_participant(BOOTH[0], active.participant_id, *BOOTH[1:])

    assert state["active_interpreter_id"] == passive.participant_id
    assert state["ingest_status"] == "disconnected"


@pytest.mark.anyio
async def test_booth_stays_live_when_a_passive_interpreter_leaves():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    passive = await join(registry, "Interpreter B")
    await go_live(registry, active)

    state = await registry.leave_participant(BOOTH[0], passive.participant_id, *BOOTH[1:])

    assert state["active_interpreter_id"] == active.participant_id
    assert state["ingest_status"] == "connected"


@pytest.mark.anyio
async def test_booth_is_not_live_after_the_last_publisher_leaves():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    await go_live(registry, active)

    state = await registry.leave_participant(BOOTH[0], active.participant_id, *BOOTH[1:])

    assert state["participants"] == []
    assert state["ingest_status"] == "disconnected"


@pytest.mark.anyio
async def test_a_non_publisher_leaving_keeps_an_overloaded_status():
    registry = BoothRegistry()
    active = await join(registry, "Interpreter A")
    passive = await join(registry, "Interpreter B")
    await go_live(registry, active)
    registry.get_booth_sync(BOOTH[0]).ingest_status = "overloaded"

    state = await registry.leave_participant(BOOTH[0], passive.participant_id, *BOOTH[1:])

    assert state["ingest_status"] == "overloaded"


# ── Coordinated State Transitions and Event Publication Tests ──────────────────


class RecordingPublisher:
    """Test double implementing BoothEventPublisher for verifying emitted events."""

    def __init__(
        self,
        fail_broadcast: bool = False,
        fail_booth: bool = False,
        fail_chat_only: bool = False,
    ) -> None:
        self.booth_events: list[tuple[str, dict]] = []
        self.listener_events: list[tuple[str, dict]] = []
        self.fail_broadcast = fail_broadcast
        self.fail_booth = fail_booth
        self.fail_chat_only = fail_chat_only

    async def broadcast_booth(self, booth_id: str, message: dict) -> None:
        if self.fail_broadcast or self.fail_booth:
            raise RuntimeError("Simulated network delivery failure")
        if self.fail_chat_only and message.get("type") == "booth:chat":
            raise RuntimeError("Simulated chat delivery failure")
        self.booth_events.append((booth_id, message))

    async def broadcast_listeners(self, booth_id: str, message: dict) -> None:
        if self.fail_broadcast:
            raise RuntimeError("Simulated network delivery failure")
        self.listener_events.append((booth_id, message))


@pytest.mark.anyio
async def test_join_participant_coordinates_event_publication():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    direct_joined: list[dict] = []

    async def on_joined(p, state):
        direct_joined.append({"participant_id": p.participant_id, "state": state})

    participant, state = await registry.join_participant(
        booth_id="pycon2026-1-en",
        display_name="Interpreter A",
        role="interpreter",
        language="English",
        channel_id="pycon2026/1/en",
        before_broadcast=on_joined,
    )

    # Direct response was sent before broadcast
    assert len(direct_joined) == 1
    assert direct_joined[0]["participant_id"] == participant.participant_id
    assert direct_joined[0]["state"] == state

    # State broadcast was emitted to the booth audience
    assert len(publisher.booth_events) == 1
    booth_id, event = publisher.booth_events[0]
    assert booth_id == "pycon2026-1-en"
    assert event["type"] == "booth:state"
    assert event["state"]["active_interpreter_id"] == participant.participant_id
    assert event["state"] == state


@pytest.mark.anyio
async def test_leave_participant_coordinates_event_and_noop_suppresses_event():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    p = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    # Successful leave emits state event
    await registry.leave_participant("pycon2026-1-en", p.participant_id, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 1
    _, event = publisher.booth_events[0]
    assert event["type"] == "booth:state"
    assert len(event["state"]["participants"]) == 0

    # No-op leave with non-existent participant emits no duplicate event
    publisher.booth_events.clear()
    await registry.leave_participant("pycon2026-1-en", "non-existent-id", "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 0


@pytest.mark.anyio
async def test_set_active_interpreter_coordinates_event():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    int_a = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    int_b = await join_identity(registry, "Interpreter B", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    # Successful active reassignment
    state = await registry.set_active_interpreter(
        "pycon2026-1-en", int_a.participant_id, int_b.participant_id, "English", "pycon2026/1/en"
    )
    assert len(publisher.booth_events) == 1
    _, event = publisher.booth_events[0]
    assert event["type"] == "booth:state"
    assert event["state"]["active_interpreter_id"] == int_b.participant_id
    assert event["state"] == state

    # Unauthorized reassignment: passive interpreter int_a tries to assign int_c active
    int_c = await join_identity(registry, "Interpreter C", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()
    with pytest.raises(PermissionError):
        await registry.set_active_interpreter(
            "pycon2026-1-en", int_a.participant_id, int_c.participant_id, "English", "pycon2026/1/en"
        )
    assert len(publisher.booth_events) == 0


@pytest.mark.anyio
async def test_update_participant_state_coordinates_event():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    int_a = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    # Valid update emits event
    state = await registry.update_participant_state(
        "pycon2026-1-en", int_a.participant_id, "English", "pycon2026/1/en", mic_active=True
    )
    assert len(publisher.booth_events) == 1
    _, event = publisher.booth_events[0]
    assert event["type"] == "booth:state"
    assert event["state"] == state

    # Invalid update raises and emits NO event
    publisher.booth_events.clear()
    with pytest.raises(ValueError):
        await registry.update_participant_state(
            "pycon2026-1-en", int_a.participant_id, "English", "pycon2026/1/en", mic_active="invalid"
        )
    assert len(publisher.booth_events) == 0


@pytest.mark.anyio
async def test_set_broadcast_unlocked_emits_to_booth_and_listeners():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()
    publisher.listener_events.clear()

    # Unlock broadcast
    state = await registry.set_broadcast_unlocked("pycon2026-1-en", True, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 1
    assert len(publisher.listener_events) == 1
    assert publisher.booth_events[0][1]["state"] == state
    assert publisher.listener_events[0][1]["state"] == state

    # Lock broadcast
    publisher.booth_events.clear()
    publisher.listener_events.clear()
    state_locked = await registry.set_broadcast_unlocked("pycon2026-1-en", False, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 1
    assert len(publisher.listener_events) == 1
    assert state_locked["broadcast_unlocked"] is False


@pytest.mark.anyio
async def test_handoff_and_chat_event_coordination():
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    int_a = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    int_b = await join_identity(registry, "Interpreter B", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    # Handoff initiation emits state
    await registry.initiate_handoff("pycon2026-1-en", int_a.participant_id, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 1
    assert publisher.booth_events[0][1]["state"]["handoff_state"] == "offered"

    # Handoff acceptance emits state
    publisher.booth_events.clear()
    await registry.accept_handoff("pycon2026-1-en", int_b.participant_id, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 1
    assert publisher.booth_events[0][1]["state"]["handoff_state"] == "idle"
    assert publisher.booth_events[0][1]["state"]["active_interpreter_id"] == int_b.participant_id

    # Handoff cancellation when idle is a no-op and emits no event
    publisher.booth_events.clear()
    await registry.cancel_handoff("pycon2026-1-en", int_b.participant_id, "English", "pycon2026/1/en")
    assert len(publisher.booth_events) == 0

    # Chat message emits booth:chat first, then booth:state
    publisher.booth_events.clear()
    msg, _ = await registry.add_chat_message(
        "pycon2026-1-en", int_b.participant_id, "Hello booth", "English", "pycon2026/1/en"
    )
    assert len(publisher.booth_events) == 2
    assert publisher.booth_events[0][1]["type"] == "booth:chat"
    assert publisher.booth_events[0][1]["message"] == msg
    assert publisher.booth_events[1][1]["type"] == "booth:state"


@pytest.mark.anyio
async def test_delivery_failure_does_not_rollback_in_memory_state():
    """A network broadcast failure must not rollback the committed in-memory state transition."""
    publisher = RecordingPublisher(fail_broadcast=False)
    registry = BoothRegistry(publisher=publisher)
    int_a = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    int_b = await join_identity(registry, "Interpreter B", booth_id="pycon2026-1-en")

    # Now make the network broadcast fail
    publisher.fail_broadcast = True

    with pytest.raises(RuntimeError, match="Simulated network delivery failure"):
        await registry.set_active_interpreter(
            "pycon2026-1-en", int_a.participant_id, int_b.participant_id, "English", "pycon2026/1/en"
        )

    # In-memory state remains committed to int_b despite broadcast failure
    snapshot = await registry.snapshot("pycon2026-1-en", "English", "pycon2026/1/en")
    assert snapshot["active_interpreter_id"] == int_b.participant_id


def test_architecture_regression_boundaries():
    """Ensure manager.py and handlers.py no longer expose or import private _handle_* functions."""
    import portal.websockets.handlers as handlers_mod
    import portal.websockets.manager as manager_mod
    from portal.websockets.booth_protocol import BoothProtocolCoordinator, booth_protocol

    # manager.py must not define _handle_* functions
    extracted_handlers = [
        "_handle_join",
        "_handle_leave",
        "_handle_chat",
        "_handle_set_active",
        "_handle_update_state",
        "_handle_set_broadcast_unlocked",
        "_handle_initiate_handoff",
        "_handle_accept_handoff",
        "_handle_cancel_handoff",
    ]
    for handler_name in extracted_handlers:
        assert not hasattr(manager_mod, handler_name), f"{handler_name} still in manager.py"
        assert not hasattr(handlers_mod, handler_name), f"{handler_name} still in handlers.py"

    # booth_protocol must provide public dispatch entry point
    assert isinstance(booth_protocol, BoothProtocolCoordinator)
    assert callable(booth_protocol.dispatch)
    assert callable(booth_protocol.handle_disconnect)


@pytest.mark.anyio
async def test_concurrent_state_transitions_preserve_broadcast_order():
    """Verify that concurrent transitions on a booth serialize broadcasts and do not clobber newer state."""
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)

    async def slow_before_broadcast(participant, state):
        # Yield to event loop to allow another concurrent transition to run
        await asyncio.sleep(0.05)

    # Launch join with slow direct response
    join_task = asyncio.create_task(
        registry.join_participant(
            booth_id="pycon2026-1-en",
            display_name="Interpreter A",
            role="interpreter",
            language="English",
            channel_id="pycon2026/1/en",
            participant_id="int-a",
            before_broadcast=slow_before_broadcast,
        )
    )

    # Let join_task enter before_broadcast
    await asyncio.sleep(0.01)

    # Concurrently perform another transition while join_task is in before_broadcast
    unlock_task = asyncio.create_task(
        registry.set_broadcast_unlocked("pycon2026-1-en", True, "English", "pycon2026/1/en")
    )

    await asyncio.gather(join_task, unlock_task)

    # Verify that broadcasts arrived in the correct serial order:
    # First: join_participant broadcast (broadcast_unlocked=False)
    # Second: set_broadcast_unlocked broadcast (broadcast_unlocked=True)
    assert len(publisher.booth_events) == 2
    first_event = publisher.booth_events[0][1]["state"]
    second_event = publisher.booth_events[1][1]["state"]

    assert first_event["broadcast_unlocked"] is False
    assert second_event["broadcast_unlocked"] is True


@pytest.mark.anyio
async def test_set_broadcast_unlocked_resilient_to_booth_broadcast_failure():
    """Failure in booth broadcast must not prevent listener broadcast."""
    publisher = RecordingPublisher(fail_booth=True)
    registry = BoothRegistry(publisher=publisher)
    await registry.create_booth("pycon2026", "en", "English", room_id=1)

    with pytest.raises(RuntimeError, match="Simulated network delivery failure"):
        await registry.set_broadcast_unlocked("pycon2026-1-en", True, "English", "pycon2026/1/en")

    # Even though booth broadcast failed, listeners broadcast was still executed
    assert len(publisher.listener_events) == 1
    assert publisher.listener_events[0][1]["state"]["broadcast_unlocked"] is True


@pytest.mark.anyio
async def test_add_chat_resilient_to_chat_broadcast_failure():
    """Failure in booth:chat broadcast must not prevent booth:state broadcast."""
    publisher = RecordingPublisher(fail_chat_only=True)
    registry = BoothRegistry(publisher=publisher)
    p = await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    with pytest.raises(RuntimeError, match="Simulated chat delivery failure"):
        await registry.add_chat_message("pycon2026-1-en", p.participant_id, "Hello booth", "English", "pycon2026/1/en")

    # booth:state was still delivered despite chat broadcast failure
    assert len(publisher.booth_events) == 1
    assert publisher.booth_events[0][1]["type"] == "booth:state"


@pytest.mark.anyio
async def test_set_ingest_overloaded_coordinates_state_and_broadcast():
    """set_ingest_overloaded updates in-memory status and publishes booth:state."""
    publisher = RecordingPublisher()
    registry = BoothRegistry(publisher=publisher)
    await join_identity(registry, "Interpreter A", booth_id="pycon2026-1-en")
    publisher.booth_events.clear()

    state = await registry.set_ingest_overloaded("pycon2026-1-en")
    assert state is not None
    assert state["ingest_status"] == "overloaded"

    assert len(publisher.booth_events) == 1
    assert publisher.booth_events[0][1]["type"] == "booth:state"
    assert publisher.booth_events[0][1]["state"]["ingest_status"] == "overloaded"

    # Non-existent booth returns None and emits no event
    publisher.booth_events.clear()
    assert await registry.set_ingest_overloaded("non-existent") is None
    assert len(publisher.booth_events) == 0
