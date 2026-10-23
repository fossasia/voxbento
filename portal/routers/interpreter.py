from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from portal.auth import get_booth_sessions, resolve_booth_role
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import (
    get_booth_by_id,
    get_session,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
)
from portal.globals import _JS_CACHE_BUST, asset_version_context, booths
from portal.models import DBBooth, Event
from portal.roles import INTERPRETER, ROLE_RANK, can_go_live, can_set_active, highest_role, role_label
from portal.user_menu import resolve_user_menu, user_menu_context
from portal.utils import _ensure_mediamtx_path, _make_jitsi_url, safe_redirect

# Loads the signed-in person once per request for the shared header and user menu.
router = APIRouter(dependencies=[Depends(resolve_user_menu)])

_BASE_DIR = Path(__file__).resolve().parent.parent

templates = Jinja2Templates(
    directory=str(_BASE_DIR / "templates"), context_processors=[asset_version_context, user_menu_context]
)
templates.env.filters["role_label"] = role_label


@router.get("/interpreter")
async def interpreter_landing_page(request: Request) -> Any:
    """Central lobby for interpreters to run pre-flight checks and view assigned booths."""
    sessions = get_booth_sessions(request)
    if not sessions:
        return safe_redirect(url="/login?next=/interpreter", status_code=status.HTTP_303_SEE_OTHER)

    booths_by_id: dict[str, dict[str, Any]] = {}

    user_sess = sessions.get("user_token")
    invite_sess = sessions.get("session_token")

    if user_sess and user_sess.get("sub"):
        try:
            uid = int(user_sess["sub"])
            async with get_session() as session:
                bms = await list_booth_memberships_for_user(session, uid)
                rms = await list_room_memberships_for_user(session, uid)
                ems = await list_memberships_for_user(session, uid)

                booth_roles: dict[int, str] = {}
                for bm in bms:
                    if bm.booth and bm.booth.event and bm.booth.event.deleted_at is None and can_go_live(bm.role):
                        prev = booth_roles.get(bm.booth_id)
                        if prev is None or ROLE_RANK.get(bm.role, 0) > ROLE_RANK.get(prev, 0):
                            booth_roles[bm.booth_id] = bm.role

                room_roles: dict[int, str] = {}
                for rm in rms:
                    if rm.room and rm.room.event and rm.room.event.deleted_at is None and can_go_live(rm.role):
                        prev = room_roles.get(rm.room_id)
                        if prev is None or ROLE_RANK.get(rm.role, 0) > ROLE_RANK.get(prev, 0):
                            room_roles[rm.room_id] = rm.role

                event_roles: dict[int, str] = {}
                for em in ems:
                    if em.event and em.event.deleted_at is None and can_go_live(em.role):
                        prev = event_roles.get(em.event_id)
                        if prev is None or ROLE_RANK.get(em.role, 0) > ROLE_RANK.get(prev, 0):
                            event_roles[em.event_id] = em.role

                booth_conditions = []
                if booth_roles:
                    booth_conditions.append(DBBooth.id.in_(list(booth_roles.keys())))
                if room_roles:
                    booth_conditions.append(DBBooth.room_id.in_(list(room_roles.keys())))
                if event_roles:
                    booth_conditions.append(DBBooth.event_id.in_(list(event_roles.keys())))

                if booth_conditions:
                    stmt = (
                        select(DBBooth)
                        .options(joinedload(DBBooth.event), joinedload(DBBooth.room))
                        .join(Event)
                        .where(
                            Event.deleted_at.is_(None),
                            or_(*booth_conditions),
                        )
                    )
                    res = await session.execute(stmt)
                    db_booths = res.scalars().unique().all()
                    for b in db_booths:
                        bid = make_booth_id(b.event.slug, b.room_id, b.language_code)
                        applicable: list[str] = []
                        if b.id in booth_roles:
                            applicable.append(booth_roles[b.id])
                        if b.room_id in room_roles:
                            applicable.append(room_roles[b.room_id])
                        if b.event_id in event_roles:
                            applicable.append(event_roles[b.event_id])
                        assigned_role = highest_role(applicable)
                        if not assigned_role:
                            continue
                        mem_booth = booths.get_booth_sync(bid)
                        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
                        booths_by_id[bid] = {
                            "booth_id": bid,
                            "is_live": is_live,
                            "event_name": b.event.display_name,
                            "language_name": b.language_name,
                            "room_name": b.room.display_name if b.room else "",
                            "room_id": b.room_id,
                            "event_slug": b.event.slug,
                            "language_code": b.language_code,
                            "role": assigned_role,
                        }
        except ValueError:
            pass

    if invite_sess and "event_slug" in invite_sess and "language_code" in invite_sess and "room_id" in invite_sess:
        bid = make_booth_id(invite_sess["event_slug"], invite_sess["room_id"], invite_sess["language_code"])
        invite_role = invite_sess.get("role", INTERPRETER)
        if bid in booths_by_id:
            existing_role = booths_by_id[bid]["role"]
            if ROLE_RANK.get(invite_role, 0) > ROLE_RANK.get(existing_role, 0):
                booths_by_id[bid]["role"] = invite_role
        else:
            mem_booth = booths.get_booth_sync(bid)
            is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
            async with get_session() as session:
                stmt = (
                    select(DBBooth)
                    .options(joinedload(DBBooth.event), joinedload(DBBooth.room))
                    .join(Event)
                    .where(
                        Event.slug == invite_sess["event_slug"],
                        Event.deleted_at.is_(None),
                        DBBooth.room_id == invite_sess["room_id"],
                        DBBooth.language_code == invite_sess["language_code"],
                    )
                )
                res = await session.execute(stmt)
                b = res.scalar_one_or_none()
                event_name = b.event.display_name if b and b.event else invite_sess["event_slug"]
                language_name = b.language_name if b else invite_sess["language_code"]
                room_name = b.room.display_name if b and b.room else ""
            booths_by_id[bid] = {
                "booth_id": bid,
                "is_live": is_live,
                "event_name": event_name,
                "language_name": language_name,
                "room_name": room_name,
                "room_id": invite_sess.get("room_id"),
                "event_slug": invite_sess["event_slug"],
                "language_code": invite_sess["language_code"],
                "role": invite_role,
            }

    # Decided after membership and invite roles are merged, so the Mission Control link follows the final role.
    for booth in booths_by_id.values():
        booth["can_coordinate"] = can_set_active(booth["role"])
    my_booths = list(booths_by_id.values())
    return templates.TemplateResponse(
        request=request, name="interpreter_landing.html", context={"my_booths": my_booths, "js_version": _JS_CACHE_BUST}
    )


@router.get("/interpreter/{event_slug}/{room_id}/{language_code}")
async def interpreter_booth_by_identity(
    request: Request, event_slug: str, room_id: int, language_code: str, token: str = "", language: str = ""
) -> Any:
    """Booth page addressed by event_slug and language_code (preferred URL).

    Requires a valid session_token (invite link) or user_token (registered user).
    The granted role is passed to the template so the client cannot self-promote.
    """
    sessions = get_booth_sessions(request)
    if not sessions:
        return safe_redirect(
            url=f"/login?next=/interpreter/{event_slug}/{room_id}/{language_code}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    booth_id = make_booth_id(event_slug, room_id, language_code)
    granted_role = await resolve_booth_role(list(sessions.values()), booth_id)
    if granted_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have a role in this event. Ask the event's owners to add you to its team.",
        )
    mediamtx_path = make_mediamtx_path(event_slug, room_id, language_code)
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
            .options(joinedload(DBBooth.room), joinedload(DBBooth.event))
            .where(Event.slug == event_slug)
            .where(Event.deleted_at.is_(None))
            .where(DBBooth.room_id == room_id)
            .where(DBBooth.language_code == language_code)
        )
        db_booth = (await session.scalars(stmt)).first()
        # Shown in the booth's top bar instead of the slug and language code.
        event_name = db_booth.event.display_name if db_booth and db_booth.event else event_slug
        room_name = db_booth.room.display_name if db_booth and db_booth.room else ""
        language_name = db_booth.language_name if db_booth else display_language
        relay_whep_url = None
        relay_language_name = None
        if db_booth and db_booth.room:
            if db_booth.room.jitsi_url:
                room_jitsi_url = db_booth.room.jitsi_url
            if db_booth.room.relay_booth_id:
                relay_b = await get_booth_by_id(session, db_booth.room.relay_booth_id)
                if relay_b:
                    relay_channel = make_mediamtx_path(event_slug, relay_b.room_id, relay_b.language_code)
                    relay_whep_url = f"{settings.mediamtx_whip_base}/{relay_channel}/whep"
                    relay_language_name = relay_b.language_name
    default_jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, settings.default_jitsi_room)
    final_jitsi_url = room_jitsi_url or default_jitsi_url
    user_sess = sessions.get("user_token", {})
    display_name = user_sess.get("display_name", "") or user_sess.get("email", "")
    return templates.TemplateResponse(
        request=request,
        name="interpreter_booth.html",
        context={
            "booth_id": booth_id,
            "booth_token": token,
            "booth_language": display_language,
            "booth_channel_id": channel_id,
            "event_slug": event_slug,
            "event_name": event_name,
            "room_name": room_name,
            "language_name": language_name,
            "room_id": room_id,
            "language_code": language_code,
            "whip_url": whip_url,
            "whep_url": whep_url,
            "relay_whep_url": relay_whep_url,
            "relay_language_name": relay_language_name,
            "granted_role": granted_role,
            "can_go_live": can_go_live(granted_role),
            "display_name": display_name,
            "default_jitsi_room": settings.default_jitsi_room,
            "jitsi_url": final_jitsi_url,
            "jitsi_domain": settings.effective_jitsi_domain,
            "jitsi_base_url": settings.effective_jitsi_base_url,
            "mediamtx_whip_base": settings.mediamtx_whip_base,
            "js_version": _JS_CACHE_BUST,
        },
    )
