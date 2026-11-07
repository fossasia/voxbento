"""Control plane for room-scoped RTMPS relay mode (#749).

The portal owns configuration, authorization and status only. The ``program-relay``
service pulls the floor over private RTSP and pushes it to YouTube; media never
traverses this process. The YouTube stream key is Fernet-encrypted at rest, write-only
from the API, and is only decrypted to hand a relay its destination.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from portal.config import settings
from portal.crypto import decrypt_val, encrypt_val
from portal.globals import get_http_client
from portal.models import Room
from portal.outage_log import OutageLog

logger = logging.getLogger(__name__)
WHIP_MODE = "whip"
RELAY_MODE = "rtmps_relay"
MODES = (WHIP_MODE, RELAY_MODE)
STREAM_KEY = re.compile(r"[A-Za-z0-9_-]{4,128}")
RELAY_CONTROL_TIMEOUT = 3.0
# MediaMTX track names for the documented H.264/AAC contribution contract.
RELAY_VIDEO_CODECS = {"H264"}
RELAY_AUDIO_CODECS = {"MPEG-4 Audio"}
YOUTUBE_DEFAULT_SERVER = "rtmps://a.rtmps.youtube.com/live2"
SERVER_URL_MAX = 255  # rooms.program_relay_server column length
# YouTube's plain-RTMP ingest hosts and their RTMPS equivalents.
YOUTUBE_RTMPS_HOSTS = {"a.rtmp.youtube.com": "a.rtmps.youtube.com", "b.rtmp.youtube.com": "b.rtmps.youtube.com"}


@dataclass
class RelayHealth:
    state: str = "idle"
    reason: str = "Relay starts when an authenticated RTMPS feed arrives."


relay_health: dict[int, RelayHealth] = {}
list_outage = OutageLog(logger, "program_relay list_unavailable")


def allowed_hosts() -> set[str]:
    return {host.strip().casefold() for host in settings.program_relay_allowed_hosts.split(",") if host.strip()}


def normalize_destination(server_url: str, stream_key: str) -> str:
    """Return the canonical server URL, or raise ValueError with an operator-facing message.

    Only RTMPS to an allow-listed host is accepted, so the relay can never be pointed at
    the private network or an arbitrary third party.
    """
    try:
        parts = urlsplit(server_url.strip())
        port = parts.port
    except ValueError:
        raise ValueError("Enter a valid RTMPS server URL.") from None
    host = (parts.hostname or "").casefold()
    if parts.scheme == "rtmp" and host in YOUTUBE_RTMPS_HOSTS and port in {None, 1935}:
        # YouTube Studio shows its plain-RTMP URL by default; the same ingest is served over RTMPS.
        parts, host, port = parts._replace(scheme="rtmps"), YOUTUBE_RTMPS_HOSTS[host], None
    if parts.scheme != "rtmps":
        raise ValueError("The destination must use rtmps:// (TLS).")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Paste the server URL without credentials or query parameters.")
    if host not in allowed_hosts():
        raise ValueError("That destination host is not allowed. Use YouTube's RTMPS server URL.")
    path = parts.path.rstrip("/")
    if not path or path.count("/") > 3:
        raise ValueError("Use the full server URL from YouTube Studio, e.g. rtmps://a.rtmps.youtube.com/live2.")
    if not STREAM_KEY.fullmatch(stream_key.strip()):
        raise ValueError("The stream key may contain only letters, digits, '-' and '_'.")
    netloc = host if port is None else f"{host}:{port}"
    normalized = f"rtmps://{netloc}{path}"
    if len(normalized) > SERVER_URL_MAX:
        raise ValueError("The server URL is too long.")
    return normalized


def split_pasted_key(server_url: str | None, stream_key: str) -> tuple[str, str]:
    """Accept a full ``<server>/<key>`` URL pasted into the key field; default the server to YouTube."""
    key = stream_key.strip()
    if "://" in key:
        server_url, _, key = key.rstrip("/").rpartition("/")
    return (server_url or "").strip() or YOUTUBE_DEFAULT_SERVER, key.strip()


def store_destination(room: Room, server_url: str | None, stream_key: str) -> None:
    server_url, stream_key = split_pasted_key(server_url, stream_key)
    room.program_relay_server = normalize_destination(server_url, stream_key)
    room.program_relay_key_enc = encrypt_val(stream_key)
    room.program_relay_updated_at = datetime.now(timezone.utc)


def clear_destination(room: Room) -> None:
    room.program_relay_server = None
    room.program_relay_key_enc = None
    room.program_relay_updated_at = datetime.now(timezone.utc)


def destination(room: Room) -> str | None:
    if not room.program_relay_server or not room.program_relay_key_enc:
        return None
    return f"{room.program_relay_server}/{decrypt_val(room.program_relay_key_enc)}"


def fingerprint(source: str, url: str) -> str:
    """Non-secret change detector: a keyed digest, so the stream key is not brute-forceable from it."""
    return hmac.new(
        settings.program_relay_control_secret.encode(), f"{source}\n{url}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def relay_compatible(codecs: list[str]) -> bool:
    return bool(RELAY_VIDEO_CODECS & set(codecs)) and bool(RELAY_AUDIO_CODECS & set(codecs))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.program_relay_control_secret}"}


async def list_relays() -> dict[str, dict]:
    response = await get_http_client().get(
        f"{settings.program_relay_base}/relays", headers=_headers(), timeout=RELAY_CONTROL_TIMEOUT
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("relays"), dict):
        raise ValueError("Invalid relay list")
    return body["relays"]


async def stop_relay(room_id: int, forget_health: bool = True) -> None:
    """Stop one room's relay. Callers treat failure as cleanup the reconciler retries."""
    if not settings.program_relay_enabled:
        return
    response = await get_http_client().delete(
        f"{settings.program_relay_base}/relays/{room_id}", headers=_headers(), timeout=RELAY_CONTROL_TIMEOUT
    )
    response.raise_for_status()
    if forget_health:
        relay_health.pop(room_id, None)


async def start_relay(room_id: int, source: str, url: str) -> dict:
    response = await get_http_client().put(
        f"{settings.program_relay_base}/relays/{room_id}",
        headers=_headers(),
        json={"source": source, "destination": url, "fingerprint": fingerprint(source, url)},
        timeout=RELAY_CONTROL_TIMEOUT,
    )
    if response.status_code == 503:
        return {"state": "capacity", "reason": "Relay service capacity reached. Stop another room's relay."}
    response.raise_for_status()
    return response.json()


async def sync(wanted: dict[int, tuple[str, str]], relay_rooms: set[int]) -> None:
    """Converge the relay service on ``wanted`` ({room_id: (source path, destination)}).

    Fails closed: relays beyond ``PROGRAM_RELAY_MAX`` are not started, relays for rooms
    that no longer qualify (stopped, revoked, switched or deleted) are stopped, and orphans
    left by a portal restart are removed.
    """
    try:
        live = await list_relays()
    except Exception:  # noqa: BLE001 - never surface upstream bodies; they may contain secrets
        for room_id in wanted:
            relay_health[room_id] = RelayHealth("degraded", "Relay service unavailable; retrying automatically.")
        list_outage.failed()
        return
    list_outage.recovered()
    for relay_id in set(live) - {str(room_id) for room_id in wanted}:
        try:
            # A relay-mode room keeps the health explaining why its relay was stopped.
            await stop_relay(int(relay_id), forget_health=int(relay_id) not in relay_rooms)
        except Exception:  # noqa: BLE001
            logger.warning("program_relay stop_failed room_id=%s", relay_id)
    # Rooms already relaying keep their slot; new rooms are admitted in a stable order.
    order = sorted(wanted, key=lambda room_id: (str(room_id) not in live, room_id))
    for index, room_id in enumerate(order):
        source, url = wanted[room_id]
        if index >= settings.program_relay_max:
            relay_health[room_id] = RelayHealth(
                "capacity",
                f"Relay capacity ({settings.program_relay_max} rooms) reached; YouTube is not receiving this room.",
            )
            continue
        current = live.get(str(room_id))
        try:
            if current is None or current.get("fingerprint") != fingerprint(source, url):
                current = await start_relay(room_id, source, url)
        except Exception:  # noqa: BLE001
            relay_health[room_id] = RelayHealth(
                "degraded", "Relay service rejected the request; retrying automatically."
            )
            logger.warning("program_relay start_failed room_id=%s", room_id)
            continue
        relay_health[room_id] = RelayHealth(str(current.get("state", "starting")), str(current.get("reason", "")))
    for room_id in set(relay_health) - relay_rooms:
        relay_health.pop(room_id, None)
