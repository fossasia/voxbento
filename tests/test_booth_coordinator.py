from __future__ import annotations

import json

import pytest

from portal.booth_coordinator import BoothCoordinator
from portal.booth_state import BoothRegistry
from portal.websockets.manager import Session


class BroadcastRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def broadcast(self, booth_id: str, message: dict) -> None:
        self.messages.append((booth_id, message))


class WebSocketRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


@pytest.mark.anyio
async def test_join_sends_acknowledgement_then_broadcasts_state():
    booth_id = "event-1-en"
    registry = BoothRegistry()
    manager = BroadcastRecorder()
    listeners = BroadcastRecorder()
    coordinator = BoothCoordinator(registry, manager, listeners)
    websocket = WebSocketRecorder()
    session = Session(booth_id, None, "English", f"{booth_id}-audio")

    participant = await coordinator.join_participant(
        websocket,
        session,
        display_name="Interpreter",
        role="interpreter",
        language="English",
        channel_id=f"{booth_id}-audio",
        participant_id=None,
        room_id=None,
    )

    acknowledgement = json.loads(websocket.messages[0])
    assert acknowledgement["type"] == "booth:joined"
    assert acknowledgement["participant_id"] == participant.participant_id
    assert [message["type"] for _, message in manager.messages] == ["booth:state"]
    assert manager.messages[0][1]["state"]["participants"][0]["participant_id"] == participant.participant_id


@pytest.mark.anyio
async def test_set_active_broadcasts_only_after_successful_state_change():
    booth_id = "event-1-en"
    registry = BoothRegistry()
    manager = BroadcastRecorder()
    coordinator = BoothCoordinator(registry, manager, BroadcastRecorder())
    language = "English"
    channel_id = f"{booth_id}-audio"
    requester, _ = await registry.join_participant(booth_id, "Interpreter A", "interpreter", language, channel_id)
    target, _ = await registry.join_participant(booth_id, "Interpreter B", "interpreter", language, channel_id)
    session = Session(booth_id, requester.participant_id, language, channel_id)

    await coordinator.set_active_interpreter(session, target.participant_id)

    assert len(manager.messages) == 1
    booth_state = manager.messages[0][1]["state"]
    assert booth_state["active_interpreter_id"] == target.participant_id

    with pytest.raises(ValueError):
        await coordinator.set_active_interpreter(session, "missing-participant")

    assert len(manager.messages) == 1


@pytest.mark.anyio
async def test_sync_sends_the_latest_booth_snapshot_to_a_reconnected_socket():
    booth_id = "event-1-en"
    registry = BoothRegistry()
    manager = BroadcastRecorder()
    coordinator = BoothCoordinator(registry, manager, BroadcastRecorder())
    participant, _ = await registry.join_participant(
        booth_id, "Interpreter", "interpreter", "English", f"{booth_id}-audio"
    )
    await registry.update_participant_state(
        booth_id, participant.participant_id, "English", f"{booth_id}-audio", mic_active=True
    )
    latest = await registry.get_booth(booth_id)
    assert latest is not None
    websocket = WebSocketRecorder()

    await coordinator.sync_state(websocket, booth_id)

    message = json.loads(websocket.messages[0])
    assert message["type"] == "booth:state"
    assert message["state"]["state_version"] == latest["state_version"]
    assert message["state"]["participants"][0]["mic_active"] is True


@pytest.mark.anyio
async def test_transcription_overload_updates_state_and_broadcasts_it():
    booth_id = "event-1-en"
    registry = BoothRegistry()
    manager = BroadcastRecorder()
    listeners = BroadcastRecorder()
    coordinator = BoothCoordinator(registry, manager, listeners)
    await registry.join_participant(booth_id, "Interpreter", "interpreter", "English", f"{booth_id}-audio")

    await coordinator.broadcast_transcription(booth_id, "[Server overloaded]")

    assert [message["type"] for _, message in manager.messages] == ["booth:error", "booth:state"]
    assert manager.messages[-1][1]["state"]["ingest_status"] == "overloaded"
    assert listeners.messages == []
