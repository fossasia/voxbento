"""Relay interpreting (#1382): the booth a room's interpreters listen to instead of the floor.

``rooms.relay_booth_id`` names the relay booth. Booth pages get it in ``booth:joined`` and as a
``booth:relay`` push whenever a coordinator changes it or the relay booth goes on or off air, so
nobody reloads mid-session. The relay booth itself is never offered its own channel: hearing your
own interpretation back through MediaMTX, late, is a loopback (AGENTS.md invariant 2).

The portal runs as one replica, so the room → relay map is kept in memory. It is reloaded from
the database when a booth joins and after every change that can move the relay.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from portal.booth_identity import make_booth_id, make_mediamtx_path, parse_booth_id
from portal.config import settings
from portal.database import get_session
from portal.globals import booths
from portal.models import Event, Room

RoomKey = tuple[str, int]
"""(event slug, room id): the part of a booth ID that names its room."""


@dataclass(frozen=True)
class RelaySource:
    db_id: int
    booth_id: str
    language_code: str
    language_name: str
    mediamtx_path: str


# Rooms whose relay has been loaded. A room with no relay maps to None.
_room_relays: dict[RoomKey, RelaySource | None] = {}
# On-air state of each relay booth as last pushed, so routine booth state changes push nothing.
_on_air: dict[str, bool] = {}


def room_key(booth_id: str) -> RoomKey | None:
    try:
        event_slug, room_id, _ = parse_booth_id(booth_id)
    except ValueError:
        return None
    return event_slug, room_id


def is_on_air(booth_id: str) -> bool:
    """On air means what Mission Control shows: someone is publishing to the booth's channel."""
    booth = booths.get_booth_sync(booth_id)
    return booth is not None and booth.ingest_status == "connected"


async def load_room_relay(room_id: int, event_slug: str | None = None) -> tuple[RoomKey, RelaySource | None] | None:
    """Read the room's relay from the database and cache it.

    Returns None when the room does not exist, its event is deleted or, with *event_slug*, the
    room belongs to another event.
    """
    async with get_session() as session:
        stmt = (
            select(Room)
            .join(Event, Room.event_id == Event.id)
            .options(joinedload(Room.event), joinedload(Room.relay_booth))
            .where(Room.id == room_id, Event.deleted_at.is_(None))
        )
        room = (await session.scalars(stmt)).first()
        if room is None or (event_slug is not None and room.event.slug != event_slug):
            return None
        slug = room.event.slug
        relay = room.relay_booth
        source = None
        if relay is not None:
            source = RelaySource(
                db_id=relay.id,
                booth_id=make_booth_id(slug, relay.room_id, relay.language_code),
                language_code=relay.language_code,
                language_name=relay.language_name,
                mediamtx_path=make_mediamtx_path(slug, relay.room_id, relay.language_code),
            )
    key = (slug, room_id)
    _room_relays[key] = source
    if source is not None:
        _on_air[source.booth_id] = is_on_air(source.booth_id)
    return key, source


def cached_room_relay(key: RoomKey) -> RelaySource | None:
    return _room_relays.get(key)


def cached_rooms() -> list[RoomKey]:
    return list(_room_relays)


def rooms_relaying_from_db_booth(db_booth_id: int) -> list[RoomKey]:
    """Rooms whose cached relay is this booth (by database ID, which survives a language change)."""
    return [key for key, source in _room_relays.items() if source is not None and source.db_id == db_booth_id]


def relay_payload(source: RelaySource | None, booth_id: str) -> dict | None:
    """What one booth's page needs to listen to the relay, or None.

    None as well for the relay booth itself, which must never hear its own channel.
    """
    if source is None or source.booth_id == booth_id:
        return None
    return {
        "booth_id": source.booth_id,
        "language_code": source.language_code,
        "language_name": source.language_name,
        "whep_url": f"{settings.mediamtx_whip_base}/{source.mediamtx_path}/whep",
        "on_air": is_on_air(source.booth_id),
    }


def relay_fields(source: RelaySource | None, booth_id: str) -> dict:
    """The relay as sent in ``booth:joined`` and ``booth:relay``.

    ``relay_booth_id`` names the room's relay for everyone, so the relay booth's page and
    Mission Control can tell it apart from "no relay"; ``relay`` is what this booth plays.
    """
    return {
        "relay_booth_id": source.booth_id if source is not None else None,
        "relay": relay_payload(source, booth_id),
    }


def note_ingest_status(booth_id: str, ingest_status: str | None) -> list[RoomKey]:
    """Record a booth's on-air state; return the rooms to tell when a relay booth went on or off air."""
    rooms = [key for key, source in _room_relays.items() if source is not None and source.booth_id == booth_id]
    if not rooms:
        return []
    on_air = ingest_status == "connected"
    if _on_air.get(booth_id) == on_air:
        return []
    _on_air[booth_id] = on_air
    return rooms


def reset() -> None:
    """Forget every cached relay (tests)."""
    _room_relays.clear()
    _on_air.clear()
