from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import require_oauth_scope
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.database import get_db_session
from portal.globals import booths
from portal.models import (
    DBBooth,
    DeveloperAccount,
    Event,
    EventMembership,
    OAuthAuditLog,
    OAuthClient,
    OAuthToken,
    Room,
    RoomMembership,
    RoomTranslationLanguage,
)
from portal.rate_limit import auth_rate_limiter
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


async def _verify_token_rbac(db: AsyncSession, token: OAuthToken, event: Event, room_id: int | None = None) -> None:
    """Ensure the OAuth token is valid for this event, AND the underlying user still has RBAC permissions."""
    if token.event_id != event.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token not authorized for this event")

    # Check if user is super admin or event owner
    from portal.models import User

    user = await db.get(User, token.user_id)
    if user and getattr(user, "is_super_admin", False):
        return

    # Check Event Owner
    evt_mem = await db.execute(
        select(EventMembership).where(
            EventMembership.user_id == token.user_id,
            EventMembership.event_id == event.id,
            EventMembership.role == "event_owner",
        )
    )
    if evt_mem.scalars().first():
        return

    # Check Room Coordinator if room is specified
    if room_id is not None:
        rm_mem = await db.execute(
            select(RoomMembership).where(
                RoomMembership.user_id == token.user_id,
                RoomMembership.room_id == room_id,
                RoomMembership.role == "room_coordinator",
            )
        )
        if rm_mem.scalars().first():
            return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User lost RBAC access to this resource")


@router.get("/events/{event_slug}")
async def get_event(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event)

    return {
        "id": event.id,
        "slug": event.slug,
        "display_name": event.display_name,
        "owner_id": event.owner_id,
        "created_at": event.created_at.isoformat(),
    }


@router.get("/events/{event_slug}/rooms")
async def get_rooms(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event)

    room_result = await db.execute(select(Room).where(Room.event_id == event.id))
    rooms = room_result.scalars().all()

    return [
        {
            "id": r.id,
            "display_name": r.display_name,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat(),
        }
        for r in rooms
    ]


class EventCreate(BaseModel):
    slug: str
    name: str


@router.post("/events/", status_code=status.HTTP_201_CREATED)
async def create_event(
    request: Request,
    payload: EventCreate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:write")),
):
    client_ip = request.client.host if request.client else "unknown"
    if await auth_rate_limiter.is_rate_limited(f"create_event_{client_ip}"):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests")

    from portal.booth_identity import validate_event_slug

    try:
        slug = validate_event_slug(payload.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(select(Event).where(Event.slug == slug))
    existing_event = result.scalars().first()

    if existing_event:
        if existing_event.deleted_at is None:
            # Update the display name if the event already exists (e.g. from OAuth auto-provisioning)
            if existing_event.display_name != payload.name:
                existing_event.display_name = payload.name
                db.add(existing_event)
                await db.flush()
            return {"id": existing_event.id, "slug": existing_event.slug, "name": existing_event.display_name}
        existing_event.deleted_at = None
        existing_event.display_name = payload.name
        event = existing_event
        action = "event.restored"
        status_code_ret = status.HTTP_200_OK
    else:
        event = Event(slug=slug, display_name=payload.name)
        db.add(event)
        await db.flush()

        db.add(EventMembership(user_id=token.user_id, event_id=event.id, role="event_owner"))

        client_res = await db.execute(select(OAuthClient).where(OAuthClient.id == token.client_id))
        client = client_res.scalars().first()
        if client:
            dev_acc_res = await db.execute(
                select(DeveloperAccount).where(DeveloperAccount.id == client.developer_account_id)
            )
            dev_acc = dev_acc_res.scalars().first()
            if dev_acc and dev_acc.user_id != token.user_id:
                db.add(EventMembership(user_id=dev_acc.user_id, event_id=event.id, role="support"))

        action = "event.created"
        status_code_ret = status.HTTP_201_CREATED

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action=action,
        request_path="/api/v1/events/",
        status_code=status_code_ret,
    )
    db.add(audit)
    await db.flush()

    return {"status": "success", "event_slug": event.slug}


@router.delete("/events/{event_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:write")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    await _verify_token_rbac(db, token, event)

    active_booths = await booths.list_booths_for_event(event_slug)
    has_active_session = any(b.get("ingest_status") == "connected" for b in active_booths)
    if has_active_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Cannot delete event while active booths are running."
        )

    for b in active_booths:
        await booths.remove_booth(event_slug, b["room_id"], b["language_code"])

    event.deleted_at = datetime.now(timezone.utc)

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action="event.deleted",
        request_path=f"/api/v1/events/{event_slug}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    db.add(audit)
    await db.flush()
    return None


class RoomUpsert(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    target_languages: list[str] = []


@router.put("/events/{event_slug}/rooms/{eventyay_room_id}")
async def upsert_room(
    event_slug: str,
    eventyay_room_id: str,
    payload: RoomUpsert,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    from portal.booth_identity import make_mediamtx_path
    from portal.globals import booths

    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event)

    room_res = await db.execute(
        select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
    )
    room = room_res.scalars().first()
    if room:
        room.display_name = payload.name
        action = "room.updated"
        status_code_ret = status.HTTP_200_OK
    else:
        room = Room(event_id=event.id, eventyay_room_id=eventyay_room_id, display_name=payload.name)
        db.add(room)
        await db.flush()
        action = "room.created"
        status_code_ret = status.HTTP_201_CREATED

    lang_res = await db.execute(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room.id))
    existing_langs = {rl.language_code: rl for rl in lang_res.scalars().all()}

    booth_res = await db.execute(select(DBBooth).where(DBBooth.room_id == room.id))
    existing_booths = {b.language_code: b for b in booth_res.scalars().all()}

    requested_langs = set(payload.target_languages)

    # Safe Delete Removed Booths & Languages
    for code, b in existing_booths.items():
        if code not in requested_langs:
            # Active Session Guard — use BoothRegistry.get_booth_sync() (not .items())
            from portal.booth_identity import make_booth_id

            booth_id = make_booth_id(event_slug, room.id, code)
            active_booth = booths.get_booth_sync(booth_id)
            if active_booth is not None:
                has_connected = active_booth.ingest_status == "connected"
                if has_connected:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot remove language '{code}' while it has an active session running.",
                    )
            await booths.remove_booth(event_slug, room.id, code)
            await db.delete(b)
            db.add(
                OAuthAuditLog(
                    token_id=token.id,
                    client_id=token.client_id,
                    action="booth.deleted",
                    request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}/booths/{code}",
                    status_code=status.HTTP_200_OK,
                )
            )

    for code, rl in existing_langs.items():
        if code not in requested_langs:
            await db.delete(rl)

    # Create Missing Booths & Languages
    for code in requested_langs:
        if code not in existing_langs:
            db.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=code))

        if code not in existing_booths:
            new_booth = DBBooth(room_id=room.id, language_code=code, event_id=event.id, language_name=code)
            db.add(new_booth)
            existing_booths[code] = new_booth
            db.add(
                OAuthAuditLog(
                    token_id=token.id,
                    client_id=token.client_id,
                    action="booth.created",
                    request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}/booths/{code}",
                    status_code=status.HTTP_201_CREATED,
                )
            )

    # Audit Logging
    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action=action,
        request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}",
        status_code=status_code_ret,
    )
    db.add(audit)

    await db.flush()

    # Construct Response with Canonical WHEP URLs
    final_booth_res = await db.execute(select(DBBooth).where(DBBooth.room_id == room.id))
    final_booths = final_booth_res.scalars().all()

    returned_booths = []
    from portal.config import settings

    try:
        for b in final_booths:
            whip_path = make_mediamtx_path(event.slug, room.id, b.language_code)
            whep_url = f"{settings.mediamtx_whip_base}/{whip_path}/whep"
            returned_booths.append({"language": b.language_code, "whip_path": whip_path, "whep_url": whep_url})
    except Exception:
        logger.exception("Error generating canonical WHEP URLs")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    return {"status": "success", "room_id": room.id, "booths": returned_booths}


@router.delete("/events/{event_slug}/rooms/{eventyay_room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    event_slug: str,
    eventyay_room_id: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event)

    room_res = await db.execute(
        select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
    )
    room = room_res.scalars().first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    room_booths = [b for b in await booths.list_booths_for_event(event_slug) if b["room_id"] == room.id]

    has_active_session = any(b.get("ingest_status") == "connected" for b in room_booths)
    if has_active_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Cannot delete room while active booths are running."
        )

    for b in room_booths:
        await booths.remove_booth(event_slug, b["room_id"], b["language_code"])

    await db.delete(room)

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action="room.deleted",
        request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    db.add(audit)
    await db.flush()
    return None


@router.patch("/events/{event_slug}/rooms/{room_id}/settings")
async def patch_room_settings(
    event_slug: str,
    room_id: int,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)
    return {"status": "success"}


@router.get("/events/{event_slug}/rooms/{room_id}/booths")
async def list_booths(
    event_slug: str,
    room_id: int,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    result = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id))
    booths_list = result.scalars().all()

    return [
        {
            "id": b.id,
            "language_code": b.language_code,
            "whip_path": make_mediamtx_path(event.slug, room_id, b.language_code),
            "created_at": b.created_at.isoformat(),
        }
        for b in booths_list
    ]


@router.get("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def get_booth(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    result = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id, DBBooth.language_code == language_code))
    booth = result.scalars().first()
    if not booth:
        raise HTTPException(status_code=404, detail="Booth not found")

    return {
        "id": booth.id,
        "language_code": booth.language_code,
        "whip_path": make_mediamtx_path(event.slug, room_id, booth.language_code),
        "created_at": booth.created_at.isoformat(),
    }


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def create_booth(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:write")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    result = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id, DBBooth.language_code == language_code))
    if result.scalars().first():
        raise HTTPException(status_code=409, detail="Booth already exists")

    from portal.booth_identity import make_mediamtx_path

    booth = DBBooth(room_id=room_id, language_code=language_code, event_id=event.id)
    db.add(booth)
    await db.flush()

    whip_path = make_mediamtx_path(event.slug, room_id, language_code)
    return {"status": "success", "booth_id": booth.id, "whip_path": whip_path}


@router.delete("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def delete_booth_endpoint(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:write")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    result = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id, DBBooth.language_code == language_code))
    booth = result.scalars().first()
    if not booth:
        raise HTTPException(status_code=404, detail="Booth not found")

    from portal.database import delete_booth

    await delete_booth(db, booth.id)
    return {"status": "deleted"}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/start")
async def start_transcription(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:manage")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    booth_id = make_booth_id(event_slug, language_code)

    # Actually start transcription using the registry
    if booth_id not in booths:
        raise HTTPException(status_code=400, detail="Booth not active in memory")

    # We call the real worker
    await start_transcription_worker(booth_id, event.id)

    return {"status": "started", "booth_id": booth_id}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/stop")
async def stop_transcription(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:manage")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    booth_id = make_booth_id(event_slug, language_code)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped", "booth_id": booth_id}


@router.get("/events/{event_slug}/rooms/{room_id}/status")
async def get_transcription_status(
    event_slug: str,
    room_id: int,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    # Collect statuses for all booths in the room
    result = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id))
    booths_list = result.scalars().all()

    statuses = {}
    for b in booths_list:
        bid = make_booth_id(event_slug, b.language_code)
        booth = booths.get(bid)
        statuses[b.language_code] = {
            "is_active": bool(booth),
            "transcription_running": bool(booth and getattr(booth, "transcription_task", None)),
        }

    return {"room_id": room_id, "statuses": statuses}


@router.get("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcripts/export")
async def export_transcript(
    event_slug: str,
    room_id: int,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("transcripts:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    return {"status": "success", "content": "Transcription export not fully implemented"}


@router.post("/events/{event_slug}/rooms/{room_id}/listener-token")
async def provision_listener_token(
    event_slug: str,
    room_id: int,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id)

    from portal.auth import create_listener_token

    t = create_listener_token(event_slug=event.slug)

    return {"listener_token": t}
