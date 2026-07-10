from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from portal.auth import get_booth_session, resolve_booth_role
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import get_booth_by_id, get_session, list_booth_memberships_for_user
from portal.globals import _JS_CACHE_BUST, booths
from portal.models import DBBooth, Event
from portal.utils import _ensure_mediamtx_path, _make_jitsi_url, safe_redirect

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@router.get("/interpreter")
async def interpreter_landing_page(request: Request) -> Any:
    """Central lobby for interpreters to run pre-flight checks and view assigned booths."""
    payload = get_booth_session(request)
    if payload is None:
        return safe_redirect(url="/login?next=/interpreter", status_code=status.HTTP_303_SEE_OTHER)
    my_booths = []
    if "event_slug" in payload and "language_code" in payload:
        bid = make_booth_id(payload["event_slug"], payload["language_code"])
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        async with get_session() as session:
            stmt = (
                select(DBBooth)
                .options(joinedload(DBBooth.event), joinedload(DBBooth.room))
                .join(Event)
                .where(Event.slug == payload["event_slug"], DBBooth.language_code == payload["language_code"])
            )
            res = await session.execute(stmt)
            b = res.scalar_one_or_none()
            event_name = b.event.display_name if b and b.event else payload["event_slug"]
            language_name = b.language_name if b else payload["language_code"]
            room_name = b.room.display_name if b and b.room else ""
        my_booths.append(
            {
                "booth_id": bid,
                "is_live": is_live,
                "event_name": event_name,
                "language_name": language_name,
                "room_name": room_name,
                "event_slug": payload["event_slug"],
                "language_code": payload["language_code"],
                "role": payload.get("role", "interpreter"),
            }
        )
    elif payload.get("sub") and payload.get("user"):
        try:
            uid = int(payload["sub"])
            async with get_session() as session:
                bms = await list_booth_memberships_for_user(session, uid)
                for bm in bms:
                    bid = make_booth_id(bm.booth.event.slug, bm.booth.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
                    my_booths.append(
                        {
                            "booth_id": bid,
                            "is_live": is_live,
                            "event_name": bm.booth.event.display_name,
                            "language_name": bm.booth.language_name,
                            "room_name": bm.booth.room.display_name if bm.booth.room else "",
                            "event_slug": bm.booth.event.slug,
                            "language_code": bm.booth.language_code,
                            "role": bm.role,
                        }
                    )
        except ValueError:
            pass
    return templates.TemplateResponse(
        request, "interpreter_landing.html", {"my_booths": my_booths, "js_version": _JS_CACHE_BUST}
    )


@router.get("/interpreter/{event_slug}/{language_code}")
async def interpreter_booth_by_identity(
    request: Request, event_slug: str, language_code: str, token: str = "", language: str = ""
) -> Any:
    """Booth page addressed by event_slug and language_code (preferred URL).

    Requires a valid session_token (invite link) or user_token (registered user).
    The granted role is passed to the template so the client cannot self-promote.
    """
    payload = get_booth_session(request)
    if payload is None:
        return safe_redirect(
            url=f"/login?next=/interpreter/{event_slug}/{language_code}", status_code=status.HTTP_303_SEE_OTHER
        )
    booth_id = make_booth_id(event_slug, language_code)
    granted_role = await resolve_booth_role(payload, booth_id)
    if granted_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have a role assigned for this event. Ask an admin to assign you one.",
        )
    booth_id = make_booth_id(event_slug, language_code)
    mediamtx_path = make_mediamtx_path(event_slug, language_code)
    channel_id = mediamtx_path
    display_language = language or language_code.upper()
    await _ensure_mediamtx_path(channel_id)
    whip_url = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whip"
    whep_url = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whep"
    room_jitsi_url = None
    async with get_session() as session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .options(joinedload(DBBooth.room))
            .where(Event.slug == event_slug)
            .where(DBBooth.language_code == language_code)
        )
        db_booth = (await session.scalars(stmt)).first()
        relay_whep_url = None
        relay_language_name = None
        if db_booth and db_booth.room:
            if db_booth.room.jitsi_url:
                room_jitsi_url = db_booth.room.jitsi_url
            if db_booth.room.relay_booth_id:
                relay_b = await get_booth_by_id(session, db_booth.room.relay_booth_id)
                if relay_b:
                    relay_channel = make_mediamtx_path(event_slug, relay_b.language_code)
                    relay_whep_url = f"{settings.mediamtx_whip_base}/{relay_channel}/whep"
                    relay_language_name = relay_b.language_name
    default_jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, settings.default_jitsi_room)
    final_jitsi_url = room_jitsi_url or default_jitsi_url
    display_name = payload.get("display_name", "") or payload.get("email", "")
    return templates.TemplateResponse(
        request,
        "interpreter_booth.html",
        {
            "booth_id": booth_id,
            "booth_token": token,
            "booth_language": display_language,
            "booth_channel_id": channel_id,
            "event_slug": event_slug,
            "language_code": language_code,
            "whip_url": whip_url,
            "whep_url": whep_url,
            "relay_whep_url": relay_whep_url,
            "relay_language_name": relay_language_name,
            "granted_role": granted_role,
            "display_name": display_name,
            "default_jitsi_room": settings.default_jitsi_room,
            "jitsi_url": final_jitsi_url,
            "jitsi_domain": settings.effective_jitsi_domain,
            "jitsi_base_url": settings.effective_jitsi_base_url,
            "mediamtx_whip_base": settings.mediamtx_whip_base,
            "js_version": _JS_CACHE_BUST,
        },
    )
