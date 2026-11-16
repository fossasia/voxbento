"""Program floor ownership, native MediaMTX authorization, and reconciliation.

Single portal process, like BoothRegistry. Media never traverses this module.
Credentials are 256-bit random secrets; only a deliberately expensive digest is persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from portal import program_relay
from portal.booth_identity import make_backup_floor_path, make_booth_id, make_mediamtx_path, parse_booth_id
from portal.booth_publish import authorize_booth_publish
from portal.config import settings
from portal.crypto import decrypt_val, encrypt_val
from portal.database import get_session
from portal.globals import get_http_client
from portal.models import Event, Room
from portal.outage_log import OutageLog
from portal.transcription.process import audio_progress
from portal.transcription.providers.base import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import active_workers, start_transcription_worker, stop_transcription_worker
from portal.websockets.manager import broadcast_transcription

logger = logging.getLogger(__name__)
room_locks: dict[int, asyncio.Lock] = {}
capacity_lock = asyncio.Lock()
FLOOR_PATH = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)/([1-9][0-9]*)/floor(?P<backup>_backup)?")
MEDIA_CONTROL_TIMEOUT = 3.0
# A freshly minted sender session may still be negotiating; reject duplicate starts within this window.
SENDER_PENDING_SECS = 15.0
TAKEOVER_STALL_SECS = 5.0


def room_lock(room_id: int) -> asyncio.Lock:
    """Serialize ownership changes for one room without blocking unrelated rooms."""
    return room_locks.setdefault(room_id, asyncio.Lock())


class MediaAuth(BaseModel):
    action: str
    path: str = Field(default="", max_length=500)
    protocol: str = ""
    id: str = ""
    ip: str = ""
    token: str = Field(default="", max_length=512, repr=False)
    password: str = Field(default="", max_length=512, repr=False)
    query: str = Field(default="", max_length=4096, repr=False)


class MediaSource(BaseModel):
    type: str
    id: str


class MediaPath(BaseModel):
    name: str
    available: bool = False
    source: MediaSource | None = None
    tracks: list[str] = Field(default_factory=list)
    inbound_bytes: int = Field(default=0, validation_alias="inboundBytes")

    @model_validator(mode="before")
    @classmethod
    def normalize_media_path(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "available" not in normalized:
            normalized["available"] = normalized.get("ready", False)
        if not normalized.get("tracks") and normalized.get("tracks2"):
            normalized["tracks"] = [
                track.get("codec", "") for track in normalized["tracks2"] if isinstance(track, dict)
            ]
        if "inboundBytes" not in normalized and "bytesReceived" in normalized:
            normalized["inboundBytes"] = normalized["bytesReceived"]
        return normalized

    @property
    def ready(self) -> bool:
        return self.available

    @property
    def bytesReceived(self) -> int:
        return self.inbound_bytes


@dataclass
class IngestHealth:
    state: str = "waiting"
    reason: str = "Waiting for an authenticated encoder."
    codecs: list[str] = field(default_factory=list)
    last_bytes: int = -1
    last_progress: float = field(default_factory=time.monotonic)
    missing_since: float | None = None
    reservation_started_at: float | None = None
    backup_session_id: str | None = None
    backup_reservation_started_at: float | None = None
    backup_connected: bool = False
    worker_config: tuple | None = None
    worker_started_at: float | None = None
    # Contribution ingest and language processing are reported separately from the overall state.
    ingest_state: str = "waiting"
    ingest_reason: str = "Waiting for an authenticated encoder."
    processing_state: str = "idle"
    processing_reason: str = "Starts when the contribution feed carries audio."
    # Gateway bitrate tracking
    last_cycle_bytes: int = -1
    last_cycle_time: float = 0.0
    high_bitrate_cycles: int = 0
    # Reconnect and session continuity tracking
    reconnect_count: int = 0
    last_session_id: str | None = None


health: dict[int, IngestHealth] = {}
failures: OrderedDict[str, tuple[float, int]] = OrderedDict()
_configured_srt_paths: set[str] = set()
path_list_outage = OutageLog(logger, "program_ingest path_list_unavailable")
database_outage = OutageLog(logger, "program_ingest database_unavailable")
PROGRAM_SECRET_KDF_ROUNDS = 120_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest_program_secret(secret: str) -> str:
    """Derive the stored token digest with password-storage-grade work factor."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        settings.effective_jwt_secret.encode("utf-8"),
        PROGRAM_SECRET_KDF_ROUNDS,
    ).hex()


def key_valid(room: Room, secret: str) -> bool:
    return key_digest_valid(room, digest_program_secret(secret))


def key_digest_valid(room: Room, digest: str) -> bool:
    return bool(
        room.floor_source == "program_ingest"
        and room.program_key_hash
        and secrets.compare_digest(room.program_key_hash, digest)
        and room.program_key_expires_at
        and aware(room.program_key_expires_at) > utc_now()
    )


def session_digest_valid(room: Room, digest: str) -> bool:
    """Validate the short-lived browser-sender secret; independent of the encoder key."""
    return bool(
        room.floor_source == "program_ingest"
        and room.program_session_secret_hash
        and secrets.compare_digest(room.program_session_secret_hash, digest)
        and room.program_session_secret_expires_at
        and aware(room.program_session_secret_expires_at) > utc_now()
    )


def credential_digest_valid(room: Room, digest: str) -> bool:
    """A floor publisher may present either the encoder key or a browser-sender secret."""
    return key_digest_valid(room, digest) or session_digest_valid(room, digest)


def has_valid_credential(room: Room) -> bool:
    now = utc_now()
    key_live = bool(room.program_key_hash and room.program_key_expires_at and aware(room.program_key_expires_at) > now)
    session_live = bool(
        room.program_session_secret_hash
        and room.program_session_secret_expires_at
        and aware(room.program_session_secret_expires_at) > now
    )
    srt_live = bool(
        room.program_ingest_mode in program_relay.SRT_MODES
        and room.program_srt_passphrase_enc
        and (room.program_srt_passphrase_expires_at is None or aware(room.program_srt_passphrase_expires_at) > now)
    )
    return key_live or session_live or srt_live


def has_valid_backup_credential(room: Room) -> bool:
    now = utc_now()
    return bool(
        room.floor_source == "program_ingest"
        and room.program_backup_enabled
        and room.program_backup_key_enc
        and (room.program_backup_key_expires_at is None or aware(room.program_backup_key_expires_at) > now)
    )


def publish_permitted(room: Room, protocol: str, digest: str) -> bool:
    """Bind each credential to the transport of the room's ingest mode.

    Both RTMPS modes accept only the encoder key over RTMPS (MediaMTX runs with
    ``rtmpEncryption: strict``, so "rtmp" here is always TLS); a browser-sender
    session secret never authorizes RTMPS. WHIP mode keeps the #690 behaviour.
    A key is never usable over the other mode's transport.
    """
    if room.program_ingest_mode == program_relay.SRT_MODE:
        return protocol == "srt" and settings.srt_ingest_enabled and has_valid_credential(room)
    if room.program_ingest_mode == program_relay.RELAY_MODE:
        return protocol == "rtmp" and settings.program_relay_enabled and key_digest_valid(room, digest)
    if room.program_ingest_mode == program_relay.RTMPS_MODE:
        return protocol == "rtmp" and settings.rtmps_ingest_enabled and key_digest_valid(room, digest)
    return protocol == "webrtc" and credential_digest_valid(room, digest)


def reserved_source(room: Room) -> MediaSource | None:
    """The MediaMTX session type a program-ingest reservation belongs to."""
    if not room.program_session_id:
        return None
    kind = (
        "srtConns"
        if room.program_ingest_mode in program_relay.SRT_MODES
        else ("rtmpsConn" if room.program_ingest_mode in program_relay.RTMPS_MODES else "webRTCSession")
    )
    return MediaSource(type=kind, id=room.program_session_id)


def reserved_backup_source(room: Room) -> MediaSource | None:
    """The MediaMTX session for a backup feed reservation."""
    current = health.get(room.id)
    if not current or not current.backup_session_id:
        return None
    return MediaSource(type="srtConns", id=current.backup_session_id)


def presented_secret(data: MediaAuth) -> str:
    """Extract the publisher credential without accepting it from more than one place."""
    if data.protocol == "webrtc":
        return "" if data.query else (data.token or data.password)
    if data.protocol == "srt":
        # Passphrase enforcement delegated to MediaMTX per-path publishPass.
        # Auth hook validates room ownership only.
        return ""
    if data.protocol == "rtmp" and settings.rtmps_ingest_enabled and not (data.token or data.password):
        # OBS stream key "floor?key=<secret>": the query must be exactly one key parameter.
        try:
            query = parse_qs(data.query, strict_parsing=True)
        except ValueError:
            return ""
        values = query.get("key", [])
        if set(query) == {"key"} and len(values) == 1:
            return values[0]
    return ""


def deny(ip: str) -> None:
    now = time.monotonic()
    start, count = failures.get(ip, (now, 0))
    if now - start >= 60:
        start, count = now, 0
    failures[ip] = (start, count + 1)
    failures.move_to_end(ip)
    while len(failures) > 2048:
        failures.popitem(last=False)
    raise HTTPException(401, "Publish authorization failed")


async def authorize_publish(data: MediaAuth) -> None:
    """Only called by MediaMTX on the private network. Never log request data."""
    if data.action != "publish":
        raise HTTPException(403, "Unsupported action")
    # Floor-like spellings fail closed instead of falling through to the
    # interpreter booth check.
    trimmed_path = data.path.rstrip("/")
    final_component = trimmed_path.rsplit("/", 1)[-1].casefold()
    if final_component in ("floor", "floor_backup") and (
        data.path.endswith("/") or not (data.path.endswith("/floor") or data.path.endswith("/floor_backup"))
    ):
        raise HTTPException(403, "Canonical floor path required")
    start, count = failures.get(data.ip, (0, 0))
    if count >= 10 and time.monotonic() - start < 60:
        raise HTTPException(429, "Publish authorization failed")
    if final_component not in ("floor", "floor_backup"):
        # Interpreter booths publish from the browser only, with a single-use
        # ticket issued to the active interpreter over the booth WebSocket.
        if data.protocol != "webrtc" or not await authorize_booth_publish(data.path, data.token):
            deny(data.ip)
        return
    match = FLOOR_PATH.fullmatch(data.path)
    if not match:
        deny(data.ip)
    slug = match.group(1)
    room_id = match.group(2)
    parsed_room_id = int(room_id)
    is_backup = match.group("backup") is not None
    secret = presented_secret(data)
    if not settings.program_ingest_enabled or not secret:
        # Bot-mode RTSP is checked after loading the room; all invalid external
        # program-ingest requests fail before doing password-grade KDF work.
        presented_digest = ""
    else:
        presented_digest = await asyncio.to_thread(digest_program_secret, secret)
    async with room_lock(parsed_room_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(Room, Event)
                    .join(Event)
                    .where(
                        Room.id == parsed_room_id,
                        Event.slug == slug,
                        Event.deleted_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                deny(data.ip)
            room, event = row
            if is_backup and (not room.program_backup_enabled or not has_valid_backup_credential(room)):
                raise HTTPException(403, "Backup contribution feed is not enabled or credential invalid")
            if room.floor_source == "jitsi_bot":
                # RTSP is not publicly exposed. WHIP must never impersonate the bot.
                try:
                    private = ipaddress.ip_address(data.ip).is_private
                except ValueError:
                    private = False
                if data.protocol == "rtsp" and private:
                    return
                deny(data.ip)
            if not settings.program_ingest_enabled or not publish_permitted(room, data.protocol, presented_digest):
                deny(data.ip)
            if data.protocol == "srt" and data.password:
                expected_enc = room.program_backup_key_enc if is_backup else room.program_srt_passphrase_enc
                expected_pass = decrypt_val(expected_enc) if expected_enc else None
                if not expected_pass or not secrets.compare_digest(data.password, expected_pass):
                    deny(data.ip)
            try:
                session_id = str(UUID(data.id))
            except ValueError:
                deny(data.ip)
            if is_backup:
                if not room.program_backup_enabled:
                    raise HTTPException(403, "Backup contribution feed is not enabled")
                current_h = health.get(room.id)
                backup_recent = bool(
                    current_h
                    and current_h.backup_reservation_started_at
                    and time.monotonic() - current_h.backup_reservation_started_at < 5.0
                )
                if current_h and current_h.backup_session_id and current_h.backup_session_id != session_id:
                    if backup_recent:
                        raise HTTPException(409, "A backup floor publisher is already connected or connecting")
                    try:
                        live_backup = await media_path(make_backup_floor_path(event.slug, room.id))
                        if live_backup and live_backup.source:
                            raise HTTPException(409, "A backup floor publisher is already connected")
                        current_h.backup_session_id = None
                    except httpx.HTTPError:
                        pass
            else:
                # A DB reservation can outlive a MediaMTX session across a process restart.
                # Clear it immediately when the path confirms that no source remains;
                # an active MediaMTX source retains the existing conflict response.
                reservation = health.get(room.id)
                reservation_recent = bool(
                    reservation
                    and reservation.reservation_started_at
                    and time.monotonic() - reservation.reservation_started_at < 5.0
                )
                if room.program_session_id and room.program_session_id != session_id and not reservation_recent:
                    try:
                        live_path = await media_path(make_mediamtx_path(event.slug, room.id, "floor"))
                        if live_path is None or live_path.source is None:
                            room.program_session_id = None
                            health.pop(room.id, None)
                    except httpx.HTTPError:
                        pass
                if room.program_session_id and room.program_session_id != session_id:
                    raise HTTPException(409, "A floor publisher is already connected or connecting")

        # Keep the cross-room lock around only the capacity transaction. Slow
        # MediaMTX calls and credential KDF work happen before reaching it.
        async with capacity_lock, get_session() as session:
            room = await session.get(Room, parsed_room_id)
            if room is None or not publish_permitted(room, data.protocol, presented_digest):
                deny(data.ip)
            if is_backup and (not room.program_backup_enabled or not has_valid_backup_credential(room)):
                raise HTTPException(403, "Backup contribution feed is not enabled or credential invalid")
            if not is_backup and room.program_session_id and room.program_session_id != session_id:
                raise HTTPException(409, "A floor publisher is already connected or connecting")
            count = await session.scalar(
                select(func.count())
                .select_from(Room)
                .where(
                    (Room.program_session_id.is_not(None) | Room.program_backup_connected_at.is_not(None)),
                    Room.floor_source == "program_ingest",
                )
            )
            is_new = not room.program_backup_connected_at if is_backup else not room.program_session_id
            if is_new and count >= settings.program_contribution_max:
                raise HTTPException(429, "Gateway at contribution capacity")
            if is_new and count >= settings.program_ingest_max_rooms:
                raise HTTPException(503, "Program ingest capacity reached")
            if is_backup:
                room.program_backup_connected_at = utc_now()
                await session.commit()
                current = health.setdefault(parsed_room_id, IngestHealth())
                current.backup_session_id = session_id
                current.backup_reservation_started_at = time.monotonic()
                return
            room.program_session_id = session_id
        current = health.setdefault(parsed_room_id, IngestHealth())
        current.reservation_started_at = time.monotonic()
        current.missing_since = None


def pipeline_problem(room: Room, event: Event) -> str | None:
    """Return why room settings block the canonical floor pipeline, if they do."""
    if not room.floor_transcription_enabled or room.floor_transcription_provider == "none":
        return "Floor transcription is disabled. Enable it in the room AI settings."
    if not room.floor_language_code:
        return "Select the floor source language in the room AI settings."
    provider = ProviderEnum(room.floor_transcription_provider)
    if provider != ProviderEnum.LOCAL and (not event.transcription_api_enabled or not get_api_key(event, provider)):
        return "Enable the event transcription API and configure its provider key."
    return None


async def media_path(path: str) -> MediaPath | None:
    response = await get_http_client().get(
        f"{settings.mediamtx_api_base}/v3/paths/get/{path}", timeout=MEDIA_CONTROL_TIMEOUT
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return MediaPath.model_validate(response.json())


async def media_paths() -> dict[str, MediaPath]:
    """Read every active path once so idle rooms do not generate repeated 404 logs."""
    response = await get_http_client().get(
        f"{settings.mediamtx_api_base}/v3/paths/list?itemsPerPage=1000", timeout=MEDIA_CONTROL_TIMEOUT
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ValueError("Invalid MediaMTX path list")
    paths = [MediaPath.model_validate(item) for item in body["items"]]
    return {path.name: path for path in paths}


async def kick(path: str, source: MediaSource | None) -> None:
    if source is None:
        return
    endpoint = {
        "webRTCSession": "webrtcsessions",
        "rtspSession": "rtspsessions",
        "rtmpConn": "rtmpconns",
        "rtmpsConn": "rtmpsconns",
        "srtConn": "srtconns",
        "srtConns": "srtconns",
    }.get(source.type)
    if endpoint is None:
        raise HTTPException(409, "Unsupported existing floor publisher; stop it before switching source")
    try:
        session_id = str(UUID(source.id))
    except ValueError:
        session_id = source.id
    response = await get_http_client().post(
        f"{settings.mediamtx_api_base}/v3/{endpoint}/kick/{session_id}", timeout=MEDIA_CONTROL_TIMEOUT
    )
    if response.status_code == 404:
        # Endpoint names are version-specific. A missing session is success,
        # but a still-live matching source means the kick endpoint is incompatible.
        current = await media_path(path)
        if not current or not current.source or current.source.id != source.id:
            return
    response.raise_for_status()


async def configure_source(
    room: Room, event: Event, action: str, offset_ms: int, mode: str | None = None
) -> str | None:
    """Caller holds the room lock and DB transaction; return a new secret only once.

    ``mode`` selects the ingest transport on enable; every action also stops the room's
    YouTube relay so a source or credential change never leaves a stale relay running.
    """
    path = make_mediamtx_path(event.slug, room.id, "floor")
    client = get_http_client()
    # Media-level duplicate protection also applies to named paths created by legacy helpers.
    response = await client.patch(
        f"{settings.mediamtx_api_base}/v3/config/paths/patch/{path}",
        json={"overridePublisher": False, "alwaysAvailable": False},
        timeout=MEDIA_CONTROL_TIMEOUT,
    )
    if response.status_code == 404:
        response = await client.post(
            f"{settings.mediamtx_api_base}/v3/config/paths/add/{path}",
            json={"overridePublisher": False, "alwaysAvailable": False},
            timeout=MEDIA_CONTROL_TIMEOUT,
        )
    response.raise_for_status()
    if room.floor_source == "jitsi_bot" and action == "enable":
        room.meeting_agent_enabled = False
        try:
            response = await client.post(
                f"{settings.floor_bot_base}/stop",
                json={"event_slug": event.slug, "room_id": room.id},
                timeout=MEDIA_CONTROL_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.RequestError:
            # A missing bot service is equivalent to an already-stopped bot.
            # MediaMTX ownership and the kick below remain authoritative.
            logger.warning("program_ingest floor_bot_unreachable room_id=%s", room.id)
    current_path = await media_path(path)
    backup_path = make_backup_floor_path(event.slug, room.id)
    current_backup = await media_path(backup_path)
    had_connection = bool(
        room.program_session_id
        or (current_path and current_path.source)
        or (current_backup and current_backup.source)
        or room.program_backup_connected_at
    )
    await kick(path, current_path.source if current_path else None)
    # Also kill a reserved session that has not negotiated audio yet.
    await kick(path, reserved_source(room))
    await kick(backup_path, current_backup.source if current_backup else None)
    await kick(backup_path, reserved_backup_source(room))
    await stop_transcription_worker(make_booth_id(event.slug, room.id, "floor"))
    try:
        await program_relay.stop_relay(room.id)
    except httpx.HTTPError:
        # The source is already kicked, so the relay has nothing to send; reconciliation retries the stop.
        logger.warning("program_relay stop_pending room_id=%s", room.id)
    room.program_session_id = None
    room.program_backup_connected_at = None
    room.program_active_feed = "primary"
    if had_connection:
        room.program_disconnected_at = utc_now()
    room.program_sync_offset_ms = offset_ms
    room.program_key_hash = None
    room.program_key_expires_at = None
    room.program_srt_passphrase_enc = None
    room.program_srt_passphrase_expires_at = None
    room.program_backup_key_enc = None
    room.program_backup_key_expires_at = None
    # Any source or credential change also invalidates an in-flight browser session.
    room.program_session_secret_hash = None
    room.program_session_secret_expires_at = None
    room.floor_source = "jitsi_bot" if action == "disable" else "program_ingest"
    if action == "enable":
        room.program_ingest_mode = mode or program_relay.WHIP_MODE
        if room.program_ingest_mode == program_relay.SRT_MODE:
            room.program_backup_enabled = True
    health.pop(room.id, None)
    secret = None
    if action in {"enable", "rotate"}:
        if room.program_ingest_mode == program_relay.SRT_MODE:
            secret = secrets.token_urlsafe(16)
            room.program_srt_passphrase_enc = encrypt_val(secret)
            room.program_srt_passphrase_expires_at = utc_now() + timedelta(days=30)
            backup_secret = None
            if room.program_backup_enabled:
                backup_secret = secrets.token_urlsafe(16)
                room.program_backup_key_enc = encrypt_val(backup_secret)
                room.program_backup_key_expires_at = utc_now() + timedelta(days=30)
            try:
                await apply_srt_passphrase_to_mediamtx(path, secret)
                _configured_srt_paths.add(path)
                if room.program_backup_enabled and backup_secret:
                    await apply_srt_passphrase_to_mediamtx(backup_path, backup_secret)
                    _configured_srt_paths.add(backup_path)
            except Exception:
                logger.warning("program_ingest srt_passphrase_apply_pending room_id=%s", room.id)
        else:
            secret = secrets.token_urlsafe(32)
            room.program_key_hash = await asyncio.to_thread(digest_program_secret, secret)
            room.program_key_expires_at = utc_now() + timedelta(days=30)
    elif action in {"revoke", "disable"}:
        try:
            await apply_srt_passphrase_to_mediamtx(path, "")
            await apply_srt_passphrase_to_mediamtx(backup_path, "")
            _configured_srt_paths.discard(path)
            _configured_srt_paths.discard(backup_path)
        except Exception:
            logger.warning("program_ingest srt_passphrase_clear_failed room_id=%s", room.id)
    logger.info("program_ingest action=%s room_id=%s", action, room.id)
    return secret


async def ensure_floor_path(path: str) -> None:
    """Create or normalize the MediaMTX floor path with duplicate protection off."""
    client = get_http_client()
    response = await client.patch(
        f"{settings.mediamtx_api_base}/v3/config/paths/patch/{path}",
        json={"overridePublisher": False, "alwaysAvailable": False},
        timeout=MEDIA_CONTROL_TIMEOUT,
    )
    if response.status_code == 404:
        response = await client.post(
            f"{settings.mediamtx_api_base}/v3/config/paths/add/{path}",
            json={"overridePublisher": False, "alwaysAvailable": False},
            timeout=MEDIA_CONTROL_TIMEOUT,
        )
    response.raise_for_status()


async def open_sender_session(room: Room, event: Event, offset_ms: int, ttl_secs: int) -> str:
    """Claim a free floor for the browser sender and mint a short-lived publish secret.

    Caller holds the room lock and DB transaction. Starting is rejected with 409 when
    an external encoder or another sender owns the floor; the Jitsi floor bot is an
    explicit takeover on the owner's click. Never touches the encoder credential.
    """
    path = make_mediamtx_path(event.slug, room.id, "floor")
    if room.floor_source == "program_ingest" and room.program_ingest_mode == program_relay.RELAY_MODE:
        raise HTTPException(
            409, "YouTube relay mode owns the floor; switch the room to independent outputs (WHIP) first."
        )
    if room.floor_source == "program_ingest" and room.program_ingest_mode == program_relay.RTMPS_MODE:
        raise HTTPException(
            409, "An RTMPS encoder owns the floor; switch the room to independent outputs (WHIP) first."
        )
    if room.floor_source == "program_ingest":
        # Floor already owned by program ingest; never displace a connected or
        # still-negotiating publisher (external encoder or another sender).
        current_path = await media_path(path)
        reservation = health.get(room.id)
        reservation_recent = bool(
            reservation
            and reservation.reservation_started_at
            and time.monotonic() - reservation.reservation_started_at < SENDER_PENDING_SECS
        )
        if (current_path and current_path.source is not None) or reservation_recent:
            raise HTTPException(
                409, "Another source is publishing to the floor; stop it before starting the browser sender."
            )
    else:
        # Explicit takeover from the Jitsi floor bot. Stop it before judging occupancy.
        room.meeting_agent_enabled = False
        try:
            response = await get_http_client().post(
                f"{settings.floor_bot_base}/stop",
                json={"event_slug": event.slug, "room_id": room.id},
                timeout=MEDIA_CONTROL_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.RequestError:
            logger.warning("program_ingest floor_bot_unreachable room_id=%s", room.id)
        current_path = await media_path(path)
    await ensure_floor_path(path)
    # Clear a lingering bot source on takeover, or a stale program-ingest reservation.
    await kick(path, current_path.source if current_path else None)
    if room.program_session_id:
        await kick(path, MediaSource(type="webRTCSession", id=room.program_session_id))
        room.program_session_id = None
    await stop_transcription_worker(make_booth_id(event.slug, room.id, "floor"))
    room.program_sync_offset_ms = offset_ms
    room.floor_source = "program_ingest"
    room.program_ingest_mode = program_relay.WHIP_MODE
    secret = secrets.token_urlsafe(32)
    room.program_session_secret_hash = await asyncio.to_thread(digest_program_secret, secret)
    room.program_session_secret_expires_at = utc_now() + timedelta(seconds=ttl_secs)
    # Mark the session pending so a racing duplicate start is rejected, not clobbered.
    pending = health.setdefault(room.id, IngestHealth())
    pending.reservation_started_at = time.monotonic()
    pending.missing_since = None
    logger.info("program_ingest sender_session_opened room_id=%s", room.id)
    return secret


async def close_sender_session(room: Room, event: Event, secret: str | None) -> None:
    """Release only the browser session whose secret matches the one presented.

    Binding to the secret stops a delayed or foreign DELETE from clearing a newer
    session's reservation or disconnecting an external encoder that owns the floor.
    """
    if not secret or not room.program_session_secret_hash:
        return
    digest = await asyncio.to_thread(digest_program_secret, secret)
    if not secrets.compare_digest(room.program_session_secret_hash, digest):
        return
    room.program_session_secret_hash = None
    room.program_session_secret_expires_at = None
    path = make_mediamtx_path(event.slug, room.id, "floor")
    if room.program_session_id:
        await kick(path, MediaSource(type="webRTCSession", id=room.program_session_id))
        room.program_session_id = None
        room.program_disconnected_at = utc_now()
    # Always drop the pending reservation for the matched session so retries are not blocked.
    health.pop(room.id, None)
    logger.info("program_ingest sender_session_closed room_id=%s", room.id)


async def generate_srt_passphrase(room: Room, session) -> str:
    """Generate, encrypt, store SRT passphrase. Returns plaintext for display once."""
    passphrase = secrets.token_urlsafe(16)
    room.program_srt_passphrase_enc = encrypt_val(passphrase)
    room.program_srt_passphrase_expires_at = utc_now() + timedelta(days=30)
    return passphrase


async def generate_backup_srt_passphrase(room: Room, session) -> str:
    """Generate, encrypt, store backup SRT passphrase. Returns plaintext for display once."""
    passphrase = secrets.token_urlsafe(16)
    room.program_backup_key_enc = encrypt_val(passphrase)
    room.program_backup_key_expires_at = utc_now() + timedelta(days=30)
    return passphrase


async def apply_srt_passphrase_to_mediamtx(path: str, passphrase: str) -> None:
    """POST /v3/config/paths/add/{path} with srtPublishPassphrase and publishPass. Called after DB commit."""
    client = get_http_client()
    body = {
        "srtPublishPassphrase": passphrase,
        "publishPass": passphrase,
        "overridePublisher": False,
    }
    response = await client.post(
        f"{settings.mediamtx_api_base}/v3/config/paths/add/{path}",
        json=body,
        timeout=MEDIA_CONTROL_TIMEOUT,
    )
    if response.status_code in (400, 409):
        response = await client.patch(
            f"{settings.mediamtx_api_base}/v3/config/paths/patch/{path}",
            json=body,
            timeout=MEDIA_CONTROL_TIMEOUT,
        )
    response.raise_for_status()


async def promote_primary_feed(room: Room, session) -> None:
    """Allow operator manual switchback to primary feed."""
    room.program_active_feed = "primary"
    current = health.get(room.id)
    if current:
        current.worker_config = None


async def stop_room_worker(current: IngestHealth, booth_id: str, room_id: int) -> bool:
    """Stop a room's floor STT worker without ever raising.

    Metadata is forgotten only after a successful stop, so a failed stop is retried next
    cycle. A stuck worker must never abort reconciliation of later rooms or the relay sync.
    """
    try:
        await stop_transcription_worker(booth_id)
    except Exception:  # noqa: BLE001 - processing cleanup must not abort room/relay reconciliation
        logger.warning("program_ingest processing_stop_failed room_id=%s", room_id)
        return False
    current.worker_config = None
    current.worker_started_at = None
    return True


def relay_request(room: Room, event: Event, path: MediaPath | None) -> tuple[str, str] | None:
    """Return (source path, destination) when this room's feed should be relayed to YouTube.

    Relay health is independent of language processing: captions being disabled or an STT
    outage never stops YouTube delivery, and a relay problem never stops captions.
    """
    if not settings.program_relay_enabled or room.program_ingest_mode != program_relay.RELAY_MODE:
        return None
    if not (room.program_relay_server and room.program_relay_key_enc):
        program_relay.relay_health[room.id] = program_relay.RelayHealth(
            "unconfigured", "Add the YouTube stream key to start relaying."
        )
        return None
    if not path or not path.ready or not path.source:
        program_relay.relay_health[room.id] = program_relay.RelayHealth(
            "idle", "Waiting for the RTMPS contribution feed; YouTube receives nothing until it arrives."
        )
        return None
    if not program_relay.relay_compatible(path.tracks):
        program_relay.relay_health[room.id] = program_relay.RelayHealth(
            "degraded", "Relay needs H.264 video and AAC audio. Check the encoder output settings."
        )
        return None
    try:
        url = program_relay.destination(room)
    except ValueError:
        program_relay.relay_health[room.id] = program_relay.RelayHealth(
            "degraded", "The saved destination cannot be decrypted; enter the YouTube stream key again."
        )
        return None
    active_path_name = (
        make_backup_floor_path(event.slug, room.id)
        if getattr(room, "program_active_feed", "primary") == "backup" and room.program_backup_enabled
        else make_mediamtx_path(event.slug, room.id, "floor")
    )
    return (active_path_name, url) if url else None


async def reconcile_room(room: Room, event: Event, path: MediaPath | None) -> tuple[str, str] | None:
    current = health.setdefault(room.id, IngestHealth())
    is_backup = getattr(room, "program_active_feed", "primary") == "backup" and room.program_backup_enabled
    path_name = (
        make_backup_floor_path(event.slug, room.id)
        if is_backup
        else make_mediamtx_path(event.slug, room.id, "floor")
    )
    booth_id = make_booth_id(event.slug, room.id, "floor")
    now = time.monotonic()
    srt_off = room.program_ingest_mode in program_relay.SRT_MODES and not settings.srt_ingest_enabled
    rtmps_off = room.program_ingest_mode == program_relay.RTMPS_MODE and not settings.rtmps_ingest_enabled
    valid_cred = has_valid_credential(room)
    if is_backup:
        backup_allowed = room.program_backup_enabled and valid_cred and bool(room.program_backup_connected_at)
        permitted = bool(settings.program_ingest_enabled and not srt_off and not rtmps_off and backup_allowed)
    else:
        # A short-lived sender secret gates new publishes, not an already-connected session.
        session_live = bool(
            path and path.source and room.program_session_id and path.source.id == room.program_session_id
        )
        if room.program_ingest_mode in program_relay.SRT_MODES:
            permitted = bool(settings.program_ingest_enabled and not srt_off and valid_cred and session_live)
        else:
            permitted = bool(settings.program_ingest_enabled and not rtmps_off and (valid_cred or session_live))
    if path and path.source and (not permitted or (not is_backup and path.source.id != room.program_session_id)):
        await kick(path_name, path.source)
        path = None
    if not path or not path.ready or not path.source:
        current.high_bitrate_cycles = 0
        current.last_cycle_bytes = -1
        if current.reservation_started_at is not None:
            grace_started_at = current.reservation_started_at
        else:
            current.missing_since = current.missing_since if current.missing_since is not None else now
            grace_started_at = current.missing_since
        current.state = "disconnected" if room.program_connected_at else "waiting"
        if permitted:
            current.reason = "No publisher. Reconnect the encoder."
        elif rtmps_off:
            current.reason = "RTMPS ingest is switched off on this server; ask the server operator."
        else:
            current.reason = "Credential revoked or expired; rotate it to publish."
        current.ingest_state, current.ingest_reason = current.state, current.reason
        current.processing_state, current.processing_reason = "idle", "No contribution feed to process."
        if now - grace_started_at >= settings.program_ingest_disconnect_grace_secs:
            await stop_room_worker(current, booth_id, room.id)
            if room.program_session_id and not is_backup:
                await kick(path_name, reserved_source(room))
                room.program_session_id = None
                room.program_disconnected_at = utc_now()
            current.reservation_started_at = None
        relay_request(room, event, None)
        return None

    # Gateway limits enforcement: Codec inspection
    if room.program_ingest_mode in program_relay.SRT_MODES:
        tracks = [t.strip() for t in path.tracks] if path.tracks else []
        has_h264 = any(t in {"H264", "H.264"} or t.upper() in {"H264", "H.264"} for t in tracks)
        has_aac = any(t in {"AAC", "MPEG-4 Audio"} or t.upper() in {"AAC", "MPEG-4 AUDIO"} for t in tracks)
        allowed_codecs = {"H264", "H.264", "AAC", "MPEG-4 Audio"}
        all_allowed = bool(tracks) and all(
            t in allowed_codecs or t.upper() in {"H264", "H.264", "AAC", "MPEG-4 AUDIO"} for t in tracks
        )
        conforming = has_h264 and has_aac and all_allowed
        if not conforming:
            await kick(path_name, path.source)
            current.state = "rejected_codec"
            current.ingest_state = "rejected_codec"
            current.reason = "Non-conforming codec detected. Expected H.264 video and AAC audio."
            current.ingest_reason = current.reason
            relay_request(room, event, None)
            return None

    # Gateway limits enforcement: Bitrate enforcement (> 12,000 kbps for 5 consecutive cycles)
    if current.last_cycle_bytes >= 0 and current.last_cycle_time > 0:
        dt = now - current.last_cycle_time
        delta_bytes = path.bytesReceived - current.last_cycle_bytes
        if delta_bytes >= 0:
            effective_dt = dt if dt > 0 else 2.0
            bitrate_kbps = (delta_bytes * 8) / (effective_dt * 1000)
            if bitrate_kbps > 12000:
                current.high_bitrate_cycles += 1
            else:
                current.high_bitrate_cycles = 0
    else:
        current.high_bitrate_cycles = 0

    current.last_cycle_bytes = path.bytesReceived
    current.last_cycle_time = now

    if current.high_bitrate_cycles >= 5:
        await kick(path_name, path.source)
        current.state = "rejected_limits"
        current.ingest_state = "rejected_limits"
        current.reason = "Inbound bitrate exceeded 12,000 kbps for 5 consecutive cycles."
        current.ingest_reason = current.reason
        relay_request(room, event, None)
        return None
    if current.missing_since is not None or current.reservation_started_at is not None or current.last_bytes == -1:
        room.program_connected_at = utc_now()
        current.last_progress = now
    current.missing_since = None
    current.reservation_started_at = None
    if path.source:
        if current.last_session_id is not None and path.source.id != current.last_session_id:
            current.reconnect_count += 1
        current.last_session_id = path.source.id
    current.codecs = path.tracks
    if path.bytesReceived != current.last_bytes:
        current.last_bytes = path.bytesReceived
        current.last_progress = now
    relay = relay_request(room, event, path)
    try:
        await reconcile_processing(room, event, path, current, booth_id, now)
    except ValueError:
        # Worker cap, an undecryptable provider key or an invalid provider affect language
        # processing only; the relay decision above still stands.
        current.state, current.reason = "degraded", "Language processing could not start; retrying automatically."
        current.processing_state, current.processing_reason = current.state, current.reason
        logger.warning("program_ingest processing_failed room_id=%s", room.id)
        # A worker may still run with the previous configuration; stop it before forgetting it.
        await stop_room_worker(current, booth_id, room.id)
    return relay


async def reconcile_processing(
    room: Room, event: Event, path: MediaPath, current: IngestHealth, booth_id: str, now: float
) -> None:
    """Drive the floor STT worker for a connected feed and report ingest/processing health."""
    problem = None
    current.ingest_state, current.ingest_reason = "connected", "Authenticated contribution feed connected."
    supported_audio = {"Opus", "G711", "G722", "LPCM", "MPEG-4 Audio", "AAC"}
    if not any(
        codec in supported_audio or codec.upper() in {"OPUS", "G711", "G722", "LPCM", "MPEG-4 AUDIO", "AAC"}
        for codec in path.tracks
    ):
        problem = (
            "No supported audio track. Configure the encoder to send AAC audio."
            if room.program_ingest_mode in (*program_relay.RTMPS_MODES, *program_relay.SRT_MODES)
            else "No supported audio track. Configure the WHIP encoder to send Opus audio."
        )
        current.ingest_state, current.ingest_reason = "degraded", problem
    elif now - current.last_progress >= settings.program_ingest_stall_secs:
        problem = "Media has stalled. Check the encoder and network."
        current.ingest_state, current.ingest_reason = "degraded", problem
    else:
        problem = pipeline_problem(room, event)
    if problem:
        current.state, current.reason = "degraded", problem
        current.processing_state, current.processing_reason = "degraded", problem
        await stop_room_worker(current, booth_id, room.id)
        return
    current.state, current.reason = "receiving", "Audio received; starting transcription."
    current.processing_state, current.processing_reason = "starting", "Starting the transcription worker."
    api_key = get_api_key(event, ProviderEnum(room.floor_transcription_provider))
    backup_active = getattr(room, "program_active_feed", "primary") == "backup"
    stream_path = make_backup_floor_path(event.slug, room.id) if backup_active else None
    config = (room.floor_transcription_provider, room.floor_transcription_model, room.floor_language_code, stream_path)
    if current.worker_config and current.worker_config != config:
        if not await stop_room_worker(current, booth_id, room.id):
            return
    started = await start_transcription_worker(
        event_slug=event.slug,
        language_code="floor",
        booth_id=booth_id,
        broadcast_callback=broadcast_transcription,
        provider=room.floor_transcription_provider,
        model_size=room.floor_transcription_model,
        config=ProviderConfig(api_key=api_key),
        transcription_language=room.floor_language_code,
        room_id=room.id,
        stream_path=stream_path,
        restart_on_key_change=True,
    )
    current.worker_config = config
    if started is True or current.worker_started_at is None:
        # Read the clock again: replacing the old worker can take a while, and the grace period starts now.
        current.worker_started_at = time.monotonic()
    last_audio = audio_progress.get(booth_id)
    if now - (last_audio or current.worker_started_at) > settings.program_ingest_stall_secs:
        current.state, current.reason = "degraded", "Audio decoder stalled or failed; restarting the processing worker."
        current.processing_state, current.processing_reason = current.state, current.reason
        await stop_room_worker(current, booth_id, room.id)
    elif last_audio:
        current.state, current.reason = "processing", "Decoded audio received; transcription worker active."
        current.processing_state, current.processing_reason = current.state, current.reason


async def stop_deleted_event_workers() -> None:
    """Stop the floor worker of rooms whose event was deleted.

    A delete can commit after a reconcile pass already read the room and started its worker, and
    ``health`` can lose the room before that (``close_sender_session``), so find the workers among
    the running ones instead: once the event is deleted the reconciler no longer sees the room.
    """
    booth_ids: dict[int, str] = {}
    for booth_id in list(active_workers):
        try:
            _, room_id, language_code = parse_booth_id(booth_id)
        except ValueError:
            continue
        if language_code == "floor":
            booth_ids[room_id] = booth_id
    if not booth_ids:
        return
    async with get_session() as session:
        deleted_room_ids = (
            await session.scalars(
                select(Room.id).join(Event).where(Room.id.in_(booth_ids), Event.deleted_at.is_not(None))
            )
        ).all()
    for room_id in deleted_room_ids:
        await stop_room_worker(health.get(room_id) or IngestHealth(), booth_ids[room_id], room_id)


async def release_deleted_event_sessions() -> None:
    """Disconnect the encoder and free the reservation of rooms whose event was deleted.

    The reconciler no longer sees these rooms, so ``reconcile_room`` never releases them. A kept
    reservation would count against ``program_ingest_max_rooms``. A failed kick keeps the
    reservation, so the next pass retries it.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Room.id, Event.slug)
                .join(Event)
                .where(
                    Room.floor_source == "program_ingest",
                    Room.program_session_id.is_not(None),
                    Event.deleted_at.is_not(None),
                )
            )
        ).all()
    for room_id, slug in rows:
        try:
            async with room_lock(room_id), get_session() as session:
                room = await session.get(Room, room_id)
                if room and room.program_session_id:
                    await kick(make_mediamtx_path(slug, room_id, "floor"), reserved_source(room))
                    room.program_session_id = None
                    room.program_disconnected_at = utc_now()
        except (httpx.HTTPError, ValueError, HTTPException, SQLAlchemyError):
            logger.warning("program_ingest deleted_event_release_failed room_id=%s", room_id)


async def stop_deleted_event_relays() -> None:
    """Stop the relays of rooms whose event was deleted, without needing the MediaMTX path list.

    ``program_relay.sync`` does this on a normal pass, but it only runs once the path list is
    read. Relays of other rooms are left alone.
    """
    try:
        room_ids = {int(relay_id) for relay_id in await program_relay.list_relays() if relay_id.isdigit()}
        if not room_ids:
            return
        async with get_session() as session:
            deleted_room_ids = (
                await session.scalars(
                    select(Room.id).join(Event).where(Room.id.in_(room_ids), Event.deleted_at.is_not(None))
                )
            ).all()
    except Exception:  # noqa: BLE001 - upstream bodies may contain secrets; the next pass retries
        logger.warning("program_ingest deleted_event_relay_list_failed")
        return
    for room_id in deleted_room_ids:
        try:
            await program_relay.stop_relay(room_id)
        except Exception:  # noqa: BLE001
            logger.warning("program_ingest deleted_event_relay_stop_failed room_id=%s", room_id)


async def reconcile_once() -> None:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Room.id, Room.program_ingest_mode)
                .join(Event)
                .where(Room.floor_source == "program_ingest", Event.deleted_at.is_(None))
            )
        ).all()
    ids = [room_id for room_id, _ in rows]
    relay_rooms = {room_id for room_id, mode in rows if mode == program_relay.RELAY_MODE}
    await stop_deleted_event_workers()
    await release_deleted_event_sessions()
    try:
        snapshots = await media_paths() if ids else {}
    except (httpx.HTTPError, ValueError):
        for room_id in ids:
            current = health.setdefault(room_id, IngestHealth())
            current.state, current.reason = (
                "degraded",
                "Media control unavailable; retrying automatically.",
            )
            current.ingest_state, current.ingest_reason = current.state, current.reason
        path_list_outage.failed()
        if settings.program_relay_enabled:
            await stop_deleted_event_relays()
        return
    if ids:
        path_list_outage.recovered()
    wanted: dict[int, tuple[str, str]] = {}
    for room_id in ids:
        try:
            async with room_lock(room_id), get_session() as session:
                row = (
                    await session.execute(
                        select(Room, Event).join(Event).where(Room.id == room_id, Event.deleted_at.is_(None))
                    )
                ).first()
                if row and row[0].floor_source == "program_ingest":
                    room, event = row[0], row[1]
                    # MediaMTX restart recovery: re-apply publishPass
                    if room.program_ingest_mode == program_relay.SRT_MODE and room.program_srt_passphrase_enc:
                        passphrase = decrypt_val(room.program_srt_passphrase_enc)
                        if passphrase:
                            primary_floor = make_mediamtx_path(event.slug, room_id, "floor")
                            if primary_floor not in snapshots or primary_floor not in _configured_srt_paths:
                                try:
                                    await apply_srt_passphrase_to_mediamtx(primary_floor, passphrase)
                                    _configured_srt_paths.add(primary_floor)
                                except Exception:
                                    logger.warning("program_ingest srt_passphrase_restore_failed room_id=%s", room_id)
                            if room.program_backup_enabled and room.program_backup_key_enc:
                                backup_passphrase = decrypt_val(room.program_backup_key_enc)
                                if backup_passphrase:
                                    backup_floor = make_backup_floor_path(event.slug, room_id)
                                    if backup_floor not in snapshots or backup_floor not in _configured_srt_paths:
                                        try:
                                            await apply_srt_passphrase_to_mediamtx(backup_floor, backup_passphrase)
                                            _configured_srt_paths.add(backup_floor)
                                        except Exception:
                                            logger.warning(
                                                "program_ingest srt_backup_passphrase_restore_failed room_id=%s", room_id
                                            )

                    primary_path_name = make_mediamtx_path(event.slug, room_id, "floor")
                    backup_path_name = make_backup_floor_path(event.slug, room_id)
                    primary_snapshot = snapshots.get(primary_path_name)
                    backup_snapshot = snapshots.get(backup_path_name)

                    current = health.setdefault(room_id, IngestHealth())
                    now = time.monotonic()

                    primary_connected = bool(primary_snapshot and primary_snapshot.ready and primary_snapshot.source)
                    primary_stalled = bool(
                        primary_connected
                        and current.last_progress > 0
                        and (now - current.last_progress >= TAKEOVER_STALL_SECS)
                    )
                    if room.program_connected_at is None:
                        # Primary has not connected yet: give primary a startup grace period before failover
                        backup_age = (
                            (utc_now() - aware(room.program_backup_connected_at)).total_seconds()
                            if room.program_backup_connected_at
                            else 0.0
                        )
                        primary_failed = backup_age >= TAKEOVER_STALL_SECS
                    else:
                        primary_failed = (not primary_connected) or primary_stalled

                    backup_connected = bool(backup_snapshot and backup_snapshot.ready and backup_snapshot.source)
                    current.backup_connected = backup_connected
                    if not backup_connected and room.program_backup_connected_at:
                        room.program_backup_connected_at = None
                        current.backup_session_id = None

                    # Reset program_active_feed if backup is not enabled or stale
                    if getattr(room, "program_active_feed", "primary") == "backup":
                        if not room.program_backup_enabled or (
                            not backup_connected and primary_connected and not primary_stalled
                        ):
                            room.program_active_feed = "primary"
                            current.worker_config = None

                    # Deterministic Backup Takeover
                    if (
                        primary_failed
                        and room.program_backup_enabled
                        and backup_connected
                        and getattr(room, "program_active_feed", "primary") == "primary"
                    ):
                        # Re-verify primary path state under room_lock to eliminate TOCTOU race
                        if not primary_connected:
                            fresh_primary = await media_path(primary_path_name)
                            if fresh_primary and fresh_primary.ready and fresh_primary.source:
                                primary_connected = True
                                primary_failed = False
                        if primary_failed:
                            booth_id = make_booth_id(event.slug, room_id, "floor")
                            await stop_room_worker(current, booth_id, room_id)
                            room.program_active_feed = "backup"
                            current.worker_config = None
                            logger.info("program_ingest backup_promoted room_id=%s", room_id)

                    active_snapshot = (
                        backup_snapshot
                        if getattr(room, "program_active_feed", "primary") == "backup" and room.program_backup_enabled
                        else primary_snapshot
                    )

                    request = await reconcile_room(room, event, active_snapshot)
                    if request:
                        wanted[room_id] = request
        except (httpx.HTTPError, ValueError, SQLAlchemyError):
            current = health.setdefault(room_id, IngestHealth())
            current.state, current.reason = (
                "degraded",
                "Media control or processing unavailable; retrying automatically.",
            )
            if room_id in relay_rooms:
                # Not in ``wanted``, so sync stops the relay; never keep reporting a stale "live".
                program_relay.relay_health[room_id] = program_relay.RelayHealth(
                    "degraded", "Media control unavailable; relay paused and retrying automatically."
                )
            # Do not log exception bodies: upstream errors may contain credentials.
            logger.warning("program_ingest reconcile_failed room_id=%s", room_id)
    if settings.program_relay_enabled:
        # Also runs with no relay rooms so orphaned relays from a restart or switch are stopped.
        await program_relay.sync(wanted, relay_rooms)
    for room_id in set(health) - set(ids):
        health.pop(room_id, None)


async def reconcile_loop() -> None:
    while True:
        try:
            await reconcile_once()
        except SQLAlchemyError:
            database_outage.failed()
        else:
            database_outage.recovered()
        await asyncio.sleep(2)
