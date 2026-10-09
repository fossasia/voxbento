from __future__ import annotations

from datetime import timedelta

import pytest

from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST, Room, utc_now
from portal.program_ingest.credentials import (
    CREDENTIAL_PREFIX,
    apply_issued_secret,
    hash_ingest_secret,
    issue_ingest_secret,
    revoke_ingest_secret,
    verify_room_ingest_secret,
)


def _room(mode: str = FLOOR_SOURCE_PROGRAM_INGEST) -> Room:
    return Room(event_id=1, display_name="Main", floor_source_mode=mode)


def test_issued_secret_is_random_prefixed_and_only_digest_is_stored():
    first = issue_ingest_secret()
    second = issue_ingest_secret()
    assert first.secret.startswith(CREDENTIAL_PREFIX)
    assert first.secret != second.secret
    assert len(first.secret) > 40
    assert first.digest.startswith("$2")
    assert first.digest != hash_ingest_secret(first.secret)  # salted
    assert first.secret not in first.digest
    assert first.hint == first.secret[-4:]

    room = _room()
    apply_issued_secret(room, first)
    stored_values = [
        room.program_ingest_secret_hash,
        room.program_ingest_secret_hint,
        str(room.program_ingest_secret_created_at),
    ]
    assert all(first.secret not in str(v) for v in stored_values)


def test_valid_secret_verifies():
    room = _room()
    issued = issue_ingest_secret()
    apply_issued_secret(room, issued)
    assert verify_room_ingest_secret(room, issued.secret)


@pytest.mark.parametrize("presented", ["", "vbi_wrong", "x" * 60])
def test_invalid_secret_rejected(presented: str):
    room = _room()
    apply_issued_secret(room, issue_ingest_secret())
    assert not verify_room_ingest_secret(room, presented)


def test_rotation_invalidates_previous_secret():
    room = _room()
    old = issue_ingest_secret()
    apply_issued_secret(room, old)
    new = issue_ingest_secret()
    apply_issued_secret(room, new)
    assert not verify_room_ingest_secret(room, old.secret)
    assert verify_room_ingest_secret(room, new.secret)


def test_revoked_secret_rejected():
    room = _room()
    issued = issue_ingest_secret()
    apply_issued_secret(room, issued)
    revoke_ingest_secret(room)
    assert not verify_room_ingest_secret(room, issued.secret)
    assert room.program_ingest_secret_hint is None


def test_expired_secret_rejected():
    now = utc_now()
    room = _room()
    issued = issue_ingest_secret(expires_in_days=1, now=now - timedelta(days=2))
    apply_issued_secret(room, issued)
    assert not verify_room_ingest_secret(room, issued.secret, now=now)


def test_unexpired_secret_with_naive_stored_timestamp_verifies():
    now = utc_now()
    room = _room()
    issued = issue_ingest_secret(expires_in_days=7, now=now)
    apply_issued_secret(room, issued)
    room.program_ingest_secret_expires_at = issued.expires_at.replace(tzinfo=None)
    assert verify_room_ingest_secret(room, issued.secret, now=now)


def test_secret_rejected_when_room_uses_floor_bot():
    room = _room(FLOOR_SOURCE_JITSI_BOT)
    issued = issue_ingest_secret()
    apply_issued_secret(room, issued)
    assert not verify_room_ingest_secret(room, issued.secret)


def test_unsupported_expiry_rejected():
    with pytest.raises(ValueError):
        issue_ingest_secret(expires_in_days=3)


def test_corrupt_stored_hash_rejected():
    room = _room()
    issued = issue_ingest_secret()
    apply_issued_secret(room, issued)
    room.program_ingest_secret_hash = "0" * 64
    assert not verify_room_ingest_secret(room, issued.secret)
