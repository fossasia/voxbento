"""Program ingest publish credentials.

A credential is a high-entropy random secret bound to exactly one room. It is
stored with bcrypt, the project's standard for credentials a client presents
(``portal.auth.hash_password``): the plaintext is returned once when it is
issued or rotated and can never be recovered afterwards. Encoders present it
as a WHIP bearer token or, for RTMP/SRT/RTSP, as the password.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from portal.auth import hash_password, verify_password
from portal.models import FLOOR_SOURCE_PROGRAM_INGEST, Room, utc_now

CREDENTIAL_PREFIX = "vbi_"
HINT_LENGTH = 4
# Expiry choices offered in the admin UI; ``None`` means "never expires".
ALLOWED_EXPIRY_DAYS = frozenset({1, 7, 30, 90})


@dataclass(frozen=True)
class IssuedSecret:
    secret: str
    digest: str
    hint: str
    created_at: datetime
    expires_at: datetime | None


def hash_ingest_secret(secret: str) -> str:
    return hash_password(secret)


def issue_ingest_secret(expires_in_days: int | None = None, now: datetime | None = None) -> IssuedSecret:
    if expires_in_days is not None and expires_in_days not in ALLOWED_EXPIRY_DAYS:
        raise ValueError(f"Unsupported expiry; choose one of {sorted(ALLOWED_EXPIRY_DAYS)} days or none.")
    created_at = now or utc_now()
    secret = CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    return IssuedSecret(
        secret=secret,
        digest=hash_ingest_secret(secret),
        hint=secret[-HINT_LENGTH:],
        created_at=created_at,
        expires_at=created_at + timedelta(days=expires_in_days) if expires_in_days else None,
    )


def apply_issued_secret(room: Room, issued: IssuedSecret) -> None:
    room.program_ingest_secret_hash = issued.digest
    room.program_ingest_secret_hint = issued.hint
    room.program_ingest_secret_created_at = issued.created_at
    room.program_ingest_secret_expires_at = issued.expires_at


def revoke_ingest_secret(room: Room) -> None:
    room.program_ingest_secret_hash = None
    room.program_ingest_secret_hint = None
    room.program_ingest_secret_created_at = None
    room.program_ingest_secret_expires_at = None


def is_secret_expired(room: Room, now: datetime | None = None) -> bool:
    expires_at = room.program_ingest_secret_expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        # SQLite drops tzinfo; stored values are always UTC.
        expires_at = expires_at.replace(tzinfo=(now or utc_now()).tzinfo)
    return (now or utc_now()) >= expires_at


def verify_room_ingest_secret(room: Room, presented: str, now: datetime | None = None) -> bool:
    """Return whether ``presented`` may publish the program feed for ``room``.

    The room must be in program-ingest mode and hold an unexpired credential.
    bcrypt verification is deliberately slow; call this off the event loop.
    """
    if room.floor_source_mode != FLOOR_SOURCE_PROGRAM_INGEST:
        return False
    stored = room.program_ingest_secret_hash
    if not stored or not presented:
        return False
    if is_secret_expired(room, now):
        return False
    try:
        return verify_password(presented, stored)
    except ValueError:
        # Not a bcrypt hash (corrupt row): treat as no valid credential.
        return False
