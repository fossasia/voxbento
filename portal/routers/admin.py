from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import secrets
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pycountry
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from starlette.datastructures import FormData

from portal import meeting_source
from portal import roles as roles_module
from portal.auth import (
    coordinates_room,
    get_access_flags,
    get_accessible_event_ids,
    get_current_user,
    require_admin,
    require_event_owner,
    require_hosting_access,
    require_owner_or_coordinator,
    require_room_event_access,
    require_user,
    resolve_principal,
    resolve_request_principal,
)
from portal.booth_identity import make_booth_id, make_mediamtx_path, validate_event_slug, validate_language_code
from portal.config import settings
from portal.crypto import encrypt_val
from portal.database import (
    USER_FILTERS,
    USER_SORT_COLUMNS,
    close_hosting_request,
    count_active_admins,
    count_events,
    count_users,
    create_api_key,
    create_auth_token,
    create_booth,
    create_event_with_owner,
    create_invite_token,
    create_room,
    create_user,
    delete_booth,
    delete_event,
    delete_room,
    get_api_keys_for_event,
    get_booth_by_id,
    get_db_session,
    get_developer_account,
    get_event_by_id,
    get_event_by_slug,
    get_hosting_request,
    get_invite_token,
    get_open_hosting_request,
    get_room_by_id,
    get_session,
    get_user_by_email,
    get_user_by_id,
    list_all_booths_for_events,
    list_booth_memberships_for_user,
    list_booths_for_event,
    list_booths_for_room,
    list_events,
    list_memberships_for_booth,
    list_memberships_for_event,
    list_memberships_for_room,
    list_memberships_for_user,
    list_open_hosting_requests,
    list_room_memberships_for_user,
    list_rooms_for_event,
    list_tokens_for_booth,
    list_users,
    remove_booth_membership,
    remove_event_membership,
    remove_room_membership,
    revoke_api_key,
    revoke_invite_token,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.globals import _JS_CACHE_BUST, asset_version_context, booths
from portal.hosting_requests import DECLINE_REASON_MAX, format_event_dates, multi_line
from portal.models import (
    HOSTING_REQUEST_DECLINED,
    HOSTING_REQUEST_GRANTED,
    HOSTING_REQUEST_OPEN,
    TOKEN_TYPE_HOSTING_INVITE,
    TOKEN_TYPE_MAGIC_LINK,
    AIVocabularyEntry,
    AuthToken,
    BoothMembership,
    BoothTranslationLanguage,
    DBBooth,
    EmailDelivery,
    EmailTemplate,
    Event,
    EventMembership,
    HostingRequest,
    Room,
    RoomMembership,
    RoomTranslationLanguage,
    TranscriptSegment,
    TranscriptTranslation,
    User,
    utc_now,
)
from portal.my_events import list_my_events
from portal.notifications import (
    NOTIFICATIONS,
    default_logo_url,
    email_branding,
    get_definition,
    preview_notification,
    publish_draft,
    queue_notification,
    save_draft,
    save_email_branding,
    template_content,
)
from portal.program_ingest import room_lock
from portal.rate_limit import check_rate_limit
from portal.roles import EVENT_OWNER, assignment_error, role_label
from portal.room_setup import (
    AI_VOICE,
    EngineCatalog,
    RoomPlan,
    RoomSummary,
    apply_plan,
    blank_plan,
    default_jitsi_url,
    engine_catalog,
    load_snapshot,
    parse_plan,
    room_audio_choice,
    summarize,
)
from portal.room_setup import ALL_LANGUAGES as ROOM_ALL_LANGUAGES
from portal.room_setup import COMMON_LANGUAGES as ROOM_COMMON_LANGUAGES
from portal.room_setup import DELIVERY_EXPLANATIONS as ROOM_DELIVERY_EXPLANATIONS
from portal.room_setup import DELIVERY_HINTS as ROOM_DELIVERY_HINTS
from portal.room_setup import DELIVERY_LABELS as ROOM_DELIVERY_LABELS
from portal.room_setup import SPEECH_MODELS as ROOM_SPEECH_MODELS
from portal.room_setup import VOICE_PRESETS as ROOM_VOICE_PRESETS
from portal.room_setup import language_name as room_language_name
from portal.team import ROLE_CHOICES as TEAM_ROLE_CHOICES
from portal.team import TeamError, load_team
from portal.team import add_member as add_team_member
from portal.team import create_invite_link as create_team_invite_link
from portal.team import parse_place as parse_team_place
from portal.team import remove_assignment as remove_team_assignment
from portal.team import revoke_invite_link as revoke_team_invite_link
from portal.transcription import ALLOWED_MODELS, ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.vocabulary import (
    VocabularyOverlapIndex,
    VocabularyScopeError,
    parse_vocabulary_csv,
    serialize_vocabulary_csv,
    validate_vocabulary_scope,
)
from portal.tts.worker import invalidate_config as invalidate_tts_config
from portal.user_admin import ERROR_MESSAGES as USER_ADMIN_ERRORS
from portal.user_admin import FIELDS as USER_ACCESS_FIELDS
from portal.user_admin import AccessChange, UserAdminError, delete_user_account, is_last_active_admin, set_user_access
from portal.user_menu import resolve_user_menu, user_menu_context
from portal.utils import _check_mediamtx, _make_jitsi_url, get_client_ip, safe_redirect, verify_same_origin
from portal.websockets.manager import broadcast_transcription, manager
from portal.websockets.revocation import revoke_booths_for_deleted_entity, revoke_membership_push
from portal.workspace_routing import management_template_context, management_url

_BASE_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)
templates = Jinja2Templates(
    directory=str(_BASE_DIR / "templates"),
    context_processors=[management_template_context, user_menu_context, asset_version_context],
)
# Templates read role names and labels from portal.roles (#757).
templates.env.globals["roles"] = roles_module
templates.env.filters["role_label"] = role_label


def _apply_api_key_updates(
    event: Event,
    updates: tuple[tuple[str, str | None, bool | None], ...],
) -> None:
    for attribute, value, clear in updates:
        if clear:
            setattr(event, attribute, None)
        elif value and (trimmed_value := value.strip()):
            setattr(event, attribute, encrypt_val(trimmed_value))


INVITE_LINK_SECONDS = 7 * 86400


async def _send_admin_invite(session, user: User, role_name: str, context_name: str, target_url: str):
    token_val = secrets.token_hex(32)
    # Invitations wait in an inbox, so they last as long as hosting invitations, not a sign-in link's 15 minutes.
    token = await create_auth_token(
        session, jti=token_val, user_id=user.id, token_type=TOKEN_TYPE_MAGIC_LINK, expires_in_seconds=INVITE_LINK_SECONDS
    )
    invite_url = f"{settings.public_base_url}/auth/magic/{token_val}?next={urllib.parse.quote(target_url)}"
    await queue_notification(
        session,
        "role_assignment",
        user.email,
        {"role_name": roles_module.role_label(role_name), "context_name": context_name, "invite_url": invite_url},
        expires_at=token.expires_at,
        idempotency_key=f"role-invite:{token_val}",
        auth_token_id=token.id,
    )


# Loads the signed-in person once per request for the shared header and user menu.
router = APIRouter(dependencies=[Depends(resolve_user_menu)])


def _email_sample_context(key: str) -> dict[str, object]:
    """Use non-sensitive links and representative content in previews and test sends."""
    values: dict[str, object] = {
        "verification_link": "https://example.invalid/auth/verify/sample-link",
        "magic_link": "https://example.invalid/auth/magic/sample-link",
        "reset_link": "https://example.invalid/auth/reset/sample-link",
        "invite_url": "https://example.invalid/auth/magic/sample-link",
        "login_url": "https://example.invalid/login",
        "role_name": "Interpreter",
        "context_name": "Example Conference",
        "is_existing_user": True,
        "requester_name": "Alex Example",
        "requester_email": "alex@example.invalid",
        "organization": "Example Organization",
        "event_name": "Example Conference",
        "event_dates": "1–2 October 2026",
        "languages": "English, Spanish",
        "message": "This is a safe preview of this notification.",
        "review_url": "https://example.invalid/admin/hosting-access/",
        "display_name": "Alex",
        "firstName": "Alex",
        "lastName": "Example",
        "email": "alex@example.invalid",
        "companyName": "Example Organization",
        "phoneNumber": "+1 555 0100",
        "country": "Exampleland",
        "industry": "Events",
        "companySize": "50–100",
        "languageSupport": "English, Spanish",
        "notes": "This is a safe preview.",
        "account_url": "https://example.invalid/account",
        "reason": "We can only offer hosting to registered organizations at the moment.",
        "admin_url": "https://example.invalid/admin/",
    }
    return {name: values[name] for name in get_definition(key).variables}


def _email_definition(key: str):
    """The notification for *key*, or 404 for an unknown email type."""
    try:
        return get_definition(key)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email not found.") from None


async def _render_email_editor(
    request: Request, db: AsyncSession, key: str, *, subject: str | None = None, html: str | None = None,
    error: str = "", status_code: int = 200,
):
    definition = _email_definition(key)
    saved_subject, saved_html, version = await template_content(db, definition.key, draft=True)
    return templates.TemplateResponse(
        request,
        "admin/email_template.html",
        {
            "definition": definition,
            "subject": saved_subject if subject is None else subject,
            "html": saved_html if html is None else html,
            "version": version,
            "error": error,
            "sample_context": _email_sample_context(definition.key),
        },
        status_code=status_code,
    )


@router.get("/admin/emails/", dependencies=[Depends(require_admin)])
async def admin_emails(
    request: Request, db: AsyncSession = Depends(get_db_session), error: str = "", logo_url: str | None = None
):
    deliveries = list(
        (
            await db.scalars(
                select(EmailDelivery).order_by(EmailDelivery.created_at.desc()).limit(20)
            )
        ).all()
    )
    template_keys = set((await db.scalars(select(EmailTemplate.key))).all())
    counts = dict((await db.execute(select(EmailDelivery.status, func.count()).group_by(EmailDelivery.status))).all())
    branding = await email_branding(db)
    return templates.TemplateResponse(
        request,
        "admin/emails.html",
        {
            "notifications": NOTIFICATIONS,
            "template_keys": template_keys,
            "deliveries": deliveries,
            "counts": counts,
            "labels": {definition.key: definition.label for definition in NOTIFICATIONS},
            "branding": branding,
            "logo_url": (branding.logo_url or "") if logo_url is None else logo_url,
            "default_logo_url": default_logo_url(preview=True),
            "error": error,
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


@router.post("/admin/emails/branding", dependencies=[Depends(require_admin)])
async def admin_email_branding(
    request: Request,
    show_logo: str = Form(""),
    logo_url: str = Form(""),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    try:
        await save_email_branding(db, show_logo=show_logo == "on", logo_url=logo_url, editor_id=int(user["sub"]))
    except ValueError as exc:
        return await admin_emails(request, db, error=str(exc), logo_url=logo_url)
    return safe_redirect(url="/admin/emails/?branding=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/emails/{key}/", dependencies=[Depends(require_admin)])
async def admin_email_template(key: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    return await _render_email_editor(request, db, key)


@router.get("/admin/emails/{key}/preview", dependencies=[Depends(require_admin)])
async def admin_email_preview(key: str, db: AsyncSession = Depends(get_db_session)) -> Response:
    definition = _email_definition(key)
    html = await preview_notification(db, definition.key, _email_sample_context(definition.key))
    return Response(
        html,
        media_type="text/html",
        headers={"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src 'self' https:"},
    )


@router.post("/admin/emails/{key}/draft", dependencies=[Depends(require_admin)])
async def admin_email_save_draft(
    key: str,
    request: Request,
    subject: str = Form(...),
    html: str = Form(...),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    safe_key = _email_definition(key).key
    try:
        await save_draft(db, safe_key, subject, html, int(user["sub"]))
    except ValueError as exc:
        return await _render_email_editor(
            request, db, safe_key, subject=subject, html=html, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST
        )
    return safe_redirect(url=f"/admin/emails/{safe_key}/?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/emails/{key}/publish", dependencies=[Depends(require_admin)])
async def admin_email_publish(
    key: str,
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    safe_key = _email_definition(key).key
    try:
        await publish_draft(db, safe_key, int(user["sub"]))
    except ValueError as exc:
        return await _render_email_editor(request, db, safe_key, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    return safe_redirect(url=f"/admin/emails/{safe_key}/?published=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/emails/{key}/test", dependencies=[Depends(require_admin)])
async def admin_email_test(
    key: str,
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    safe_key = _email_definition(key).key
    await queue_notification(
        db,
        safe_key,
        str(user["email"]),
        _email_sample_context(safe_key),
        idempotency_key=f"email-test:{safe_key}:{user['sub']}:{utc_now().isoformat()}",
    )
    return safe_redirect(url=f"/admin/emails/{safe_key}/?test=1", status_code=status.HTTP_303_SEE_OTHER)

def _slugify(text: str) -> str:
    """Derive a URL-safe event slug from free text."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-")


async def _room_has_booth_language(session: AsyncSession, room_id: int, language_code: str) -> bool:
    existing = await session.scalar(
        select(DBBooth.id).where(DBBooth.room_id == room_id, DBBooth.language_code == language_code)
    )
    return existing is not None


async def _create_booth_unless_duplicate(
    *, event_id: int, room_id: int, language_code: str, language_name: str
) -> bool:
    """Create a booth for a room unless a booth with that language already exists."""
    try:
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            if room is None or room.event_id != event_id:
                raise ValueError(f"Room {room_id} does not belong to event {event_id}.")
            if await _room_has_booth_language(session, room_id, language_code):
                return False
            await create_booth(
                session,
                event_id=event_id,
                room_id=room_id,
                language_code=language_code,
                language_name=language_name,
            )
    except IntegrityError:
        async with get_session() as session:
            if await _room_has_booth_language(session, room_id, language_code):
                return False
        raise
    return True


async def _management_event_ids(request: Request, user_id: int | None) -> set[int] | None:
    """Return events appropriate to the active management namespace."""
    is_admin, allowed_event_ids = await get_accessible_event_ids(request, user_id=user_id)
    if not getattr(request.state, "is_workspace", False) or is_admin or user_id is None:
        return allowed_event_ids

    async with get_session() as session:
        memberships = await list_memberships_for_user(session, user_id)
    return {membership.event_id for membership in memberships if membership.role == EVENT_OWNER}


@router.get("/mission-control/")
async def mission_control_list(request: Request, user=Depends(require_user), page: int = 1):
    is_admin, allowed_event_ids = await get_accessible_event_ids(request, user_id=int(user["sub"]))
    async with get_session() as session:
        limit = 20
        offset = (page - 1) * limit
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        accessible_events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)
    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(
        request=request,
        name="mission_control/event_list.html",
        context={
            "events": accessible_events,
            "is_admin": is_admin,
            "is_event_owner": True,
            "is_room_coordinator": True,
            "active_nav": "mission-control",
            "page": page,
            "total_pages": total_pages,
        },
    )


@router.get("/mission-control/{event_slug}/")
async def mission_control_grid(request: Request, event_slug: str, user=Depends(require_user)):
    is_admin, _ = await get_accessible_event_ids(request, user_id=int(user["sub"]))
    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        allowed_room_ids = None
        principal = await resolve_request_principal(request, session)
        if not is_admin and event.id not in principal["owned_event_ids"]:
            # Room coordinators see the rooms they coordinate (all of them when given for the whole event).
            rooms = await list_rooms_for_event(session, event.id)
            allowed_room_ids = {r.id for r in rooms if coordinates_room(principal, r.id, event.id)}
            if not allowed_room_ids:
                raise HTTPException(status_code=403, detail="Access denied. Event owner or Room coordinator required.")
        db_booths = await list_booths_for_event(session, event.id)
        event_booths = []
        for db_b in db_booths:
            if allowed_room_ids is not None and db_b.room_id not in allowed_room_ids:
                continue
            booth_id = make_booth_id(event.slug, db_b.room_id, db_b.language_code)
            in_mem = booths._booths.get(booth_id)
            if in_mem is not None:
                state = in_mem.as_public_dict()
            else:
                mtx_path = db_b.mediamtx_path
                state = {
                    "booth_id": booth_id,
                    "event_slug": event.slug,
                    "language_code": db_b.language_code,
                    "language": db_b.language_name,
                    "instance": "primary",
                    "mediamtx_path": mtx_path,
                    "room_id": db_b.room_id,
                    "channel_id": mtx_path,
                    "active_interpreter_id": None,
                    "handoff_state": "idle",
                    "handoff_initiator_id": None,
                    # Broadcast on/off is live, in-memory session state owned by
                    # Mission Control. A booth with no active session is off.
                    "broadcast_unlocked": False,
                    "ingest_status": "disconnected",
                    "participants": [],
                    "chat_messages": [],
                }
            state["room_name"] = db_b.room.display_name if db_b.room else "Unknown Room"
            event_booths.append(state)
        # One section per room the person may control, even before it has booths, with what
        # the floor-caption control needs. Program ingest starts captions on its own.
        mc_rooms = [
            {
                "id": room.id,
                "name": room.display_name,
                "floor_captions": bool(room.floor_transcription_enabled) and room.floor_transcription_provider != "none",
                "floor_source": room.floor_source or "jitsi_bot",
            }
            for room in await list_rooms_for_event(session, event.id)
            if allowed_room_ids is None or room.id in allowed_room_ids
        ]
    return templates.TemplateResponse(
        request=request,
        name="mission_control/grid.html",
        context={
            "event": event,
            "booths": event_booths,
            "mc_config": {"eventSlug": event.slug, "whipBase": settings.mediamtx_whip_base, "rooms": mc_rooms, "booths": event_booths},
            "whip_base": settings.mediamtx_whip_base,
            "js_version": _JS_CACHE_BUST,
            "active_nav": "mission-control",
            "is_admin": is_admin,
            "is_event_owner": True,
            "is_room_coordinator": True,
        },
    )


@router.get("/admin/login")
async def admin_login_page():
    """Retired admin sign-in page; everyone signs in through ``/login``."""
    return safe_redirect(url="/login?next=/admin/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/logout")
async def admin_logout():
    response = safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("admin_token")
    response.delete_cookie("user_token")
    response.delete_cookie("session_token")
    return response


@router.get("/workspace")
@router.get("/workspace/")
async def my_events_page(request: Request):
    """"My events": every event the signed-in person has a role in (#766)."""
    principal = request.state.principal
    user = principal["user"]
    if user is None:
        return safe_redirect(url="/login?next=/workspace/", status_code=status.HTTP_303_SEE_OTHER)
    request.state.is_workspace = True
    async with get_session() as session:
        my_events = await list_my_events(session, user.id)
    return templates.TemplateResponse(
        request=request,
        name="admin/my_events.html",
        context={
            "my_events": my_events,
            "can_create_events": principal["can_host"] or principal["is_admin"],
        },
    )


@router.get("/admin/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_dashboard(request: Request, page: int = 1):
    admin_flags = await get_access_flags(request)
    user = await get_current_user(request)
    user_id = int(user["sub"]) if user and user.get("sub") else None
    allowed_event_ids = await _management_event_ids(request, user_id)
    limit = 20
    offset = (page - 1) * limit
    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)
        if not admin_flags.get("is_admin") and user:
            if len(events) == 1 and total_events == 1:
                return safe_redirect(
                    url=management_url(request, f"events/{events[0].id}/"),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        event_ids = [ev.id for ev in events]
        booths_by_event = await list_all_booths_for_events(session, event_ids)
        event_data = []
        for ev in events:
            db_booths = booths_by_event.get(ev.id, [])
            booth_statuses = []
            for b in db_booths:
                booth_id = make_booth_id(ev.slug, b.room_id, b.language_code)
                mem_booth = booths.get_booth_sync(booth_id)
                is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
                booth_statuses.append({"db": b, "booth_id": booth_id, "is_live": is_live})
            event_data.append(
                {
                    "event": ev,
                    "booths": booth_statuses,
                    "live_count": sum((1 for bs in booth_statuses if bs["is_live"])),
                    "total_booths": len(booth_statuses),
                }
            )
    mediamtx_ok = await _check_mediamtx()
    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "event_data": event_data,
            "mediamtx_ok": mediamtx_ok,
            "page": page,
            "total_pages": total_pages,
            **admin_flags,
        },
    )


@router.get("/admin/events/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_event_list(request: Request, page: int = 1, search: str | None = None):
    requested_page = page
    search = (search or "").strip() or None
    admin_flags = await get_access_flags(request)
    user = await get_current_user(request)
    user_id = int(user["sub"]) if user and user.get("sub") else None
    allowed_event_ids = await _management_event_ids(request, user_id)
    limit = 20
    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids, search=search)
        total_pages = max(1, math.ceil(total_events / limit))
        page = min(max(page, 1), total_pages)
        offset = (page - 1) * limit
        events = await list_events(
            session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids, search=search
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/event_list.html",
        context={
            "events": events,
            "page": page,
            "requested_page": requested_page,
            "total_pages": total_pages,
            "total_events": total_events,
            "search": search,
            **admin_flags,
        },
    )


@router.post("/admin/events/", dependencies=[Depends(require_hosting_access)])
async def admin_create_event(request: Request, principal: dict = Depends(require_hosting_access)):
    form = await request.form()
    slug = form.get("slug", "").strip()
    display_name = form.get("display_name", "").strip()
    if not slug or not display_name:
        return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        slug = validate_event_slug(slug)
    except ValueError:
        return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        async with get_session() as session:
            # Read-only slug pre-check before consuming rate limit
            if await get_event_by_slug(session, slug) is not None:
                return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)

            client_ip = get_client_ip(request)
            if not check_rate_limit("event_create", client_ip, max_requests=10, window_seconds=60):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many events created. Please try again later.",
                )

            await create_event_with_owner(
                session,
                slug=slug,
                display_name=display_name,
                owner_user_id=principal["user_id"],
            )
    except HTTPException:
        raise
    except Exception:
        return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)
    return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Guided setup wizard: Event → Rooms → Booths → Invite
# ---------------------------------------------------------------------------


@router.get("/admin/setup", dependencies=[Depends(require_hosting_access)])
async def admin_setup_start(request: Request):
    admin_flags = await get_access_flags(request)
    if not getattr(request.state, "is_workspace", False) and not admin_flags.get("is_admin"):
        return safe_redirect("/workspace/setup")
    return templates.TemplateResponse(request=request, name="admin/wizard_event.html", context={**admin_flags})


@router.post("/admin/setup", dependencies=[Depends(require_hosting_access)])
async def admin_setup_create_event(request: Request, principal: dict = Depends(require_hosting_access)):
    form = await request.form()
    display_name = form.get("display_name", "").strip()
    raw_slug = form.get("slug", "").strip()
    if not display_name:
        return templates.TemplateResponse(
            request=request,
            name="admin/wizard_event.html",
            context={"error": "Please enter an event name.", "slug": raw_slug},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    candidate = raw_slug or _slugify(display_name)
    try:
        slug = validate_event_slug(candidate)
    except ValueError:
        return templates.TemplateResponse(
            request=request,
            name="admin/wizard_event.html",
            context={
                "error": "That URL slug isn't valid. Use lowercase letters, numbers and hyphens.",
                "display_name": display_name,
                "slug": candidate,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    async with get_session() as session:
        # Read-only slug pre-check before consuming rate limit
        if await get_event_by_slug(session, slug) is not None:
            return templates.TemplateResponse(
                request=request,
                name="admin/wizard_event.html",
                context={
                    "error": f"An event with the slug '{slug}' already exists. Choose another.",
                    "display_name": display_name,
                    "slug": slug,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        client_ip = get_client_ip(request)
        if not check_rate_limit("event_create", client_ip, max_requests=10, window_seconds=60):
            return templates.TemplateResponse(
                request=request,
                name="admin/wizard_event.html",
                context={
                    "error": "Rate limit exceeded. Please wait a moment before creating another event.",
                    "display_name": display_name,
                    "slug": slug,
                },
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        event = await create_event_with_owner(
            session,
            slug=slug,
            display_name=display_name,
            owner_user_id=principal["user_id"],
        )
        event_id = event.id
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/setup/rooms"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _live_booth_codes(event_slug: str, room_id: int, codes: Iterable[str]) -> frozenset[str]:
    live = set()
    for code in codes:
        mem_booth = booths.get_booth_sync(make_booth_id(event_slug, room_id, code))
        if mem_booth is not None and mem_booth.ingest_status == "connected":
            live.add(code)
    return frozenset(live)


async def _room_summaries(session: AsyncSession, event: Event, rooms: Sequence[Room]) -> dict[int, RoomSummary]:
    catalog = engine_catalog(event)
    return {room.id: summarize(room, await load_snapshot(session, room, catalog)) for room in rooms}


@router.get("/admin/events/{event_id}/setup/rooms", dependencies=[Depends(require_event_owner)])
async def admin_setup_rooms(request: Request, event_id: int):
    admin_flags = await get_access_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        rooms = await list_rooms_for_event(session, event_id)
        summaries = await _room_summaries(session, event, rooms)
    return templates.TemplateResponse(
        request=request,
        name="admin/wizard_rooms.html",
        context={"event": event, "rooms": rooms, "summaries": summaries, **admin_flags},
    )


@router.post("/admin/events/{event_id}/setup/rooms", dependencies=[Depends(require_event_owner)])
async def admin_setup_add_room(request: Request, event_id: int):
    # Compatibility for forms opened before room setup was consolidated.
    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if display_name:
        async with get_session() as session:
            ev = await get_event_by_id(session, event_id)
            jitsi_url = None
            if ev:
                clean_name = re.sub("[^a-zA-Z0-9]+", "", display_name)
                room_id_str = f"Voxbento-{ev.slug}-{clean_name}"
                jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)
            await create_room(session, event_id=event_id, display_name=display_name, jitsi_url=jitsi_url)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/setup/rooms"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/setup/booths", dependencies=[Depends(require_event_owner)])
async def admin_setup_add_booth(request: Request, event_id: int):
    """Add a booth to a room during event setup."""
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
    form = await request.form()
    room_id_str = form.get("room_id", "").strip()
    language_code = form.get("language_code", "").strip().lower()
    language_name = form.get("language_name", "").strip()
    error = None
    room = None
    room_id = None
    if not room_id_str:
        error = "room_required"
    else:
        try:
            room_id = int(room_id_str)
        except ValueError:
            room_id = None
        # Keep the id inside a 32-bit SQL integer before it reaches the database.
        if room_id is not None and 0 < room_id <= 2**31 - 1:
            async with get_session() as session:
                room = await get_room_by_id(session, room_id)
        else:
            room_id = None
        if room is None or room.event_id != event_id:
            # A malformed or foreign room must be reported, not silently ignored.
            room_id_str = ""
            error = "room_not_found"
    if error is None and not language_code:
        error = "language_required"
    if error is None:
        if not language_name:
            lang = pycountry.languages.get(alpha_2=language_code)
            language_name = lang.name if lang else language_code.upper()
        try:
            created = await _create_booth_unless_duplicate(
                event_id=event_id,
                room_id=room.id,
                language_code=language_code,
                language_name=language_name,
            )
            if not created:
                error = "duplicate_language"
        except (ValueError, SQLAlchemyError) as e:
            logger.warning("Error creating booth in setup wizard: %s", e)
            error = "booth_create_failed"

    query = {}
    if room_id_str:
        query["room_id"] = room_id_str
    if error:
        query["error"] = error
    redirect_url = management_url(request, f"events/{event_id}/setup/rooms")
    if query:
        redirect_url += f"?{urllib.parse.urlencode(query)}"

    return safe_redirect(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/setup/booths", dependencies=[Depends(require_event_owner)])
async def admin_setup_booths(request: Request, event_id: int):
    """Booths are now chosen inside each room's setup; old links land on the Rooms step."""
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/setup/rooms"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _room_setup_response(
    request: Request,
    *,
    event: Event,
    room: Room | None,
    plan: RoomPlan,
    catalog: EngineCatalog,
    admin_flags: dict,
    errors: Sequence[str] = (),
    booth_codes: Sequence[str] = (),
    live_codes: frozenset[str] = frozenset(),
    from_setup: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    chosen = [code for code, _ in plan.languages]
    chip_codes = list(dict.fromkeys([code for code, _ in ROOM_COMMON_LANGUAGES] + chosen))
    return templates.TemplateResponse(
        request=request,
        name="admin/room_setup.html",
        context={
            "event": event,
            "room": room,
            "plan": plan,
            "catalog": catalog,
            "errors": errors,
            "chip_languages": [(code, room_language_name(code)) for code in chip_codes],
            "all_languages": ROOM_ALL_LANGUAGES,
            "common_languages": ROOM_COMMON_LANGUAGES,
            "common_codes": [code for code, _ in ROOM_COMMON_LANGUAGES],
            "language_name": room_language_name,
            "delivery_labels": ROOM_DELIVERY_LABELS,
            "delivery_hints": ROOM_DELIVERY_HINTS,
            "delivery_explanations": ROOM_DELIVERY_EXPLANATIONS,
            "booth_codes": list(booth_codes),
            "live_codes": sorted(live_codes),
            "from_setup": from_setup,
            **admin_flags,
        },
        status_code=status_code,
    )


@router.get("/admin/events/{event_id}/rooms/new", dependencies=[Depends(require_event_owner)])
async def admin_room_setup_new(request: Request, event_id: int, source: str | None = Query(None, alias="from")):
    admin_flags = await get_access_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
    catalog = engine_catalog(event)
    return _room_setup_response(
        request,
        event=event,
        room=None,
        plan=blank_plan(catalog),
        catalog=catalog,
        admin_flags=admin_flags,
        from_setup=source == "setup",
    )


@router.post("/admin/events/{event_id}/rooms/new", dependencies=[Depends(require_event_owner)])
async def admin_room_setup_create(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    form = await request.form()
    from_setup = form.get("from") == "setup"
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        catalog = engine_catalog(event)
        plan, errors = parse_plan(form, catalog)
        if errors:
            admin_flags = await get_access_flags(request, event_id=event_id)
            return _room_setup_response(
                request,
                event=event,
                room=None,
                plan=plan,
                catalog=catalog,
                admin_flags=admin_flags,
                errors=errors,
                from_setup=from_setup,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        room = await create_room(
            session,
            event_id=event_id,
            display_name=plan.display_name,
            jitsi_url=default_jitsi_url(event.slug, plan.display_name),
        )
        await apply_plan(session, event, room, plan, catalog)
        room_id = room.id
    target = f"events/{event_id}/setup/rooms" if from_setup else f"events/{event_id}/rooms/{room_id}/?created=1"
    return safe_redirect(url=management_url(request, target), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/{room_id}/setup", dependencies=[Depends(require_event_owner)])
async def admin_room_setup_edit(request: Request, event_id: int, room_id: int):
    source = request.query_params.get("from")
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await room_in_event(session, event_id, room_id)
        catalog = engine_catalog(event)
        snapshot = await load_snapshot(session, room, catalog)
    live = _live_booth_codes(event.slug, room_id, snapshot.booth_codes)
    return _room_setup_response(
        request,
        event=event,
        room=room,
        plan=snapshot.plan,
        catalog=catalog,
        admin_flags=admin_flags,
        booth_codes=snapshot.booth_codes,
        live_codes=live,
        from_setup=source == "setup",
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/setup", dependencies=[Depends(require_event_owner)])
async def admin_room_setup_save(request: Request, event_id: int, room_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    form = await request.form()
    from_setup = form.get("from") == "setup"
    async with room_lock(room_id), get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await room_in_event(session, event_id, room_id)
        catalog = engine_catalog(event)
        booth_codes = [b.language_code for b in await list_booths_for_room(session, room_id)]
        plan, errors = parse_plan(form, catalog, existing_booth_codes=booth_codes)
        live = _live_booth_codes(event.slug, room_id, booth_codes)
        if errors:
            admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
            return _room_setup_response(
                request,
                event=event,
                room=room,
                plan=plan,
                catalog=catalog,
                admin_flags=admin_flags,
                errors=errors,
                booth_codes=booth_codes,
                live_codes=live,
                from_setup=from_setup,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        result = await apply_plan(session, event, room, plan, catalog, live_booth_codes=live)
    # The session has committed: drop the old voice settings so AI text languages stop speaking now.
    invalidate_tts_config(room_id)
    query = {"updated": "1"}
    if result.kept_live_booths:
        query["kept_live"] = ",".join(result.kept_live_booths)
    target = f"events/{event_id}/setup/rooms" if from_setup else f"events/{event_id}/rooms/{room_id}/?{urllib.parse.urlencode(query)}"
    return safe_redirect(
        url=management_url(request, target),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/events/{event_id}/setup/invite", dependencies=[Depends(require_event_owner)])
async def admin_setup_invite(request: Request, event_id: int, success: str | None = None, error: str | None = None):
    admin_flags = await get_access_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        if not event.listener_join_code:
            event.listener_join_code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            await session.flush()
        memberships = await list_memberships_for_event(session, event_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/wizard_invite.html",
        context={
            "event": event,
            "memberships": memberships,
            "success": success,
            "error": error,
            "public_base_url": settings.public_base_url,
            **admin_flags,
        },
    )


@router.post("/admin/events/{event_id}/setup/invite", dependencies=[Depends(require_event_owner)])
async def admin_setup_add_invite(request: Request, event_id: int):
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", EVENT_OWNER).strip() or EVENT_OWNER
    if role and assignment_error(role, "event"):
        return invalid_role_redirect(request, f"events/{event_id}/setup/invite", role)
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                user = await create_user(session, email=email, display_name=email.split("@")[0], email_verified=False)
            try:
                await set_event_membership(session, user_id=user.id, event_id=event_id, role=role)
            except ValueError:
                return safe_redirect(
                    url=management_url(request, f"events/{event_id}/setup/invite?error=invalid_role&role={urllib.parse.quote(role)}"),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            event = await get_event_by_id(session, event_id)
            if event:
                try:
                    await _send_admin_invite(session, user, role, f"event {event.display_name}", "/account")
                except Exception as e:
                    logger.warning(f"Failed to send setup invite email to {email}: {e}")
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/setup/invite?success=invite_sent"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/regenerate_join_code/", dependencies=[Depends(require_event_owner)])
async def admin_regenerate_join_code(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        event.listener_join_code = "".join((secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)))
        await session.flush()
    return safe_redirect(url=management_url(request, f"events/{event_id}/"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_event_detail(request: Request, event_id: int):
    admin_flags = await get_access_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        rooms = await list_rooms_for_event(session, event_id)
        db_booths = await list_booths_for_event(session, event_id)
        summaries = await _room_summaries(session, event, rooms)
    # Keep each room's booths together, in the same order as the Rooms list.
    room_order = {room.id: index for index, room in enumerate(rooms)}
    db_booths.sort(key=lambda b: (room_order.get(b.room_id, len(room_order)), b.language_code))
    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.room_id, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    return templates.TemplateResponse(
        request=request,
        name="admin/event_detail.html",
        context={
            "event": event,
            "rooms": rooms,
            "summaries": summaries,
            "booths": booth_statuses,
            "live_count": sum((1 for bs in booth_statuses if bs["is_live"])),
            **admin_flags,
        },
    )


@router.get("/admin/events/{event_id}/api-settings/", dependencies=[Depends(require_event_owner)])
async def admin_event_api_settings_get(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
    admin_flags = await get_access_flags(request, event_id=event_id)
    return templates.TemplateResponse(
        request=request, name="admin/api_settings.html", context={"event": event, **admin_flags}
    )


@router.post("/admin/events/{event_id}/api-settings", dependencies=[Depends(require_event_owner)])
async def admin_event_api_settings_post(
    request: Request,
    event_id: int,
    transcription_api_enabled: bool | None = Form(False),
    openai_api_key: str | None = Form(None),
    deepgram_api_key: str | None = Form(None),
    nvidia_api_key: str | None = Form(None),
    elevenlabs_api_key: str | None = Form(None),
    clear_openai_api_key: bool | None = Form(False),
    clear_deepgram_api_key: bool | None = Form(False),
    clear_nvidia_api_key: bool | None = Form(False),
    clear_elevenlabs_api_key: bool | None = Form(False),
    translation_openai_api_key: str | None = Form(None),
    openrouter_api_key: str | None = Form(None),
    gemini_api_key: str | None = Form(None),
    anthropic_api_key: str | None = Form(None),
    groq_api_key: str | None = Form(None),
    clear_translation_openai_api_key: bool | None = Form(False),
    clear_openrouter_api_key: bool | None = Form(False),
    clear_gemini_api_key: bool | None = Form(False),
    clear_anthropic_api_key: bool | None = Form(False),
    clear_groq_api_key: bool | None = Form(False),
):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        event.transcription_api_enabled = bool(transcription_api_enabled)
        try:
            _apply_api_key_updates(
                event,
                (
                    ("encrypted_openai_api_key", openai_api_key, clear_openai_api_key),
                    ("encrypted_deepgram_api_key", deepgram_api_key, clear_deepgram_api_key),
                    ("encrypted_nvidia_api_key", nvidia_api_key, clear_nvidia_api_key),
                    ("encrypted_elevenlabs_api_key", elevenlabs_api_key, clear_elevenlabs_api_key),
                    (
                        "encrypted_translation_openai_api_key",
                        translation_openai_api_key,
                        clear_translation_openai_api_key,
                    ),
                    ("encrypted_openrouter_api_key", openrouter_api_key, clear_openrouter_api_key),
                    ("encrypted_gemini_api_key", gemini_api_key, clear_gemini_api_key),
                    ("encrypted_anthropic_api_key", anthropic_api_key, clear_anthropic_api_key),
                    ("encrypted_groq_api_key", groq_api_key, clear_groq_api_key),
                ),
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=f"API Key encryption failed: {e}")
        await session.flush()
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/api-settings/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/delete", dependencies=[Depends(require_event_owner)])
async def admin_delete_event(request: Request, event_id: int):
    """Delete an event by ID, returning 404 if missing or redirecting with an error alert on database failure."""
    try:
        async with get_session() as session:
            await revoke_booths_for_deleted_entity(session, level="event", target_id=event_id)
            deleted = await delete_event(session, event_id)
            if not deleted:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        logger.error(f"Failed to delete event {event_id}: {e}")
        return safe_redirect(
            url=management_url(request, "events/?error=delete_failed"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return safe_redirect(url=management_url(request, "events/"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_room_list(request: Request, event_id: int, search: str | None = Query(None)):
    search_q = (search or "").strip()
    search_val = search_q if search_q else None
    admin_flags = await get_access_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        rooms = await list_rooms_for_event(session, event_id, search=search_val)
        room_data = [{"room": room} for room in rooms]
        summaries = await _room_summaries(session, event, rooms)
    return templates.TemplateResponse(
        request=request,
        name="admin/room_list.html",
        context={
            "event": event,
            "room_data": room_data,
            "summaries": summaries,
            "search": search_val or "",
            **admin_flags,
        },
    )


@router.post("/admin/events/{event_id}/rooms/", dependencies=[Depends(require_event_owner)])
async def admin_create_room(request: Request, event_id: int):
    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if not display_name:
        return safe_redirect(
            url=management_url(request, f"events/{event_id}/rooms/"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with get_session() as session:
        ev = await get_event_by_id(session, event_id)
        jitsi_url = default_jitsi_url(ev.slug, display_name) if ev else None
        await create_room(session, event_id=event_id, display_name=display_name, jitsi_url=jitsi_url)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/"), status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/events/by-slug/{event_slug}/rooms/{room_id}/")
@router.get("/admin/events/by-slug/{event_slug}/rooms/{room_id}")
@router.get("/workspace/events/by-slug/{event_slug}/rooms/{room_id}/")
@router.get("/workspace/events/by-slug/{event_slug}/rooms/{room_id}")
async def admin_redirect_room_by_slug(request: Request, event_slug: str, room_id: str, id_type: str | None = None):
    """Redirect by-slug room URL to canonical workspace room page."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import Event, Room

    lookup_type = id_type or request.query_params.get("by") or request.query_params.get("type")

    async with get_session() as session:
        event = (await session.execute(select(Event).where(Event.slug == event_slug))).scalars().first()
        if not event:
            event = (await session.execute(select(Event).where(Event.display_name == event_slug))).scalars().first()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        room = None
        if lookup_type in ("eventyay", "external"):
            room = (
                (
                    await session.execute(
                        select(Room).where(Room.eventyay_room_id == str(room_id), Room.event_id == event.id)
                    )
                )
                .scalars()
                .first()
            )
        elif lookup_type in ("id", "internal", "pk"):
            if room_id.isdigit():
                room = (
                    (await session.execute(select(Room).where(Room.id == int(room_id), Room.event_id == event.id)))
                    .scalars()
                    .first()
                )
        else:
            # Disambiguate when no explicit lookup type is provided
            if not room_id.isdigit():
                room = (
                    (
                        await session.execute(
                            select(Room).where(Room.eventyay_room_id == str(room_id), Room.event_id == event.id)
                        )
                    )
                    .scalars()
                    .first()
                )
            else:
                internal_room = (
                    (await session.execute(select(Room).where(Room.id == int(room_id), Room.event_id == event.id)))
                    .scalars()
                    .first()
                )
                ext_room = (
                    (
                        await session.execute(
                            select(Room).where(Room.eventyay_room_id == str(room_id), Room.event_id == event.id)
                        )
                    )
                    .scalars()
                    .first()
                )
                room = internal_room or ext_room

        target_room_id = room.id if room else room_id
        return safe_redirect(
            url=f"/workspace/events/{event.id}/rooms/{target_room_id}/",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@router.get("/admin/events/by-slug/{event_slug}/rooms/by-eventyay/{eventyay_room_id}/")
@router.get("/admin/events/by-slug/{event_slug}/rooms/by-eventyay/{eventyay_room_id}")
@router.get("/workspace/events/by-slug/{event_slug}/rooms/by-eventyay/{eventyay_room_id}/")
@router.get("/workspace/events/by-slug/{event_slug}/rooms/by-eventyay/{eventyay_room_id}")
async def admin_redirect_room_by_eventyay_id(request: Request, event_slug: str, eventyay_room_id: str):
    """Redirect unambiguous eventyay external room ID to canonical workspace room page."""
    return await admin_redirect_room_by_slug(request, event_slug, eventyay_room_id, id_type="eventyay")


@router.get("/admin/events/{event_id}/rooms/{room_id}/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_room_detail(request: Request, event_id: int, room_id: int):
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
        db_booths = await list_booths_for_room(session, room_id)
        catalog = engine_catalog(event)
        snapshot = await load_snapshot(session, room, catalog)
        vocabulary_entries = list(
            await session.scalars(
                select(AIVocabularyEntry)
                .where(AIVocabularyEntry.event_id == event_id, AIVocabularyEntry.room_id == room_id)
                .order_by(AIVocabularyEntry.priority.desc(), AIVocabularyEntry.source_term)
            )
        )
    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.room_id, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    clean_name = re.sub("[^a-zA-Z0-9]+", "", room.display_name)
    room_id_str = f"Voxbento-{event.slug}-{clean_name}"
    fallback_jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)
    async with get_session() as session:
        memberships = await list_memberships_for_room(session, room_id)
    kept_live = [
        room_language_name(code) for code in request.query_params.get("kept_live", "").split(",") if code.strip()
    ]
    return templates.TemplateResponse(
        request=request,
        name="admin/room_detail.html",
        context={
            "event": event,
            "room": room,
            "booths": booth_statuses,
            "summary": summarize(room, snapshot),
            "booths_by_code": {bs["db"].language_code: bs for bs in booth_statuses},
            "just_created": request.query_params.get("created") == "1",
            "just_updated": request.query_params.get("updated") == "1",
            "kept_live_languages": kept_live,
            "fallback_jitsi_url": fallback_jitsi_url,
            "catalog": catalog,
            "audio_choice": room_audio_choice(room),
            "speech_models": ROOM_SPEECH_MODELS,
            "voice_presets": ROOM_VOICE_PRESETS,
            "ai_language_names": [room_language_name(code) for code in snapshot.plan.ai_languages],
            "voice_language_names": [
                room_language_name(code) for code, delivery in snapshot.plan.languages if delivery == AI_VOICE
            ],
            "vocabulary_entries": vocabulary_entries,
            "vocabulary_imported": request.query_params.get("vocab_imported"),
            "vocabulary_warnings": request.query_params.get("vocab_warnings"),
            "vocabulary_languages": request.query_params.get("vocab_languages"),
            "memberships": memberships,
            **admin_flags,
        },
    )


@router.get("/admin/events/{event_id}/rooms/{room_id}/sender/", dependencies=[Depends(require_event_owner)])
async def admin_room_sender(request: Request, event_id: int, room_id: int):
    """Standalone program audio sender, meant to stay open on the sound-desk machine."""
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
    return templates.TemplateResponse(
        request=request,
        name="admin/program_sender.html",
        context={"event": event, "room": room},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/events/{event_id}/rooms/{room_id}/transcripts/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_room_transcripts(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
        booths = await list_booths_for_room(session, room_id)
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/room_transcripts.html",
        context={"event": event, "room": room, "booths": booths, **admin_flags},
    )


def _apply_ai_settings(room: Room, form: FormData, catalog: EngineCatalog) -> None:
    """Save the room page's AI settings: services, models, voice and translator instructions.

    What a room offers (captions on or off, which languages use AI) belongs to the room setup
    form, so this never switches anything on or off. A service the event can't use, or one
    left out of the form, keeps the room's current choice.
    """
    speech = catalog.find("speech", str(form.get("floor_transcription_provider", "")))
    if speech:
        model = str(form.get("floor_transcription_model", ""))
        allowed = {value for value, _ in ROOM_SPEECH_MODELS.get(speech.provider, ())}
        room.floor_transcription_provider = speech.provider
        room.floor_transcription_model = model if model in allowed else speech.model

    translation = catalog.find("translation", str(form.get("floor_translation_provider", "")))
    if translation:
        model = str(form.get("floor_translation_model", "")).strip()[:100]
        room.floor_translation_provider = translation.provider
        room.floor_translation_model = model or translation.model
    if "floor_ai_interpreter_persona" in form:
        room.floor_ai_interpreter_persona = str(form.get("floor_ai_interpreter_persona", "")).strip() or None
        room.floor_ai_interpretation_style = str(form.get("floor_ai_interpretation_style", "")).strip() or None
        room.floor_ai_vocabulary_enabled = form.get("floor_ai_vocabulary_enabled") == "on"

    voice = catalog.find("voice", str(form.get("floor_tts_provider", "")))
    if voice:
        room.floor_tts_provider = voice.provider
        preset = str(form.get("floor_tts_voice", "")).strip().upper()
        if preset in {value for value, _ in ROOM_VOICE_PRESETS}:
            room.floor_tts_voice = preset


@router.post("/admin/events/{event_id}/rooms/{room_id}/edit", dependencies=[Depends(require_event_owner)])
async def admin_edit_room(request: Request, event_id: int, room_id: int):
    """Update settings and configuration for an event room."""
    form = await request.form()
    form_section = form.get("form_section", "").strip()
    display_name = form.get("display_name", "").strip()
    jitsi_url = form.get("jitsi_url", "").strip()
    relay_booth_id_str = form.get("relay_booth_id", "").strip()
    relay_booth_id = int(relay_booth_id_str) if relay_booth_id_str and relay_booth_id_str.lower() != "none" else None
    audio_delay_ms = parse_audio_delay_ms(form.get("audio_delay_ms", "0"))
    floor_transcription_enabled = form.get("floor_transcription_enabled") == "on"
    floor_transcription_provider = form.get("floor_transcription_provider", "local").strip()
    floor_transcription_model = form.get("floor_transcription_model", "tiny").strip()
    floor_language_code = form.get("floor_language_code", "").strip() or None
    floor_translation_enabled = form.get("floor_translation_enabled") == "on"
    floor_translation_provider = form.get("floor_translation_provider", "").strip() or None
    floor_translation_model = form.get("floor_translation_model", "").strip() or None
    floor_translation_languages = form.getlist("floor_translation_languages")
    floor_ai_interpreter_persona = form.get("floor_ai_interpreter_persona", "").strip() or None
    floor_ai_interpretation_style = form.get("floor_ai_interpretation_style", "").strip() or None
    floor_ai_vocabulary_enabled = form.get("floor_ai_vocabulary_enabled") == "on"
    floor_tts_enabled = form.get("floor_tts_enabled") == "on"
    floor_tts_provider = (form.get("floor_tts_provider", "deepgram") or "deepgram").strip().lower() or "deepgram"
    if floor_tts_provider not in {"deepgram", "supertonic"}:
        floor_tts_provider = "deepgram"
    floor_tts_voice = (form.get("floor_tts_voice", "M1") or "M1").strip().upper() or "M1"
    if floor_tts_voice not in {"M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"}:
        floor_tts_voice = "M1"
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")

        if not form_section or form_section == "general":
            if display_name:
                room.display_name = display_name
            room.jitsi_url = jitsi_url if jitsi_url else None
            room.audio_delay_ms = audio_delay_ms

        if not form_section or form_section == "relay":
            if relay_booth_id is not None:
                relay_booth = await get_booth_by_id(session, relay_booth_id)
                if relay_booth is None or relay_booth.event_id != event_id:
                    raise HTTPException(status_code=404, detail="Booth not found.")
                relay_room = await get_room_by_id(session, relay_booth.room_id)
                if relay_room is None or relay_room.event_id != event_id:
                    raise HTTPException(status_code=404, detail="Booth not found.")
            room.relay_booth_id = relay_booth_id

        if not form_section or form_section == "transcription":
            room.floor_transcription_enabled = floor_transcription_enabled
            room.floor_transcription_provider = floor_transcription_provider
            room.floor_transcription_model = floor_transcription_model
            room.floor_language_code = floor_language_code

        if not form_section or form_section == "translation":
            room.floor_translation_enabled = floor_translation_enabled
            room.floor_translation_provider = floor_translation_provider
            room.floor_translation_model = floor_translation_model
            room.floor_ai_interpreter_persona = floor_ai_interpreter_persona
            room.floor_ai_interpretation_style = floor_ai_interpretation_style
            room.floor_ai_vocabulary_enabled = floor_ai_vocabulary_enabled

            existing_langs = {lang.language_code: lang for lang in room.translation_languages}
            requested_codes = set(floor_translation_languages)
            for code, lang in existing_langs.items():
                if code not in requested_codes:
                    lang.enabled = False
            for code in requested_codes:
                if code in existing_langs:
                    existing_langs[code].enabled = True
                else:
                    lang_obj = pycountry.languages.get(alpha_2=code)
                    lang_name = lang_obj.name if lang_obj else code
                    new_lang = RoomTranslationLanguage(
                        room_id=room_id, language_code=code, language_name=lang_name, enabled=True
                    )
                    session.add(new_lang)

        if not form_section or form_section == "tts":
            room.floor_tts_enabled = floor_tts_enabled
            room.floor_tts_provider = floor_tts_provider
            room.floor_tts_voice = floor_tts_voice

        if form_section == "ai":
            event = await get_event_by_id(session, event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="Event not found.")
            _apply_ai_settings(room, form, engine_catalog(event))
        await session.flush()
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _vocabulary_result_url(event_id: int, room_id: int, imported: int, warnings: list[str], languages: set[str]) -> str:
    query = urllib.parse.urlencode(
        {
            "vocab_imported": imported,
            "vocab_warnings": " | ".join(warnings[:5]),
            "vocab_languages": ", ".join(sorted(languages)),
        }
    )
    return f"/admin/events/{event_id}/rooms/{room_id}/?{query}"


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/upload",
    dependencies=[Depends(require_event_owner)],
)
async def admin_upload_ai_vocabulary(request: Request, event_id: int, room_id: int):
    form = await request.form()
    upload = form.get("vocabulary_file")
    if upload is None or not getattr(upload, "filename", ""):
        raise HTTPException(status_code=400, detail="Select a vocabulary CSV file.")
    if not upload.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Vocabulary uploads must be CSV files.")
    content = await upload.read()
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="Vocabulary CSV files must be 2 MB or smaller.")
    try:
        csv_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Vocabulary CSV must use UTF-8 encoding.") from exc

    parsed_entries, warnings = parse_vocabulary_csv(csv_text)
    replace_existing = form.get("import_mode") == "replace"
    imported = 0
    languages: set[str] = set()
    async with get_session() as session:
        try:
            await validate_vocabulary_scope(session, event_id, room_id=room_id)
        except VocabularyScopeError as exc:
            raise HTTPException(status_code=404, detail="Room not found.") from exc
        existing_entries = list(
            await session.scalars(
                select(AIVocabularyEntry).where(
                    AIVocabularyEntry.event_id == event_id,
                    AIVocabularyEntry.room_id == room_id,
                )
            )
        )
        if replace_existing and parsed_entries:
            await session.execute(
                delete(AIVocabularyEntry).where(
                    AIVocabularyEntry.event_id == event_id,
                    AIVocabularyEntry.room_id == room_id,
                )
            )
            accepted = VocabularyOverlapIndex()
        else:
            accepted = VocabularyOverlapIndex()
            for existing in existing_entries:
                accepted.add(existing)

        for entry in parsed_entries:
            if accepted.conflicts(entry):
                warnings.append(
                    f"Existing duplicate: '{entry.source_term}' for target language '{entry.target_language}'"
                )
                continue
            accepted.add(entry)
            languages.add(entry.target_language)
            session.add(
                AIVocabularyEntry(
                    event_id=event_id,
                    room_id=room_id,
                    source_term=entry.source_term,
                    target_language=entry.target_language,
                    target_term=entry.target_term,
                    description=entry.description,
                    case_sensitive=entry.case_sensitive,
                    match_type=entry.match_type,
                    priority=entry.priority,
                )
            )
            imported += 1
        await session.flush()

    return safe_redirect(
        url=_vocabulary_result_url(event_id, room_id, imported, warnings, languages),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/export",
    dependencies=[Depends(require_owner_or_coordinator)],
)
async def admin_export_ai_vocabulary(event_id: int, room_id: int):
    async with get_session() as session:
        try:
            await validate_vocabulary_scope(session, event_id, room_id=room_id)
        except VocabularyScopeError as exc:
            raise HTTPException(status_code=404, detail="Room not found.") from exc
        entries = list(
            await session.scalars(
                select(AIVocabularyEntry)
                .where(AIVocabularyEntry.event_id == event_id, AIVocabularyEntry.room_id == room_id)
                .order_by(AIVocabularyEntry.priority.desc(), AIVocabularyEntry.source_term)
            )
        )
    return Response(
        serialize_vocabulary_csv(entries),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="room-{room_id}-ai-vocabulary.csv"'},
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/delete",
    dependencies=[Depends(require_event_owner)],
)
async def admin_delete_ai_vocabulary(request: Request, event_id: int, room_id: int):
    form = await request.form()
    entry_id = form.get("entry_id", "").strip()
    async with get_session() as session:
        try:
            await validate_vocabulary_scope(session, event_id, room_id=room_id)
        except VocabularyScopeError as exc:
            raise HTTPException(status_code=404, detail="Room not found.") from exc
        statement = delete(AIVocabularyEntry).where(
            AIVocabularyEntry.event_id == event_id,
            AIVocabularyEntry.room_id == room_id,
        )
        if entry_id != "all":
            try:
                statement = statement.where(AIVocabularyEntry.id == int(entry_id))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid vocabulary entry ID.") from exc
        await session.execute(statement)
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/admin/providers/translation/models", dependencies=[Depends(require_owner_or_coordinator)])
async def get_translation_models():
    return TRANSLATION_MODELS


MAX_AUDIO_DELAY_MS = 10_000


def parse_audio_delay_ms(value: object) -> int:
    try:
        delay_ms = int(str(value or "0").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Audio synchronization delay must be an integer.")
    if delay_ms < 0 or delay_ms > MAX_AUDIO_DELAY_MS:
        raise HTTPException(status_code=400, detail="Audio synchronization delay must be between 0 and 10000 ms.")
    return delay_ms


class APIKeyCreateRequest(BaseModel):
    name: str


@router.get("/admin/api/events/{event_id}/api-keys", dependencies=[Depends(require_event_owner)])
async def admin_list_api_keys(request: Request, event_id: int):
    async with get_session() as session:
        keys = await get_api_keys_for_event(session, event_id)
        return [
            {
                "id": k.id,
                "name": k.name,
                "preview": k.preview,
                "created_at": k.created_at.isoformat(),
                "active": k.active,
            }
            for k in keys
        ]


@router.post("/admin/api/events/{event_id}/api-keys", dependencies=[Depends(require_event_owner)])
async def admin_create_api_key(request: Request, event_id: int, data: APIKeyCreateRequest):
    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="API Key name cannot be blank.")

    async with get_session() as session:
        from sqlalchemy import select

        from portal.models import EventAPIKey

        # Check if active key with same name exists for this event
        stmt = select(EventAPIKey).where(
            EventAPIKey.event_id == event_id, EventAPIKey.name == data.name.strip(), EventAPIKey.active
        )
        existing = await session.execute(stmt)
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=400, detail=f"An active API Key named '{data.name.strip()}' already exists."
            )

        api_key, raw_key = await create_api_key(session, event_id, data.name.strip())
        return {
            "id": api_key.id,
            "name": api_key.name,
            "preview": api_key.preview,
            "created_at": api_key.created_at.isoformat(),
            "raw_key": raw_key,
        }


@router.delete("/admin/api/events/{event_id}/api-keys/{key_id}", dependencies=[Depends(require_event_owner)])
async def admin_delete_api_key(request: Request, event_id: int, key_id: int):
    async with get_session() as session:
        success = await revoke_api_key(session, key_id, event_id)
        if not success:
            raise HTTPException(status_code=404, detail="API Key not found or already revoked")
        return {"success": True}


# Legacy room-scoped API retained for coordinators and scripts. It drives the
# same managed meeting agent as the room page, so the reconcile loop owns retries
# and starts the floor worker once audio arrives.
@router.post("/api/rooms/{room_id}/floor-transcription/start", dependencies=[Depends(require_room_event_access)])
async def api_start_floor_transcription(room_id: int):
    async with room_lock(room_id), get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room or not room.floor_transcription_enabled:
            raise HTTPException(status_code=400, detail="Floor transcription not enabled or invalid room")
        if room.floor_source == "program_ingest":
            raise HTTPException(
                status_code=409,
                detail="Program ingest owns the floor; transcription starts automatically when audio arrives",
            )
        if room.floor_transcription_provider == "none":
            raise HTTPException(status_code=400, detail="Cannot start transcription with 'none' provider")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")
        await meeting_source.connect(room, event, None, confirm_source_change=False)
    return {"status": "started"}


@router.post("/api/rooms/{room_id}/floor-transcription/stop", dependencies=[Depends(require_room_event_access)])
async def api_stop_floor_transcription(room_id: int):
    async with room_lock(room_id), get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room:
            raise HTTPException(status_code=400, detail="Invalid room")
        if room.floor_source == "program_ingest":
            raise HTTPException(
                status_code=409, detail="Revoke or disable program ingest in the room settings to stop its floor source"
            )
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")
        stopped = await meeting_source.disconnect(room, event)
    if not stopped:
        # Mirror the owner route: the agent may still be in the meeting until reconcile retries.
        return JSONResponse({"status": "stopping", "cleanup_pending": True}, status_code=202)
    return {"status": "stopped"}


@router.get("/api/rooms/{room_id}/floor-transcription/status", dependencies=[Depends(require_room_event_access)])
async def api_floor_transcription_status(room_id: int):
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room:
            raise HTTPException(status_code=404, detail="Invalid room")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        body = meeting_source.status(room)
    # Keep the pre-managed fields with their original meaning (raw bot stage such as
    # launching, in_meeting or dead) so existing scripts and monitors keep working;
    # the managed lifecycle is reported separately in "state".
    body["bot_reachable"], body["stage"], body["running"] = await meeting_source.bot_stage(event.slug, room.id)
    return body


@router.post("/admin/events/{event_id}/rooms/{room_id}/delete", dependencies=[Depends(require_event_owner)])
async def admin_delete_room(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
        await revoke_booths_for_deleted_entity(session, level="room", target_id=room_id)
        await delete_room(session, room_id)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/"), status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/events/{event_id}/rooms/{room_id}/booths/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_booth_list(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
        db_booths = await list_booths_for_room(session, room_id)
    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.room_id, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/booth_list.html",
        context={"event": event, "room": room, "booths": booth_statuses, **admin_flags},
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/", dependencies=[Depends(require_event_owner)])
async def admin_create_booth(request: Request, event_id: int, room_id: int):
    form = await request.form()
    language_code = form.get("language_code", "").strip().lower()
    language_name = form.get("language_name", "").strip()
    if not language_code or not language_name:
        return safe_redirect(
            url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    redirect_path = f"events/{event_id}/rooms/{room_id}/booths/"
    async with get_session() as session:
        await room_in_event(session, event_id, room_id)
    try:
        created = await _create_booth_unless_duplicate(
            event_id=event_id,
            room_id=room_id,
            language_code=language_code,
            language_name=language_name,
        )
        if not created:
            redirect_path += "?error=duplicate_language"
    except Exception as e:
        logger.warning(f"Error creating booth: {e}")
    return safe_redirect(
        url=management_url(request, redirect_path),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", dependencies=[Depends(require_owner_or_coordinator)])
async def admin_booth_detail(request: Request, event_id: int, room_id: int, booth_id: int):
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await room_in_event(session, event_id, room_id)
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.event_id != event_id or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail="Booth not found.")
        tokens = await list_tokens_for_booth(session, booth_id)
        users = await list_users(session)
        memberships = await list_memberships_for_booth(session, booth_id)
    membership_map = {m.user_id: m for m in memberships}
    bid = make_booth_id(event.slug, db_booth.room_id, db_booth.language_code)
    mediamtx_path = make_mediamtx_path(event.slug, db_booth.room_id, db_booth.language_code)
    whep_url = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whep"
    mem_booth = booths.get_booth_sync(bid)
    is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
    participants = list(mem_booth.participants.values()) if mem_booth else []
    active_interpreter = None
    if mem_booth and mem_booth.active_interpreter_id:
        active_interpreter = mem_booth.participants.get(mem_booth.active_interpreter_id)
    translation_languages_dataset = [
        {"code": lang.alpha_2, "name": lang.name} for lang in pycountry.languages if hasattr(lang, "alpha_2")
    ]
    translation_languages_dataset.sort(key=lambda x: x["name"])
    enabled_translation_language_codes = [lang.language_code for lang in db_booth.translation_languages if lang.enabled]
    return templates.TemplateResponse(
        request=request,
        name="admin/booth_detail.html",
        context={
            "event": event,
            "room": room,
            "booth": db_booth,
            "booth_id": bid,
            "mediamtx_path": mediamtx_path,
            "whep_url": whep_url,
            "is_live": is_live,
            "participants": participants,
            "active_interpreter": active_interpreter,
            "tokens": tokens,
            "users": users,
            "memberships": memberships,
            "membership_map": membership_map,
            "translation_languages_dataset": translation_languages_dataset,
            "enabled_translation_language_codes": enabled_translation_language_codes,
            **admin_flags,
        },
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/members/", dependencies=[Depends(require_event_owner)])
async def admin_add_room_member(request: Request, event_id: int, room_id: int):
    """Add or update a room membership assignment for a user."""
    async with get_session() as session:
        await room_in_event(session, event_id, room_id)
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if role and assignment_error(role, "room"):
        return invalid_role_redirect(request, f"events/{event_id}/rooms/{room_id}/", role)
    if email:
        async with get_session() as session:
            room = await room_in_event(session, event_id, room_id)
            user = await get_user_by_email(session, email)
            if not user:
                display_name = email.split("@")[0]
                user = await create_user(session, email=email, display_name=display_name, email_verified=False)

            uid = user.id
            if role:
                try:
                    await set_room_membership(session, user_id=uid, room_id=room_id, role=role)
                    await revoke_membership_push(session, user_id=uid, level="room", target_id=room_id)
                    # Send invite
                    event = await get_event_by_id(session, event_id)
                    if event and room:
                        target_url = "/account"
                        await _send_admin_invite(
                            session, user, role, f"room ({room.display_name}) for {event.display_name}", target_url
                        )
                except ValueError:
                    return safe_redirect(
                        url=management_url(request, f"events/{event_id}/rooms/{room_id}/?error=invalid_role&role={urllib.parse.quote(role)}"),
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_room(session, room_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_room_membership(session, m.id)
                        await revoke_membership_push(session, user_id=uid, level="room", target_id=room_id)
                        break
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/members/{membership_id}/invite", dependencies=[Depends(require_event_owner)]
)
async def admin_invite_room_member(request: Request, event_id: int, room_id: int, membership_id: int):
    """Send an invitation email to a room member."""
    async with get_session() as session:
        room = await room_in_event(session, event_id, room_id)
        membership = await session.scalar(
            select(RoomMembership).where(RoomMembership.id == membership_id, RoomMembership.room_id == room_id)
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Membership not found.")
        user = await get_user_by_id(session, membership.user_id)
        event = await get_event_by_id(session, event_id)
        if user and event:
            target_url = "/account"
            await _send_admin_invite(
                session, user, membership.role, f"room ({room.display_name}) for {event.display_name}", target_url
            )
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/?success=invite_sent"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/members/{membership_id}/delete", dependencies=[Depends(require_event_owner)]
)
async def admin_remove_room_member(request: Request, event_id: int, room_id: int, membership_id: int):
    """Remove a room membership assignment."""
    try:
        async with get_session() as session:
            await room_in_event(session, event_id, room_id)
            membership = await session.scalar(
                select(RoomMembership).where(RoomMembership.id == membership_id, RoomMembership.room_id == room_id)
            )
            if membership is None:
                raise HTTPException(status_code=404, detail="Membership not found.")
            await remove_team_assignment(session, event_id=event_id, level="room", membership_id=membership_id)
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), status_code=400)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/", dependencies=[Depends(require_event_owner)]
)
async def admin_add_booth_member(request: Request, event_id: int, room_id: int, booth_id: int):
    """Add or update a booth membership assignment for a user."""
    async with get_session() as session:
        await booth_in_room(session, event_id, room_id, booth_id)
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if role and assignment_error(role, "booth"):
        return invalid_role_redirect(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/", role)
    if email:
        async with get_session() as session:
            booth = await booth_in_room(session, event_id, room_id, booth_id)
            user = await get_user_by_email(session, email)
            if not user:
                display_name = email.split("@")[0]
                user = await create_user(session, email=email, display_name=display_name, email_verified=False)

            uid = user.id
            if role:
                try:
                    await set_booth_membership(session, user_id=uid, booth_id=booth_id, role=role)
                    await revoke_membership_push(session, user_id=uid, level="booth", target_id=booth_id)
                    # Send invite
                    event = await get_event_by_id(session, event_id)
                    if event and booth:
                        target_url = "/interpreter"
                        await _send_admin_invite(
                            session, user, role, f"booth ({booth.language_code}) for {event.display_name}", target_url
                        )
                except ValueError:
                    return safe_redirect(
                        url=management_url(
                            request,
                            f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=invalid_role&role={urllib.parse.quote(role)}",
                        ),
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_booth(session, booth_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_booth_membership(session, m.id)
                        await revoke_membership_push(session, user_id=uid, level="booth", target_id=booth_id)
                        break
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/{membership_id}/invite",
    dependencies=[Depends(require_event_owner)],
)
async def admin_invite_booth_member(request: Request, event_id: int, room_id: int, booth_id: int, membership_id: int):
    """Send an invitation email to a booth member."""
    async with get_session() as session:
        booth = await booth_in_room(session, event_id, room_id, booth_id)
        membership = await session.scalar(
            select(BoothMembership).where(BoothMembership.id == membership_id, BoothMembership.booth_id == booth_id)
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Membership not found.")
        user = await get_user_by_id(session, membership.user_id)
        event = await get_event_by_id(session, event_id)
        if user and event and booth:
            target_url = "/interpreter"
            await _send_admin_invite(
                session, user, membership.role, f"booth ({booth.language_code}) for {event.display_name}", target_url
            )
    return safe_redirect(
        url=management_url(
            request,
            f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/?success=invite_sent",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/{membership_id}/delete",
    dependencies=[Depends(require_event_owner)],
)
async def admin_remove_booth_member(request: Request, event_id: int, room_id: int, booth_id: int, membership_id: int):
    """Remove a booth membership assignment."""
    try:
        async with get_session() as session:
            await booth_in_room(session, event_id, room_id, booth_id)
            membership = await session.scalar(
                select(BoothMembership).where(BoothMembership.id == membership_id, BoothMembership.booth_id == booth_id)
            )
            if membership is None:
                raise HTTPException(status_code=404, detail="Membership not found.")
            await remove_team_assignment(session, event_id=event_id, level="booth", membership_id=membership_id)
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), status_code=400)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/delete", dependencies=[Depends(require_event_owner)])
async def admin_delete_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    """Delete an interpretation booth from a room."""
    async with get_session() as session:
        await booth_in_room(session, event_id, room_id, booth_id)
        await revoke_booths_for_deleted_entity(session, level="booth", target_id=booth_id)
        await delete_booth(session, booth_id)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
    dependencies=[Depends(require_event_owner)],
)
async def admin_booth_translation_settings(
    request: Request,
    event_id: int,
    room_id: int,
    booth_id: int,
    translation_enabled: bool | None = Form(False),
    translation_provider: str = Form("openai"),
    translation_model: str = Form("gpt-4o-mini"),
    translation_languages: list[str] = Form([]),
):
    async with get_session() as session:
        db_booth = await booth_in_room(session, event_id, room_id, booth_id)
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        try:
            TranslationProviderEnum(translation_provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid translation provider")
        db_booth.translation_enabled = translation_enabled
        db_booth.translation_provider = translation_provider
        db_booth.translation_model = translation_model
        current_langs = {lang.language_code: lang for lang in db_booth.translation_languages}
        for code in translation_languages:
            if code in current_langs:
                current_langs[code].enabled = True
            else:
                lang_obj = pycountry.languages.get(alpha_2=code)
                lang_name = lang_obj.name if lang_obj else code
                db_booth.translation_languages.append(
                    BoothTranslationLanguage(
                        booth_id=db_booth.id, language_code=code, language_name=lang_name, enabled=True
                    )
                )
        for code, lang_model in current_langs.items():
            if code not in translation_languages:
                lang_model.enabled = False
        await session.flush()
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/edit", dependencies=[Depends(require_event_owner)])
async def admin_edit_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    form = await request.form()
    language_name = form.get("language_name", "").strip()
    language_code_raw = form.get("language_code", "").strip()
    async with get_session() as session:
        booth = await booth_in_room(session, event_id, room_id, booth_id)
        if language_name:
            booth.language_name = language_name
        if language_code_raw:
            try:
                booth.language_code = validate_language_code(language_code_raw)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        await session.flush()
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/transcription-settings",
    dependencies=[Depends(require_event_owner)],
)
async def admin_transcription_settings(
    request: Request,
    event_id: int,
    room_id: int,
    booth_id: int,
    transcription_enabled: bool | None = Form(False),
    transcription_provider: str = Form("local"),
    transcription_model: str = Form("tiny"),
):
    async with get_session() as session:
        db_booth = await booth_in_room(session, event_id, room_id, booth_id)
        try:
            provider_enum = ProviderEnum(transcription_provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid transcription provider")
        if transcription_model not in ALLOWED_MODELS.get(provider_enum, set()):
            raise HTTPException(
                status_code=400, detail=f"Invalid model '{transcription_model}' for provider '{transcription_provider}'"
            )
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        if provider_enum != ProviderEnum.LOCAL:
            if not event.transcription_api_enabled:
                raise HTTPException(status_code=400, detail="External API transcription is disabled for this event.")
            if not get_api_key(event, provider_enum):
                raise HTTPException(
                    status_code=400, detail=f"API key for {transcription_provider} is not configured on the event."
                )
        old_enabled = db_booth.transcription_enabled
        old_provider = db_booth.transcription_provider
        old_model = db_booth.transcription_model
        db_booth.transcription_enabled = bool(transcription_enabled)
        db_booth.transcription_provider = transcription_provider
        db_booth.transcription_model = transcription_model
        await session.flush()
        bid = make_booth_id(event.slug, db_booth.room_id, db_booth.language_code)
        state = booths.get_booth_sync(bid)
        is_live = state is not None and state.active_interpreter_id is not None
        if is_live:
            if not transcription_enabled:
                await stop_transcription_worker(bid)
                await broadcast_transcription(bid, "")
            elif (
                old_enabled != transcription_enabled
                or old_provider != transcription_provider
                or old_model != transcription_model
            ):
                await stop_transcription_worker(bid)
                await broadcast_transcription(bid, "")
                api_key = get_api_key(event, ProviderEnum(transcription_provider))
                config = ProviderConfig(api_key=api_key)
                await asyncio.sleep(0.1)
                await start_transcription_worker(
                    event.slug,
                    db_booth.language_code,
                    bid,
                    broadcast_transcription,
                    transcription_provider,
                    transcription_model,
                    config,
                    room_id=db_booth.room_id,
                )
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


USER_SHOW_OPTIONS = (
    ("all", "All users"),
    ("admins", "Admins"),
    ("hosting", "Hosting access"),
    ("deactivated", "Deactivated"),
    ("unverified", "Email not verified"),
)


@router.get("/admin/users/", dependencies=[Depends(require_admin)])
async def admin_users(
    request: Request,
    page: int = 1,
    limit: int = 50,
    search: str | None = None,
    show: str = "all",
    sort_by: str = "created_at",
    sort_order: str = "desc",
):
    """Everyone with a VoxBento account, newest first, with inline access toggles (#765)."""
    search = (search or "").strip() or None
    if show not in USER_FILTERS:
        show = "all"
    if sort_by not in USER_SORT_COLUMNS:
        sort_by = "created_at"
    sort_order = "asc" if sort_order == "asc" else "desc"
    limit = min(max(limit, 1), 200)
    async with get_session() as session:
        total_users = await count_users(session, search=search, show=show)
        total_pages = max(1, math.ceil(total_users / limit))
        page = min(max(page, 1), total_pages)
        users = await list_users(
            session,
            limit=limit,
            offset=(page - 1) * limit,
            search=search,
            show=show,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        user_ids = [u.id for u in users]
        token_map = {}
        if user_ids:
            stmt = (
                select(AuthToken)
                .where(
                    AuthToken.user_id.in_(user_ids),
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                    AuthToken.used_at.is_(None),
                )
                .order_by(AuthToken.created_at.desc())
            )
            for tok in (await session.execute(stmt)).scalars().all():
                token_map.setdefault(tok.user_id, tok)
        active_admin_count = await count_active_admins(session)

    return templates.TemplateResponse(
        request=request,
        name="admin/user_list.html",
        context={
            "users": users,
            "total_users": total_users,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "search": search,
            "show": show,
            "show_options": USER_SHOW_OPTIONS,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "token_map": token_map,
            "current_user_id": request.state.principal["user_id"],
            "active_admin_count": active_admin_count,
            "notice": user_notice(request),
            "is_admin": True,
            "is_event_owner": True,
            "is_room_coordinator": True,
        },
    )


async def room_in_event(session, event_id: int, room_id: int):
    """The room in the URL, or 404 when it isn't part of the event in the URL."""
    room = await get_room_by_id(session, room_id)
    if room is None or room.event_id != event_id:
        raise HTTPException(status_code=404, detail="Room not found.")
    return room


async def booth_in_room(session, event_id: int, room_id: int, booth_id: int):
    """The booth in the URL, or 404 when the room or booth isn't in the event in the URL."""
    await room_in_event(session, event_id, room_id)
    booth = await get_booth_by_id(session, booth_id)
    if booth is None or booth.event_id != event_id or booth.room_id != room_id:
        raise HTTPException(status_code=404, detail="Booth not found.")
    return booth


def invalid_role_redirect(request: Request, path: str, role: str) -> RedirectResponse:
    """Back to the form with the refused role, so the page can say why (portal.roles.assignment_error)."""
    return safe_redirect(
        url=management_url(request, f"{path}?error=invalid_role&role={urllib.parse.quote(role)}"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def user_notice(request: Request) -> tuple[str, str] | None:
    """("success" | "error", message) for a change made without JavaScript."""
    error = request.query_params.get("error")
    if error in USER_ADMIN_ERRORS:
        return "error", USER_ADMIN_ERRORS[error]
    if error in LEGACY_USER_ERRORS:
        return "error", LEGACY_USER_ERRORS[error]
    notice = request.query_params.get("notice") or request.query_params.get("success")
    if notice in USER_NOTICES:
        return "success", USER_NOTICES[notice]
    return None


# Codes still sent by the invitation and hosting endpoints.
LEGACY_USER_ERRORS = {
    "already_host": "This person already has hosting access.",
    "already_admin": "This person is already an Admin.",
    "invalid_email": "Enter a valid email address.",
    "invite_not_found": "That invitation was not found or has expired.",
}


USER_NOTICES = {
    "hosting_on": "Hosting access granted.",
    "hosting_off": "Hosting access removed. Existing events are kept.",
    "admin_on": "This person is now an Admin.",
    "admin_off": "This person is no longer an Admin.",
    "active_on": "The account was reactivated.",
    "active_off": "The account was deactivated and signed out.",
    "deleted": "The account was deleted.",
    "invite_sent": "Invitation sent successfully.",
    "invite_resent": "Invitation resent.",
    "invite_cancelled": "Invitation cancelled.",
    "hosting_granted": "Hosting access granted.",
    "hosting_revoked": "Hosting access removed. Existing events are kept.",
}


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def user_action_redirect(request: Request, user_id: int, query: str) -> RedirectResponse:
    """Send a plain form post back to the page it came from."""
    referrer = request.headers.get("referer", "")
    base = f"/admin/users/{user_id}/" if f"/admin/users/{user_id}/" in referrer else "/admin/users/"
    return safe_redirect(url=f"{base}?{query}", status_code=status.HTTP_303_SEE_OTHER)


async def apply_user_access(request: Request, user_id: int, field: str, value: bool | None) -> AccessChange:
    """Run one access change for the signed-in Admin; value None flips the current state."""
    async with get_session() as session:
        actor_id = request.state.principal["user_id"]
        if value is None:
            user = await get_user_by_id(session, user_id)
            if user is None:
                raise UserAdminError("not_found", 404)
            value = not {
                "hosting": user.can_host,
                "admin": user.is_admin,
                "active": user.is_active,
                "verified": user.email_verified,
            }[field]
        change = await set_user_access(session, actor_id=actor_id, user_id=user_id, field=field, value=value)
        if change.changed:
            messages = {
                "hosting": "Hosting access was granted." if value else "Hosting access was removed.",
                "admin": "You are now an Admin." if value else "Your Admin access was removed.",
                "active": "Your account was reactivated." if value else "Your account was deactivated.",
                "verified": "Your email address was verified by an Admin." if value else "Your email verification was removed.",
            }
            notification_key = "hosting_granted" if field == "hosting" and value else "account_access"
            context = (
                {"login_url": f"{settings.public_base_url.rstrip('/')}/workspace/"}
                if notification_key == "hosting_granted"
                else {"message": messages[field], "login_url": f"{settings.public_base_url.rstrip('/')}/login"}
            )
            await queue_notification(
                session,
                notification_key,
                change.email,
                context,
                idempotency_key=f"access:{change.user_id}:{field}:{value}:{utc_now().isoformat()}",
            )
    if field in ("admin", "active") and not value:
        # Demotion or deactivation invalidates every live booth session for this
        # account. The sweep runs whenever the resulting state is demoted/inactive
        # (idempotent, does not depend on change.changed) so that retrying an
        # interrupted request still evicts live sessions. Wrapped in asyncio.shield
        # so client cancellation does not abort the revocation after commit.
        await asyncio.shield(manager.close_user_sessions(change.user_id))
    return change


@router.post("/admin/users/{user_id}/access", dependencies=[Depends(require_admin)])
async def admin_set_user_access(request: Request, user_id: int):
    """Turn hosting access, Admin or the account on or off (users list toggles and user page buttons)."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    field = form.get("field")
    raw_value = form.get("value")
    if field not in USER_ACCESS_FIELDS or raw_value not in ("true", "false"):
        error = UserAdminError("invalid")
    else:
        try:
            change = await apply_user_access(request, user_id, field, raw_value == "true")
        except UserAdminError as exc:
            error = exc
        else:
            if wants_json(request):
                return JSONResponse({"ok": True, "field": change.field, "value": change.value, "message": change.message})
            return user_action_redirect(request, user_id, f"notice={change.notice}")
    if wants_json(request):
        return JSONResponse({"ok": False, "detail": error.message}, status_code=error.status_code)
    return user_action_redirect(request, user_id, f"error={error.code}")


@router.post("/admin/users/invite", dependencies=[Depends(require_admin)])
@router.post("/admin/users/invite/", dependencies=[Depends(require_admin)])
async def admin_invite_user(request: Request):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    form = await request.form()
    raw_email = form.get("email", "").strip().lower()
    display_name = form.get("display_name", "").strip()

    if not raw_email or "@" not in raw_email or "." not in raw_email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email address.")

    async with get_session() as session:
        user = await get_user_by_email(session, raw_email)
        if user is not None:
            if user.is_admin:
                raise HTTPException(status_code=400, detail="User is already an administrator.")
            if not user.is_active:
                raise HTTPException(status_code=400, detail="Cannot invite inactive user.")
            if user.can_host:
                accept_header = request.headers.get("accept", "")
                if "text/html" in accept_header:
                    return safe_redirect(
                        url=hosting_return_url(request, "/admin/users/", "error=already_host"),
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                raise HTTPException(status_code=400, detail="User already has hosting access.")
            target_user = user
        else:
            stub_name = display_name or raw_email.split("@")[0]
            target_user = User(
                email=raw_email,
                display_name=stub_name,
                password_hash=None,
                email_verified=False,
                can_host=False,
                is_active=True,
            )
            session.add(target_user)
            await session.flush()

        delete_stmt = (
            select(AuthToken)
            .where(
                AuthToken.user_id == target_user.id,
                AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                AuthToken.used_at.is_(None),
            )
        )
        existing_tokens = (await session.execute(delete_stmt)).scalars().all()
        for et in existing_tokens:
            await session.delete(et)
        await session.flush()

        raw_token = secrets.token_urlsafe(32)
        token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        invite_token = await create_auth_token(
            session,
            user_id=target_user.id,
            token_type=TOKEN_TYPE_HOSTING_INVITE,
            jti=token_digest,
            expires_in_seconds=7 * 86400,
        )

        invite_url = f"{settings.public_base_url.rstrip('/')}/invite/accept/{raw_token}"
        await queue_notification(
            session,
            "hosting_invite",
            target_user.email,
            {"invite_url": invite_url, "is_existing_user": bool(target_user.password_hash)},
            expires_at=invite_token.expires_at,
            idempotency_key=f"hosting-invite:{invite_token.jti}",
            auth_token_id=invite_token.id,
        )
    return safe_redirect(url=hosting_return_url(request, "/admin/users/", "success=invite_sent"), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/{user_id}/hosting", dependencies=[Depends(require_admin)])
@router.post("/admin/users/{user_id}/hosting/", dependencies=[Depends(require_admin)])
async def admin_toggle_user_hosting(request: Request, user_id: int):
    """Grant or remove hosting access (used by the Hosting access page)."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    action = form.get("action")
    value = True if action == "grant" else False if action == "revoke" else None
    try:
        change = await apply_user_access(request, user_id, "hosting", value)
    except UserAdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    referrer = request.headers.get("referer", "")
    if f"/admin/users/{user_id}" in referrer:
        return safe_redirect(url=f"/admin/users/{user_id}/", status_code=status.HTTP_303_SEE_OTHER)
    outcome = f"success=hosting_{'granted' if change.value else 'revoked'}"
    return safe_redirect(url=hosting_return_url(request, "/admin/users/", outcome), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/invite/{token_id}/cancel", dependencies=[Depends(require_admin)])
@router.post("/admin/users/invite/{token_id}/cancel/", dependencies=[Depends(require_admin)])
async def admin_cancel_invite(request: Request, token_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    user_id = None
    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.id == token_id, AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE)
        tok = (await session.execute(tok_stmt)).scalar_one_or_none()
        if tok:
            user_id = tok.user_id
            await session.delete(tok)

    return safe_redirect(
        url=hosting_return_url(request, "/admin/users/", "success=invite_cancelled", user_id=user_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/users/invite/{token_id}/resend", dependencies=[Depends(require_admin)])
@router.post("/admin/users/invite/{token_id}/resend/", dependencies=[Depends(require_admin)])
async def admin_resend_invite(request: Request, token_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.id == token_id, AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE)
        old_tok = (await session.execute(tok_stmt)).scalar_one_or_none()
        if old_tok is None:
            raise HTTPException(status_code=404, detail="Invitation not found.")

        user = await get_user_by_id(session, old_tok.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="User is inactive or not found.")

        user_id = user.id

        await session.delete(old_tok)
        await session.flush()

        raw_token = secrets.token_urlsafe(32)
        token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        invite_token = await create_auth_token(
            session,
            user_id=user_id,
            token_type=TOKEN_TYPE_HOSTING_INVITE,
            jti=token_digest,
            expires_in_seconds=7 * 86400,
        )

        invite_url = f"{settings.public_base_url.rstrip('/')}/invite/accept/{raw_token}"
        await queue_notification(
            session,
            "hosting_invite",
            user.email,
            {"invite_url": invite_url, "is_existing_user": bool(user.password_hash)},
            expires_at=invite_token.expires_at,
            idempotency_key=f"hosting-invite:{invite_token.jti}",
            auth_token_id=invite_token.id,
        )
    return safe_redirect(
        url=hosting_return_url(request, "/admin/users/", "success=invite_resent", user_id=user_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


HOSTING_ACCESS_URL = "/admin/hosting-access/"
HOSTING_REQUESTS_URL = "/admin/hosting-access/requests/"


def hosting_return_url(request: Request, default: str, query: str, user_id: int | None = None) -> str:
    """Send hosting actions taken from the Hosting access or User detail page back to it."""
    referrer = request.headers.get("referer", "")
    try:
        ref_path = urllib.parse.urlsplit(referrer).path if referrer else ""
    except ValueError:
        ref_path = ""
    if user_id is not None and ref_path == f"/admin/users/{user_id}/":
        base = f"/admin/users/{user_id}/"
    elif ref_path == HOSTING_ACCESS_URL:
        base = HOSTING_ACCESS_URL
    else:
        base = default
    return f"{base}?{query}"


@dataclass(frozen=True)
class HostingAccessEntry:
    user: User
    events_owned: int
    pending_invite: AuthToken | None


async def list_hosting_access_entries(session: AsyncSession) -> list[HostingAccessEntry]:
    """People with hosting access, then people with a pending hosting invitation."""
    hosts = (
        await session.execute(
            select(User)
            .where(User.can_host.is_(True), User.is_admin.is_(False))
            .order_by(func.lower(User.display_name), User.id)
        )
    ).scalars().all()
    invites = (
        await session.execute(
            select(AuthToken)
            .options(joinedload(AuthToken.user))
            .join(User, User.id == AuthToken.user_id)
            .where(
                AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                AuthToken.used_at.is_(None),
                AuthToken.expires_at > utc_now(),
                User.can_host.is_(False),
            )
            .order_by(AuthToken.created_at.desc())
        )
    ).scalars().all()
    latest_invites: dict[int, AuthToken] = {}
    for tok in invites:
        latest_invites.setdefault(tok.user_id, tok)

    user_ids = [u.id for u in hosts] + list(latest_invites)
    owned_counts: dict[int, int] = {}
    if user_ids:
        rows = await session.execute(
            select(EventMembership.user_id, func.count(EventMembership.id))
            .where(EventMembership.user_id.in_(user_ids), EventMembership.role == EVENT_OWNER)
            .group_by(EventMembership.user_id)
        )
        owned_counts = dict(rows.all())

    entries = [HostingAccessEntry(user=u, events_owned=owned_counts.get(u.id, 0), pending_invite=None) for u in hosts]
    entries.extend(
        HostingAccessEntry(user=tok.user, events_owned=owned_counts.get(tok.user_id, 0), pending_invite=tok)
        for tok in latest_invites.values()
    )
    return entries


def render_hosting_access(
    request: Request,
    *,
    tab: str,
    entries: list[HostingAccessEntry],
    hosting_requests: list[HostingRequest],
):
    return templates.TemplateResponse(
        request=request,
        name="admin/hosting_access.html",
        context={
            "tab": tab,
            "entries": entries,
            "hosting_requests": hosting_requests,
            "open_hosting_requests": len(hosting_requests),
            "format_event_dates": format_event_dates,
            "decline_reason_max": DECLINE_REASON_MAX,
            "is_admin": True,
            "is_event_owner": True,
            "is_room_coordinator": True,
        },
    )


@router.get("/admin/hosting-access/", dependencies=[Depends(require_admin)])
async def admin_hosting_access(request: Request):
    """People with hosting access and pending hosting invitations (Admin only)."""
    async with get_session() as session:
        entries = await list_hosting_access_entries(session)
        hosting_requests = await list_open_hosting_requests(session)
    return render_hosting_access(request, tab="people", entries=entries, hosting_requests=hosting_requests)


@router.get("/admin/hosting-access/requests/", dependencies=[Depends(require_admin)])
async def admin_hosting_requests(request: Request):
    """Open hosting access requests, newest first (Admin only)."""
    async with get_session() as session:
        hosting_requests = await list_open_hosting_requests(session)
    return render_hosting_access(request, tab="requests", entries=[], hosting_requests=hosting_requests)


@router.post("/admin/hosting-access/requests/{request_id}/grant", dependencies=[Depends(require_admin)])
async def admin_grant_hosting_request(request: Request, request_id: int):
    """Grant hosting access to the requester and close the request."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    async with get_session() as session:
        principal = await resolve_principal(request, session)
        hosting_request = await get_hosting_request(session, request_id)
        if hosting_request is None or hosting_request.status != HOSTING_REQUEST_OPEN:
            return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?error=request_not_found", status_code=status.HTTP_303_SEE_OTHER)
        user = hosting_request.user
        if not user.is_active:
            return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?error=inactive_user", status_code=status.HTTP_303_SEE_OTHER)

        user.can_host = True
        user.hosting_revoked_at = None
        await close_hosting_request(
            session, hosting_request, status=HOSTING_REQUEST_GRANTED, decided_by_user_id=principal["user_id"]
        )
        pending_invites = (
            await session.execute(
                select(AuthToken).where(
                    AuthToken.user_id == user.id,
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                    AuthToken.used_at.is_(None),
                )
            )
        ).scalars().all()
        for tok in pending_invites:
            await session.delete(tok)
        await queue_notification(
            session,
            "hosting_granted",
            user.email,
            {"login_url": f"{settings.public_base_url.rstrip('/')}/workspace/"},
            idempotency_key=f"hosting-granted:{hosting_request.id}",
        )
        logger.info(
            "Admin %s granted hosting request %d for user %d", principal["user_id"], hosting_request.id, user.id
        )

    return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?success=request_granted", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/hosting-access/requests/{request_id}/decline", dependencies=[Depends(require_admin)])
async def admin_decline_hosting_request(request: Request, request_id: int):
    """Decline a hosting request and tell the requester why."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    form = await request.form()
    reason = multi_line(form.get("reason"))
    if not reason or len(reason) > DECLINE_REASON_MAX:
        return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?error=decline_reason", status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        principal = await resolve_principal(request, session)
        hosting_request = await get_hosting_request(session, request_id)
        if hosting_request is None or hosting_request.status != HOSTING_REQUEST_OPEN:
            return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?error=request_not_found", status_code=status.HTTP_303_SEE_OTHER)
        await close_hosting_request(
            session,
            hosting_request,
            status=HOSTING_REQUEST_DECLINED,
            decided_by_user_id=principal["user_id"],
            decline_reason=reason,
        )
        await queue_notification(
            session,
            "hosting_request_declined",
            hosting_request.user.email,
            {"reason": reason, "account_url": f"{settings.public_base_url.rstrip('/')}/account"},
            idempotency_key=f"hosting-declined:{hosting_request.id}",
        )
        logger.info(
            "Admin %s declined hosting request %d for user %d",
            principal["user_id"],
            hosting_request.id,
            hosting_request.user_id,
        )

    return safe_redirect(url=f"{HOSTING_REQUESTS_URL}?success=request_declined", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/{user_id}/toggle-active", dependencies=[Depends(require_admin)])
async def admin_toggle_user_active(request: Request, user_id: int):
    """Older form endpoint: flip the account between active and deactivated."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    try:
        change = await apply_user_access(request, user_id, "active", None)
    except UserAdminError as exc:
        return user_action_redirect(request, user_id, f"error={exc.code}")
    return user_action_redirect(request, user_id, f"notice={change.notice}")


@router.post("/admin/users/{user_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_user(request: Request, user_id: int):
    """Delete an account and all its roles; the form must repeat the account's email."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    confirm_email = form.get("confirm_email")
    try:
        async with get_session() as session:
            deleted_email = await delete_user_account(
                session,
                actor_id=request.state.principal["user_id"],
                user_id=user_id,
                confirm_email=confirm_email if isinstance(confirm_email, str) else "",
            )
            await queue_notification(
                session,
                "account_access",
                deleted_email,
                {
                    "message": "Your VoxBento account was deleted by an administrator, with all its event roles.",
                    "login_url": settings.public_base_url.rstrip("/") + "/",
                },
                idempotency_key=f"account-deleted:{user_id}",
            )
    except UserAdminError as exc:
        return safe_redirect(url=f"/admin/users/{user_id}/?error={exc.code}", status_code=status.HTTP_303_SEE_OTHER)
    # The account and its memberships are gone; end any live booth sessions.
    await asyncio.shield(manager.close_user_sessions(user_id))
    return safe_redirect(url="/admin/users/?notice=deleted", status_code=status.HTTP_303_SEE_OTHER)


@dataclass(frozen=True)
class UserRoleRow:
    event: Event
    where: str
    role: str
    since: object


async def user_roles_and_owned_events(session: AsyncSession, user_id: int) -> tuple[list[UserRoleRow], list[Event]]:
    """Roles a person holds in events (other than owner), and the events they own."""
    rows: list[UserRoleRow] = []
    owned: list[Event] = []
    for m in await list_memberships_for_user(session, user_id):
        if m.event is None or m.event.deleted_at is not None:
            continue
        if m.role == EVENT_OWNER:
            owned.append(m.event)
        else:
            rows.append(UserRoleRow(m.event, "Whole event", role_label(m.role), m.created_at))
    for m in await list_room_memberships_for_user(session, user_id):
        if m.room and m.room.event and m.room.event.deleted_at is None:
            rows.append(UserRoleRow(m.room.event, m.room.display_name, role_label(m.role), m.created_at))
    for m in await list_booth_memberships_for_user(session, user_id):
        booth = m.booth
        if booth and booth.event and booth.event.deleted_at is None:
            room = booth.room.display_name if booth.room else ""
            where = " · ".join(part for part in (room, f"{booth.language_name} booth") if part)
            rows.append(UserRoleRow(booth.event, where, role_label(m.role), m.created_at))
    rows.sort(key=lambda r: (r.event.display_name.casefold(), r.where.casefold()))
    owned.sort(key=lambda e: e.display_name.casefold())
    return rows, owned


@router.get("/admin/users/{user_id}/", dependencies=[Depends(require_admin)])
async def admin_user_detail(request: Request, user_id: int):
    """One person's access, roles and account, with the actions Admins can take (#765)."""
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if not user:
            return safe_redirect(url="/admin/users/", status_code=status.HTTP_303_SEE_OTHER)
        roles, owned_events = await user_roles_and_owned_events(session, user_id)
        pending_token = None
        if not user.can_host:
            tok_stmt = (
                select(AuthToken)
                .where(
                    AuthToken.user_id == user_id,
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                    AuthToken.used_at.is_(None),
                )
                .order_by(AuthToken.created_at.desc())
            )
            pending_token = (await session.execute(tok_stmt)).scalars().first()
        open_request = await get_open_hosting_request(session, user_id)
        developer_account = await get_developer_account(session, user_id)
        is_last_admin = await is_last_active_admin(session, user)

    return templates.TemplateResponse(
        request=request,
        name="admin/user_detail.html",
        context={
            "user_detail": user,
            "roles": roles,
            "owned_events": owned_events,
            "pending_token": pending_token,
            "open_request": open_request,
            "developer_account": developer_account,
            "is_self": user.id == request.state.principal["user_id"],
            "is_last_admin": is_last_admin,
            "notice": user_notice(request),
            "is_admin": True,
            "is_event_owner": True,
            "is_room_coordinator": True,
        },
    )


@router.post("/admin/users/{user_id}/toggle-admin", dependencies=[Depends(require_admin)])
async def admin_toggle_user_admin(request: Request, user_id: int):
    """Older form endpoint: flip Admin. Event roles are managed by each event's owners (#765)."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    try:
        change = await apply_user_access(request, user_id, "admin", None)
    except UserAdminError as exc:
        return user_action_redirect(request, user_id, f"error={exc.code}")
    return user_action_redirect(request, user_id, f"notice={change.notice}")


TEAM_NOTICES = {
    "added": "Added. They'll get an email with a link to sign in.",
    "updated": "Their role was updated.",
    "already": "They already have that role there.",
    "removed": "Removed from the team.",
    "resent": "Invitation sent again.",
    "link_created": "Invite link created. Copy it and send it to the interpreter.",
    "link_revoked": "Invite link revoked. It no longer works.",
}


async def render_team(request: Request, event_id: int, *, error: str = "", form: dict | None = None, status_code: int = 200):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        team = await load_team(session, event_id)
    notice = TEAM_NOTICES.get(request.query_params.get("notice", ""))
    return templates.TemplateResponse(
        request=request,
        name="admin/team.html",
        context={
            "event": event,
            "team": team,
            "role_choices": TEAM_ROLE_CHOICES,
            "booth_roles": sorted(roles_module.BOOTH_ROLES, key=lambda r: -roles_module.ROLE_RANK[r]),
            "error": error,
            "notice": notice,
            "form": form or {},
            "current_user_id": request.state.principal["user_id"],
            "public_base_url": settings.public_base_url.rstrip("/"),
            **await get_access_flags(request, event_id=event_id),
        },
        status_code=status_code,
    )


def team_redirect(request: Request, event_id: int, notice: str) -> RedirectResponse:
    return safe_redirect(url=management_url(request, f"events/{event_id}/team/?notice={notice}"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/team/", dependencies=[Depends(require_event_owner)])
async def admin_event_team(request: Request, event_id: int):
    """Everyone with a role in the event, and its invite links, managed in one place."""
    return await render_team(request, event_id)


@router.post("/admin/events/{event_id}/team/add", dependencies=[Depends(require_event_owner)])
async def admin_team_add(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    data = {key: str(form.get(key, "")).strip() for key in ("email", "role", "where")}
    try:
        async with get_session() as session:
            team = await load_team(session, event_id)
            place = parse_team_place(data["where"], team)
            user, changed = await add_team_member(session, event_id=event_id, email=data["email"], role=data["role"], place=place)
            event = await get_event_by_id(session, event_id)
            if changed:
                try:
                    await _send_admin_invite(
                        session, user, role_label(data["role"]), f"{place.label} at {event.display_name}", "/workspace/"
                    )
                except Exception as e:
                    logger.warning(f"Failed to send team invite email to {user.email}: {e}")
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), form=data, status_code=400)
    return team_redirect(request, event_id, "added" if changed else "already")


@router.post("/admin/events/{event_id}/team/remove", dependencies=[Depends(require_event_owner)])
async def admin_team_remove(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    try:
        membership_id = int(str(form.get("membership_id", "")))
    except ValueError:
        return await render_team(request, event_id, error="That person is no longer on this team.", status_code=400)
    try:
        async with get_session() as session:
            await remove_team_assignment(
                session, event_id=event_id, level=str(form.get("level", "")), membership_id=membership_id
            )
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), status_code=400)
    return team_redirect(request, event_id, "removed")


@router.post("/admin/events/{event_id}/team/resend", dependencies=[Depends(require_event_owner)])
async def admin_team_resend(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    async with get_session() as session:
        team = await load_team(session, event_id)
        member = next((m for m in team.members if str(m.user.id) == str(form.get("user_id", ""))), None)
        if member is None:
            return await render_team(request, event_id, error="That person is no longer on this team.", status_code=400)
        event = await get_event_by_id(session, event_id)
        first = member.assignments[0]
        try:
            await _send_admin_invite(
                session, member.user, role_label(first.role), f"{first.place.label} at {event.display_name}", "/workspace/"
            )
        except Exception as e:
            logger.warning(f"Failed to resend team invite email to {member.user.email}: {e}")
    return team_redirect(request, event_id, "resent")


@router.post("/admin/events/{event_id}/team/links", dependencies=[Depends(require_event_owner)])
async def admin_team_create_link(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    data = {key: str(form.get(key, "")).strip() for key in ("booth", "link_role", "label", "expires_hours")}
    try:
        async with get_session() as session:
            team = await load_team(session, event_id)
            await create_team_invite_link(
                session,
                team=team,
                booth_key=data["booth"],
                role=data["link_role"],
                label=data["label"],
                expires_hours=data["expires_hours"],
            )
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), form=data, status_code=400)
    return team_redirect(request, event_id, "link_created")


@router.post("/admin/events/{event_id}/team/links/revoke", dependencies=[Depends(require_event_owner)])
async def admin_team_revoke_link(request: Request, event_id: int):
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    form = await request.form()
    try:
        async with get_session() as session:
            team = await load_team(session, event_id)
            await revoke_team_invite_link(session, team=team, token=str(form.get("token", "")))
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), status_code=400)
    return team_redirect(request, event_id, "link_revoked")


@router.get("/admin/events/{event_id}/members/", dependencies=[Depends(require_event_owner)])
async def admin_event_members(request: Request, event_id: int):
    """The old Members page; everyone with a role is now on the Team page."""
    return safe_redirect(url=management_url(request, f"events/{event_id}/team/"), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/events/{event_id}/members/", dependencies=[Depends(require_event_owner)])
async def admin_add_event_member(request: Request, event_id: int):
    """Add or update an event membership assignment for a user."""
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if role and assignment_error(role, "event"):
        return invalid_role_redirect(request, f"events/{event_id}/members/", role)
    if email:
        async with get_session() as session:
            event = await get_event_by_id(session, event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="Event not found.")
            user = await get_user_by_email(session, email)
            if not user:
                display_name = email.split("@")[0]
                user = await create_user(session, email=email, display_name=display_name, email_verified=False)

            uid = user.id
            if role:
                try:
                    await set_event_membership(session, user_id=uid, event_id=event_id, role=role)
                    await revoke_membership_push(session, user_id=uid, level="event", target_id=event_id)
                    # Send invite
                    if event:
                        target_url = "/account"
                        await _send_admin_invite(session, user, role, f"event {event.display_name}", target_url)
                except ValueError:
                    return safe_redirect(
                        url=management_url(request, f"events/{event_id}/members/?error=invalid_role&role={urllib.parse.quote(role)}"),
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_event(session, event_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_event_membership(session, m.id)
                        await revoke_membership_push(session, user_id=uid, level="event", target_id=event_id)
                        break
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/members/"), status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/events/{event_id}/members/{membership_id}/invite", dependencies=[Depends(require_event_owner)])
async def admin_invite_event_member(request: Request, event_id: int, membership_id: int):
    """Send an invitation email to an event member."""
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        membership = await session.scalar(
            select(EventMembership).where(EventMembership.id == membership_id, EventMembership.event_id == event_id)
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Membership not found.")
        user = await get_user_by_id(session, membership.user_id)
        if user and event:
            target_url = "/account"
            await _send_admin_invite(session, user, membership.role, f"event {event.display_name}", target_url)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/members/?success=invite_sent"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/events/{event_id}/members/{membership_id}/delete", dependencies=[Depends(require_event_owner)])
async def admin_remove_event_member(request: Request, event_id: int, membership_id: int):
    """Remove an event membership assignment."""
    try:
        async with get_session() as session:
            event = await get_event_by_id(session, event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="Event not found.")
            membership = await session.scalar(
                select(EventMembership).where(
                    EventMembership.id == membership_id,
                    EventMembership.event_id == event_id,
                )
            )
            if membership is None:
                raise HTTPException(status_code=404, detail="Membership not found.")
            await remove_team_assignment(session, event_id=event_id, level="event", membership_id=membership_id)
    except TeamError as exc:
        return await render_team(request, event_id, error=str(exc), status_code=400)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/members/"), status_code=status.HTTP_303_SEE_OTHER
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/", dependencies=[Depends(require_event_owner)]
)
async def admin_create_token(request: Request, event_id: int, room_id: int, booth_id: int):
    """Create an invite token for a booth."""
    async with get_session() as session:
        await booth_in_room(session, event_id, room_id, booth_id)
    form = await request.form()
    role = form.get("role", "").strip()
    if role and assignment_error(role, "booth"):
        return invalid_role_redirect(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/", role)
    label = form.get("label", "").strip()
    expires_hours = form.get("expires_hours", "").strip()
    expires_at = None
    if expires_hours:
        try:
            expires_at = utc_now() + timedelta(hours=int(expires_hours))
        except ValueError:
            pass
    async with get_session() as session:
        await booth_in_room(session, event_id, room_id, booth_id)
        if role:
            await create_invite_token(session, booth_id=booth_id, role=role, label=label, expires_at=expires_at)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/{token_id}/revoke",
    dependencies=[Depends(require_event_owner)],
)
async def admin_revoke_token(request: Request, event_id: int, room_id: int, booth_id: int, token_id: str):
    async with get_session() as session:
        await booth_in_room(session, event_id, room_id, booth_id)
        token = await get_invite_token(session, token_id)
        if token is None or token.booth_id != booth_id:
            raise HTTPException(status_code=404, detail="Invite link not found.")
        await revoke_invite_token(session, token_id)
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/booths/{booth_id}/"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/api/admin/events/{event_id}/rooms/{room_id}/transcripts/{language_code}")
async def api_admin_get_transcripts(
    event_id: int,
    room_id: int,
    language_code: str,
    target_lang: str = Query(None),
    admin: bool = Depends(require_owner_or_coordinator),
):
    from sqlalchemy import select

    from portal.database import get_session

    async with get_session() as session:
        # require_owner_or_coordinator only checks access to event_id, so the room must belong to it.
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")

        if target_lang:
            stmt = (
                select(TranscriptTranslation)
                .join(TranscriptSegment)
                .where(
                    TranscriptSegment.room_id == room_id,
                    TranscriptSegment.language_code == language_code,
                    TranscriptTranslation.language_code == target_lang,
                )
                .order_by(TranscriptSegment.created_at)
            )
            result = await session.execute(stmt)
            translations = result.scalars().all()
            return [{"id": t.id, "text": t.text, "created_at": t.created_at.isoformat()} for t in translations]
        else:
            stmt = (
                select(TranscriptSegment)
                .where(TranscriptSegment.room_id == room_id, TranscriptSegment.language_code == language_code)
                .order_by(TranscriptSegment.created_at)
            )
            result = await session.execute(stmt)
            segments = result.scalars().all()
            return [{"id": s.id, "text": s.text, "created_at": s.created_at.isoformat()} for s in segments]


@router.post("/admin/models/trigger_download", dependencies=[Depends(require_event_owner)])
async def api_trigger_download(request: Request):
    from portal.translations.providers.local import trigger_download

    data = await request.json()
    model = data.get("model", "nllb-200-distilled-600M")

    from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum

    supported_local_models = TRANSLATION_MODELS.get(TranslationProviderEnum.LOCAL.value, [])

    if model not in supported_local_models:
        raise HTTPException(status_code=400, detail=f"Unsupported local model: {model}")

    import asyncio

    asyncio.get_running_loop().run_in_executor(None, trigger_download, model)
    return {"status": "started"}


@router.get("/admin/models/download_progress", dependencies=[Depends(require_event_owner)])
async def api_download_progress(model: str = Query("nllb-200-distilled-600M")):
    from portal.translations.providers.local import get_download_progress

    progress = get_download_progress(model)
    return progress


@router.post("/admin/models/supertonic/trigger_download", dependencies=[Depends(require_event_owner)])
async def api_supertonic_trigger_download():
    from portal.tts.providers.supertonic import trigger_supertonic_download

    trigger_supertonic_download()
    return {"status": "started"}


@router.get("/admin/models/supertonic/download_progress", dependencies=[Depends(require_event_owner)])
async def api_supertonic_download_progress():
    from portal.tts.providers.supertonic import get_supertonic_download_progress

    progress = get_supertonic_download_progress()
    return progress


# ---------------------------------------------------------------------------
# Developer Accounts
# ---------------------------------------------------------------------------


@router.get("/admin/developer-accounts", include_in_schema=False, dependencies=[Depends(require_admin)])
async def admin_developer_accounts(
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    from sqlalchemy.orm import selectinload

    from portal.models import DeveloperAccount

    result = await db.execute(
        select(DeveloperAccount)
        .options(selectinload(DeveloperAccount.user))
        .order_by(DeveloperAccount.created_at.desc())
    )
    accounts = result.scalars().all()

    from portal.auth import get_access_flags

    admin_flags = await get_access_flags(request)

    return templates.TemplateResponse(
        request=request,
        name="admin/developer_accounts.html",
        context={
            "user": user,
            "accounts": accounts,
            **admin_flags,
        },
    )


@router.post(
    "/api/admin/developer-accounts/{account_id}/approve",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)
async def admin_developer_approve(
    account_id: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    from datetime import datetime, timezone

    from portal.models import DeveloperAccount

    account = await db.get(DeveloperAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    account.status = "approved"
    account.reviewed_by = int(user["sub"])
    account.reviewed_at = datetime.now(timezone.utc)
    await db.flush()
    account_user = await get_user_by_id(db, account.user_id)
    if account_user is None:
        raise HTTPException(status_code=404, detail="Account user not found")
    await queue_notification(
        db,
        "developer_account",
        account_user.email,
        {"message": "Your developer account has been approved.", "login_url": f"{settings.public_base_url.rstrip('/')}/developer"},
        idempotency_key=f"developer-account:{account.id}:approved",
    )

    return RedirectResponse(url="/admin/developer-accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/api/admin/developer-accounts/{account_id}/reject",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)
async def admin_developer_reject(
    account_id: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    from datetime import datetime, timezone

    from portal.models import DeveloperAccount

    account = await db.get(DeveloperAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    account.status = "rejected"
    account.reviewed_by = int(user["sub"])
    account.reviewed_at = datetime.now(timezone.utc)
    await db.flush()
    account_user = await get_user_by_id(db, account.user_id)
    if account_user is None:
        raise HTTPException(status_code=404, detail="Account user not found")
    await queue_notification(
        db,
        "developer_account",
        account_user.email,
        {"message": "Your developer account request was not approved.", "login_url": f"{settings.public_base_url.rstrip('/')}/developer"},
        idempotency_key=f"developer-account:{account.id}:rejected",
    )

    return RedirectResponse(url="/admin/developer-accounts", status_code=status.HTTP_303_SEE_OTHER)
