import pytest

from portal.booth_state import BoothRegistry


def join(registry, name, role='interpreter'):
    participant, _ = registry.join_participant(
        booth_id='hall-a-fr',
        display_name=name,
        role=role,
        language='French',
        channel_id='hall-a-fr-audio',
    )
    return participant


def test_first_interpreter_becomes_active():
    registry = BoothRegistry()
    interpreter = join(registry, 'Interpreter A')

    state = registry.snapshot('hall-a-fr', 'French', 'hall-a-fr-audio')

    assert state['active_interpreter_id'] == interpreter.participant_id


def test_active_interpreter_can_pass_relay_and_old_publisher_is_cleared():
    registry = BoothRegistry()
    interpreter_a = join(registry, 'Interpreter A')
    interpreter_b = join(registry, 'Interpreter B')
    registry.update_participant_state(
        'hall-a-fr',
        interpreter_a.participant_id,
        'French',
        'hall-a-fr-audio',
        mic_active=True,
        ingest_connected=True,
    )

    state = registry.set_active_interpreter(
        'hall-a-fr',
        interpreter_a.participant_id,
        interpreter_b.participant_id,
        'French',
        'hall-a-fr-audio',
    )

    participants = {participant['participant_id']: participant for participant in state['participants']}
    assert state['active_interpreter_id'] == interpreter_b.participant_id
    assert state['ingest_status'] == 'disconnected'
    assert participants[interpreter_a.participant_id]['mic_active'] is False
    assert participants[interpreter_a.participant_id]['ingest_connected'] is False


def test_standby_interpreter_cannot_reassign_another_interpreter():
    registry = BoothRegistry()
    join(registry, 'Interpreter A')
    interpreter_b = join(registry, 'Interpreter B')
    interpreter_c = join(registry, 'Interpreter C')

    with pytest.raises(PermissionError):
        registry.set_active_interpreter(
            'hall-a-fr',
            interpreter_b.participant_id,
            interpreter_c.participant_id,
            'French',
            'hall-a-fr-audio',
        )


def test_standby_interpreter_cannot_mark_ingest_connected():
    registry = BoothRegistry()
    join(registry, 'Interpreter A')
    interpreter_b = join(registry, 'Interpreter B')

    with pytest.raises(PermissionError):
        registry.update_participant_state(
            'hall-a-fr',
            interpreter_b.participant_id,
            'French',
            'hall-a-fr-audio',
            mic_active=True,
            ingest_connected=True,
        )


def test_coordinator_can_assign_active_interpreter():
    registry = BoothRegistry()
    join(registry, 'Interpreter A')
    interpreter_b = join(registry, 'Interpreter B')
    coordinator = join(registry, 'Coordinator', role='coordinator')

    state = registry.set_active_interpreter(
        'hall-a-fr',
        coordinator.participant_id,
        interpreter_b.participant_id,
        'French',
        'hall-a-fr-audio',
    )

    assert state['active_interpreter_id'] == interpreter_b.participant_id
