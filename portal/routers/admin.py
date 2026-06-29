import asyncio
import logging
import math
import re
import secrets
import urllib.parse
from datetime import timedelta
from pathlib import Path

import httpx
import pycountry
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import update

from portal.auth import (
    create_admin_token,
    get_accessible_event_ids,
    get_admin_flags,
    get_current_user,
    require_admin,
    require_super_admin,
    require_user,
)
from portal.booth_identity import make_booth_id, make_mediamtx_path, validate_language_code
from portal.config import settings
from portal.crypto import encrypt_val
from portal.database import (
    count_events,
    count_users,
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    delete_booth,
    delete_event,
    delete_room,
    delete_user,
    get_booth_by_id,
    get_event_by_id,
    get_event_by_slug,
    get_room_by_id,
    get_session,
    get_user_by_email,
    get_user_by_id,
    list_all_booths_for_events,
    list_booths_for_event,
    list_booths_for_room,
    list_events,
    list_memberships_for_booth,
    list_memberships_for_event,
    list_memberships_for_room,
    list_memberships_for_user,
    list_room_memberships_for_user,
    list_rooms_for_event,
    list_tokens_for_booth,
    list_users,
    remove_booth_membership,
    remove_event_membership,
    remove_room_membership,
    revoke_invite_token,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
    update_user_active,
)
from portal.globals import _JS_CACHE_BUST, booths
from portal.models import (
    BoothTranslationLanguage,
    RoomTranslationLanguage,
    TranscriptSegment,
    TranscriptTranslation,
    User,
    utc_now,
)
from portal.transcription import ALLOWED_MODELS, ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.utils import _check_mediamtx, _make_jitsi_url, safe_redirect
from portal.websockets.manager import broadcast_transcription

_BASE_DIR = Path(__file__).resolve().parent.parent.parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()


@router.get("/mission-control/")
async def mission_control_list(request: Request, user=Depends(require_user), page: int = 1):
    is_super_admin, allowed_event_ids = await get_accessible_event_ids(request, user_id=int(user["sub"]))
    async with get_session() as session:
        limit = 20
        offset = (page - 1) * limit
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        accessible_events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)
    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(
        request,
        "mission_control/event_list.html",
        {
            "events": accessible_events,
            "is_super_admin": is_super_admin,
            "active_nav": "mission-control",
            "page": page,
            "total_pages": total_pages,
        },
    )


@router.get("/mission-control/{event_slug}/")
async def mission_control_grid(request: Request, event_slug: str, user=Depends(require_user)):
    is_super_admin, _ = await get_accessible_event_ids(request, user_id=int(user["sub"]))
    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        allowed_room_ids = None
        if not is_super_admin:
            memberships = await list_memberships_for_user(session, int(user["sub"]))
            room_memberships = await list_room_memberships_for_user(session, int(user["sub"]))
            event_owner_ids = {m.event_id for m in memberships if m.role == "event_owner"}
            coord_room_ids = {rm.room_id for rm in room_memberships if rm.role == "room_coordinator"}
            if event.id not in event_owner_ids:
                rooms = await list_rooms_for_event(session, event.id)
                event_room_ids = {r.id for r in rooms}
                if not coord_room_ids.intersection(event_room_ids):
                    raise HTTPException(
                        status_code=403, detail="Access denied. Event Owner or Room Coordinator required."
                    )
                allowed_room_ids = coord_room_ids
        db_booths = await list_booths_for_event(session, event.id)
        event_booths = []
        for db_b in db_booths:
            if allowed_room_ids is not None and db_b.room_id not in allowed_room_ids:
                continue
            booth_id = make_booth_id(event.slug, db_b.language_code)
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
                    "broadcast_unlocked": db_b.broadcast_unlocked,
                    "ingest_status": "disconnected",
                    "participants": [],
                    "chat_messages": [],
                }
            state["room_name"] = db_b.room.display_name if db_b.room else "Unknown Room"
            event_booths.append(state)
    return templates.TemplateResponse(
        request,
        "mission_control/grid.html",
        {
            "event": event,
            "booths": event_booths,
            "whip_base": settings.mediamtx_whip_base,
            "js_version": _JS_CACHE_BUST,
            "active_nav": "mission-control",
        },
    )


@router.get("/admin/login")
async def admin_login_page(request: Request):
    user = await get_current_user(request)
    if user and user.get("is_admin"):
        return safe_redirect(url="/admin/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "admin/login.html", {})


@router.post("/admin/login")
async def admin_login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if not settings.admin_password or password != settings.admin_password:
        return templates.TemplateResponse(
            request, "admin/login.html", {"error": "Invalid password."}, status_code=status.HTTP_403_FORBIDDEN
        )
    token = create_admin_token()
    response = safe_redirect(url="/admin/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="admin_token", value=token, httponly=True, samesite="lax", max_age=settings.jwt_expiry_seconds
    )
    return response


@router.get("/admin/logout")
async def admin_logout():
    response = safe_redirect(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("admin_token")
    response.delete_cookie("user_token")
    response.delete_cookie("session_token")
    return response


@router.get("/admin/", dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request, page: int = 1):
    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user["sub"]) if user and user.get("sub") else None
    _, allowed_event_ids = await get_accessible_event_ids(request, user_id=user_id)
    limit = 20
    offset = (page - 1) * limit
    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)
        if not admin_flags.get("is_super_admin") and user:
            if len(events) == 1 and total_events == 1:
                return safe_redirect(url=f"/admin/events/{events[0].id}/", status_code=status.HTTP_303_SEE_OTHER)
        event_ids = [ev.id for ev in events]
        booths_by_event = await list_all_booths_for_events(session, event_ids)
        event_data = []
        for ev in events:
            db_booths = booths_by_event.get(ev.id, [])
            booth_statuses = []
            for b in db_booths:
                booth_id = make_booth_id(ev.slug, b.language_code)
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
        request,
        "admin/dashboard.html",
        {"event_data": event_data, "mediamtx_ok": mediamtx_ok, "page": page, "total_pages": total_pages, **admin_flags},
    )


@router.get("/admin/events/", dependencies=[Depends(require_admin)])
async def admin_event_list(request: Request, page: int = 1):
    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user["sub"]) if user and user.get("sub") else None
    _, allowed_event_ids = await get_accessible_event_ids(request, user_id=user_id)
    limit = 20
    offset = (page - 1) * limit
    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)
    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(
        request, "admin/event_list.html", {"events": events, "page": page, "total_pages": total_pages, **admin_flags}
    )


@router.post("/admin/events/", dependencies=[Depends(require_admin)])
async def admin_create_event(request: Request):
    form = await request.form()
    slug = form.get("slug", "").strip()
    display_name = form.get("display_name", "").strip()
    if not slug or not display_name:
        return safe_redirect(url="/admin/events/", status_code=status.HTTP_303_SEE_OTHER)
    try:
        async with get_session() as session:
            await create_event(session, slug=slug, display_name=display_name)
    except Exception:
        return safe_redirect(url="/admin/events/", status_code=status.HTTP_303_SEE_OTHER)
    return safe_redirect(url="/admin/events/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/events/{event_id}/regenerate_join_code/", dependencies=[Depends(require_admin)])
async def admin_regenerate_join_code(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        event.listener_join_code = "".join((secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)))
        await session.commit()
    return safe_redirect(url=f"/admin/events/{event_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/", dependencies=[Depends(require_admin)])
async def admin_event_detail(request: Request, event_id: int):
    admin_flags = await get_admin_flags(request, event_id=event_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        rooms = await list_rooms_for_event(session, event_id)
        db_booths = await list_booths_for_event(session, event_id)
    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    return templates.TemplateResponse(
        request,
        "admin/event_detail.html",
        {
            "event": event,
            "rooms": rooms,
            "booths": booth_statuses,
            "live_count": sum((1 for bs in booth_statuses if bs["is_live"])),
            **admin_flags,
        },
    )


@router.get("/admin/events/{event_id}/api-settings/", dependencies=[Depends(require_admin)])
async def admin_event_api_settings_get(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
    admin_flags = await get_admin_flags(request, event_id=event_id)
    return templates.TemplateResponse(request, "admin/api_settings.html", {"event": event, **admin_flags})


@router.post("/admin/events/{event_id}/api-settings", dependencies=[Depends(require_admin)])
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
            if clear_openai_api_key:
                event.encrypted_openai_api_key = None
            elif openai_api_key and openai_api_key.strip():
                event.encrypted_openai_api_key = encrypt_val(openai_api_key.strip())
            if clear_deepgram_api_key:
                event.encrypted_deepgram_api_key = None
            elif deepgram_api_key and deepgram_api_key.strip():
                event.encrypted_deepgram_api_key = encrypt_val(deepgram_api_key.strip())
            if clear_nvidia_api_key:
                event.encrypted_nvidia_api_key = None
            elif nvidia_api_key and nvidia_api_key.strip():
                event.encrypted_nvidia_api_key = encrypt_val(nvidia_api_key.strip())
            if clear_elevenlabs_api_key:
                event.encrypted_elevenlabs_api_key = None
            elif elevenlabs_api_key and elevenlabs_api_key.strip():
                event.encrypted_elevenlabs_api_key = encrypt_val(elevenlabs_api_key.strip())
            if clear_translation_openai_api_key:
                event.encrypted_translation_openai_api_key = None
            elif translation_openai_api_key and translation_openai_api_key.strip():
                event.encrypted_translation_openai_api_key = encrypt_val(translation_openai_api_key.strip())
            if clear_openrouter_api_key:
                event.encrypted_openrouter_api_key = None
            elif openrouter_api_key and openrouter_api_key.strip():
                event.encrypted_openrouter_api_key = encrypt_val(openrouter_api_key.strip())
            if clear_gemini_api_key:
                event.encrypted_gemini_api_key = None
            elif gemini_api_key and gemini_api_key.strip():
                event.encrypted_gemini_api_key = encrypt_val(gemini_api_key.strip())
            if clear_anthropic_api_key:
                event.encrypted_anthropic_api_key = None
            elif anthropic_api_key and anthropic_api_key.strip():
                event.encrypted_anthropic_api_key = encrypt_val(anthropic_api_key.strip())
            if clear_groq_api_key:
                event.encrypted_groq_api_key = None
            elif groq_api_key and groq_api_key.strip():
                event.encrypted_groq_api_key = encrypt_val(groq_api_key.strip())
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=f"API Key encryption failed: {e}")
        await session.commit()
    return safe_redirect(url=f"/admin/events/{event_id}/api-settings/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/events/{event_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_event(request: Request, event_id: int):
    async with get_session() as session:
        await delete_event(session, event_id)
    return safe_redirect(url="/admin/events/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/", dependencies=[Depends(require_admin)])
async def admin_room_list(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        rooms = await list_rooms_for_event(session, event_id)
        room_data = []
        for room in rooms:
            room_booths = await list_booths_for_room(session, room.id)
            room_data.append({"room": room, "booth_count": len(room_booths)})
    return templates.TemplateResponse(request, "admin/room_list.html", {"event": event, "room_data": room_data})


@router.post("/admin/events/{event_id}/rooms/", dependencies=[Depends(require_admin)])
async def admin_create_room(request: Request, event_id: int):
    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if not display_name:
        return safe_redirect(url=f"/admin/events/{event_id}/rooms/", status_code=status.HTTP_303_SEE_OTHER)
    async with get_session() as session:
        ev = await get_event_by_id(session, event_id)
        jitsi_url = None
        if ev:
            clean_name = re.sub("[^a-zA-Z0-9]+", "", display_name)
            room_id_str = f"Voxbento-{ev.slug}-{clean_name}"
            jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)
        await create_room(session, event_id=event_id, display_name=display_name, jitsi_url=jitsi_url)
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/{room_id}/", dependencies=[Depends(require_admin)])
async def admin_room_detail(request: Request, event_id: int, room_id: int):
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)
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
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    clean_name = re.sub("[^a-zA-Z0-9]+", "", room.display_name)
    room_id_str = f"Voxbento-{event.slug}-{clean_name}"
    fallback_jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)
    translation_languages_dataset = [
        {"code": lang.alpha_2, "name": lang.name} for lang in pycountry.languages if hasattr(lang, "alpha_2")
    ]
    translation_languages_dataset.sort(key=lambda x: x["name"])
    enabled_translation_language_codes = [lang.language_code for lang in room.translation_languages if lang.enabled]
    async with get_session() as session:
        memberships = await list_memberships_for_room(session, room_id)
    return templates.TemplateResponse(
        request,
        "admin/room_detail.html",
        {
            "event": event,
            "room": room,
            "booths": booth_statuses,
            "fallback_jitsi_url": fallback_jitsi_url,
            "translation_languages_dataset": translation_languages_dataset,
            "enabled_translation_language_codes": enabled_translation_language_codes,
            "memberships": memberships,
            **admin_flags,
        },
    )


@router.get("/admin/events/{event_id}/rooms/{room_id}/transcripts/", dependencies=[Depends(require_admin)])
async def admin_room_transcripts(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        booths = await list_booths_for_room(session, room_id)
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)
    return templates.TemplateResponse(
        request, "admin/room_transcripts.html", {"event": event, "room": room, "booths": booths, **admin_flags}
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/edit", dependencies=[Depends(require_admin)])
async def admin_edit_room(request: Request, event_id: int, room_id: int):
    form = await request.form()
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
    floor_tts_enabled = form.get("floor_tts_enabled") == "on"
    floor_tts_provider = (form.get("floor_tts_provider", "deepgram") or "deepgram").strip().lower() or "deepgram"
    if floor_tts_provider not in {"deepgram", "supertonic"}:
        floor_tts_provider = "deepgram"
    floor_tts_voice = (form.get("floor_tts_voice", "M1") or "M1").strip().upper() or "M1"
    if floor_tts_voice not in {"M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"}:
        floor_tts_voice = "M1"
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if room and room.event_id == event_id:
            if display_name:
                room.display_name = display_name
            room.jitsi_url = jitsi_url if jitsi_url else None
            room.relay_booth_id = relay_booth_id
            room.audio_delay_ms = audio_delay_ms
            room.floor_transcription_enabled = floor_transcription_enabled
            room.floor_transcription_provider = floor_transcription_provider
            room.floor_transcription_model = floor_transcription_model
            room.floor_language_code = floor_language_code
            room.floor_translation_enabled = floor_translation_enabled
            room.floor_translation_provider = floor_translation_provider
            room.floor_translation_model = floor_translation_model
            room.floor_tts_enabled = floor_tts_enabled
            room.floor_tts_provider = floor_tts_provider
            room.floor_tts_voice = floor_tts_voice
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
            await session.commit()
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/admin/providers/translation/models", dependencies=[Depends(require_admin)])
async def get_translation_models():
    return TRANSLATION_MODELS


logger = logging.getLogger(__name__)
MAX_AUDIO_DELAY_MS = 10_000


def parse_audio_delay_ms(value: object) -> int:
    try:
        delay_ms = int(str(value or "0").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Audio synchronization delay must be an integer.")
    if delay_ms < 0 or delay_ms > MAX_AUDIO_DELAY_MS:
        raise HTTPException(status_code=400, detail="Audio synchronization delay must be between 0 and 10000 ms.")
    return delay_ms


@router.post("/api/rooms/{room_id}/floor-transcription/start", dependencies=[Depends(require_admin)])
async def api_start_floor_transcription(room_id: int):
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room or not room.floor_transcription_enabled:
            raise HTTPException(status_code=400, detail="Floor transcription not enabled or invalid room")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")
        event_slug = event.slug
        clean_name = re.sub("[^a-zA-Z0-9]+", "", room.display_name)
        room_id_str = f"Voxbento-{event.slug}-{clean_name}"
        if room.jitsi_url:
            jitsi_url = room.jitsi_url
            parsed = urllib.parse.urlparse(room.jitsi_url)
            internal_parsed = urllib.parse.urlparse(settings.effective_jitsi_internal_base)
            base_parsed = urllib.parse.urlparse(settings.effective_jitsi_base_url)
            if parsed.netloc in ("jitsi.voxbento.com", base_parsed.netloc) or parsed.netloc.startswith(
                ("localhost", "127.0.0.1")
            ):
                parsed = parsed._replace(scheme=internal_parsed.scheme, netloc=internal_parsed.netloc)
                jitsi_url = urllib.parse.urlunparse(parsed)
            else:
                jitsi_url = room.jitsi_url
        else:
            jitsi_url = f"{settings.effective_jitsi_internal_base}/{room_id_str}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.floor_bot_base}/start",
                json={
                    "event_slug": event_slug,
                    "jitsi_url": jitsi_url,
                    "mediamtx_rtsp_base": settings.mediamtx_rtsp_base,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to start floor-bot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start floor bot: {e}")
    try:
        api_key = get_api_key(event, ProviderEnum(room.floor_transcription_provider))
        config = ProviderConfig(api_key=api_key)
        await start_transcription_worker(
            event_slug=event_slug,
            language_code="floor",
            booth_id=f"{event_slug}-floor",
            broadcast_callback=broadcast_transcription,
            provider=room.floor_transcription_provider,
            model_size=room.floor_transcription_model,
            config=config,
            transcription_language=room.floor_language_code,
            room_id=room_id,
        )
    except Exception as e:
        logger.error(f"Failed to start transcription worker: {e}")
        async with httpx.AsyncClient() as client:
            await client.post(f"{settings.floor_bot_base}/stop", json={"event_slug": event_slug})
        raise HTTPException(status_code=500, detail=f"Failed to start transcription worker: {e}")
    return {"status": "started"}


@router.post("/api/rooms/{room_id}/floor-transcription/stop", dependencies=[Depends(require_admin)])
async def api_stop_floor_transcription(room_id: int):
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room:
            raise HTTPException(status_code=400, detail="Invalid room")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")
        event_slug = event.slug
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{settings.floor_bot_base}/stop", json={"event_slug": event_slug}, timeout=5.0)
    except Exception as e:
        logger.error(f"Failed to stop floor-bot: {e}")
    await stop_transcription_worker(f"{event_slug}-floor")
    return {"status": "stopped"}


@router.post("/admin/events/{event_id}/rooms/{room_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_room(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        await delete_room(session, room_id)
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/{room_id}/booths/", dependencies=[Depends(require_admin)])
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
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
        booth_statuses.append({"db": b, "booth_id": bid, "is_live": is_live})
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)
    return templates.TemplateResponse(
        request, "admin/booth_list.html", {"event": event, "room": room, "booths": booth_statuses, **admin_flags}
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/", dependencies=[Depends(require_admin)])
async def admin_create_booth(request: Request, event_id: int, room_id: int):
    form = await request.form()
    language_code = form.get("language_code", "").strip().lower()
    language_name = form.get("language_name", "").strip()
    if not language_code or not language_name:
        return safe_redirect(
            url=f"/admin/events/{event_id}/rooms/{room_id}/booths/", status_code=status.HTTP_303_SEE_OTHER
        )
    try:
        async with get_session() as session:
            await create_booth(
                session, event_id=event_id, room_id=room_id, language_code=language_code, language_name=language_name
            )
    except Exception as e:
        logger.warning(f"Error creating booth: {e}")
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/booths/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", dependencies=[Depends(require_admin)])
async def admin_booth_detail(request: Request, event_id: int, room_id: int, booth_id: int):
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail="Room not found.")
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail="Booth not found.")
        tokens = await list_tokens_for_booth(session, booth_id)
        users = await list_users(session)
        memberships = await list_memberships_for_booth(session, booth_id)
    membership_map = {m.user_id: m for m in memberships}
    bid = make_booth_id(event.slug, db_booth.language_code)
    mediamtx_path = make_mediamtx_path(event.slug, db_booth.language_code)
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
        request,
        "admin/booth_detail.html",
        {
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


@router.post("/admin/events/{event_id}/rooms/{room_id}/members/", dependencies=[Depends(require_admin)])
async def admin_add_room_member(request: Request, event_id: int, room_id: int):
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f"/admin/events/{event_id}/rooms/{room_id}/?error=user_not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                try:
                    await set_room_membership(session, user_id=uid, room_id=room_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f"/admin/events/{event_id}/rooms/{room_id}/?error=invalid_role",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_room(session, room_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_room_membership(session, m.id)
                        break
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/members/{membership_id}/delete", dependencies=[Depends(require_admin)]
)
async def admin_remove_room_member(request: Request, event_id: int, room_id: int, membership_id: int):
    async with get_session() as session:
        await remove_room_membership(session, membership_id)
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/", dependencies=[Depends(require_admin)]
)
async def admin_add_booth_member(request: Request, event_id: int, room_id: int, booth_id: int):
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=user_not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                try:
                    await set_booth_membership(session, user_id=uid, booth_id=booth_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=invalid_role",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_booth(session, booth_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_booth_membership(session, m.id)
                        break
    return safe_redirect(
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/{membership_id}/delete",
    dependencies=[Depends(require_admin)],
)
async def admin_remove_booth_member(request: Request, event_id: int, room_id: int, booth_id: int, membership_id: int):
    async with get_session() as session:
        await remove_booth_membership(session, membership_id)
    return safe_redirect(
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    async with get_session() as session:
        await delete_booth(session, booth_id)
    return safe_redirect(url=f"/admin/events/{event_id}/rooms/{room_id}/booths/", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
    dependencies=[Depends(require_admin)],
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
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail="Booth not found.")
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
        await session.commit()
    return safe_redirect(
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/edit", dependencies=[Depends(require_admin)])
async def admin_edit_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    form = await request.form()
    language_name = form.get("language_name", "").strip()
    language_code_raw = form.get("language_code", "").strip()
    async with get_session() as session:
        booth = await get_booth_by_id(session, booth_id)
        if booth and booth.event_id == event_id and (booth.room_id == room_id):
            if language_name:
                booth.language_name = language_name
            if language_code_raw:
                try:
                    booth.language_code = validate_language_code(language_code_raw)
                except ValueError:
                    pass
        await session.commit()
    return safe_redirect(
        url=str(request.url_for("admin_booth_detail", event_id=event_id, room_id=room_id, booth_id=booth_id)),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/transcription-settings",
    dependencies=[Depends(require_admin)],
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
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail="Booth not found.")
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
        await session.commit()
        bid = make_booth_id(event.slug, db_booth.language_code)
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
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/users/", dependencies=[Depends(require_super_admin)])
async def admin_user_list(request: Request, page: int = 1):
    limit = 20
    offset = (page - 1) * limit
    async with get_session() as session:
        total_users = await count_users(session)
        users = await list_users(session, limit=limit, offset=offset)
    total_pages = max(1, math.ceil(total_users / limit))
    return templates.TemplateResponse(
        request, "admin/user_list.html", {"users": users, "page": page, "total_pages": total_pages}
    )


@router.post("/admin/users/{user_id}/toggle-active", dependencies=[Depends(require_super_admin)])
async def admin_toggle_user_active(request: Request, user_id: int):
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            await update_user_active(session, user_id, is_active=not user.is_active)
    return safe_redirect(url="/admin/users/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/{user_id}/delete", dependencies=[Depends(require_super_admin)])
async def admin_delete_user(request: Request, user_id: int):
    async with get_session() as session:
        await delete_user(session, user_id)
    return safe_redirect(url="/admin/users/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/users/{user_id}/", dependencies=[Depends(require_super_admin)])
async def admin_user_detail(request: Request, user_id: int):
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if not user:
            return safe_redirect(url="/admin/users/", status_code=status.HTTP_303_SEE_OTHER)
        events = await list_events(session)
        memberships = await list_memberships_for_user(session, user_id)
        event_owner_map = {m.event_id: m for m in memberships if m.role == "event_owner"}
    return templates.TemplateResponse(
        request, "admin/user_detail.html", {"user_detail": user, "events": events, "event_owner_map": event_owner_map}
    )


@router.post("/admin/users/{user_id}/toggle-admin", dependencies=[Depends(require_super_admin)])
async def admin_toggle_user_admin(request: Request, user_id: int):
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            stmt = update(User).where(User.id == user_id).values(is_admin=not user.is_admin)
            await session.execute(stmt)
            await session.commit()
    return safe_redirect(url=f"/admin/users/{user_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/{user_id}/events/{event_id}/toggle-owner", dependencies=[Depends(require_super_admin)])
async def admin_toggle_user_event_owner(request: Request, user_id: int, event_id: int):
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            memberships = await list_memberships_for_user(session, user_id)
            event_owner_membership = next(
                (m for m in memberships if m.event_id == event_id and m.role == "event_owner"), None
            )
            if event_owner_membership:
                await remove_event_membership(session, event_owner_membership.id)
            else:
                await set_event_membership(session, user_id=user_id, event_id=event_id, role="event_owner")
    return safe_redirect(url=f"/admin/users/{user_id}/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/events/{event_id}/members/", dependencies=[Depends(require_admin)])
async def admin_event_members(request: Request, event_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        memberships = await list_memberships_for_event(session, event_id)
        users = await list_users(session)
    membership_map = {m.user_id: m for m in memberships}
    return templates.TemplateResponse(
        request,
        "admin/event_members.html",
        {"event": event, "memberships": memberships, "membership_map": membership_map, "users": users},
    )


@router.post("/admin/events/{event_id}/members/", dependencies=[Depends(require_admin)])
async def admin_add_event_member(request: Request, event_id: int):
    form = await request.form()
    email = form.get("email", "").strip()
    role = form.get("role", "").strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f"/admin/events/{event_id}/members/?error=user_not_found", status_code=status.HTTP_303_SEE_OTHER
                )
            uid = user.id
            if role:
                try:
                    await set_event_membership(session, user_id=uid, event_id=event_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f"/admin/events/{event_id}/members/?error=invalid_role",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_event(session, event_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_event_membership(session, m.id)
                        break
    return safe_redirect(url=f"/admin/events/{event_id}/members/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/events/{event_id}/members/{membership_id}/delete", dependencies=[Depends(require_admin)])
async def admin_remove_event_member(request: Request, event_id: int, membership_id: int):
    async with get_session() as session:
        await remove_event_membership(session, membership_id)
    return safe_redirect(url=f"/admin/events/{event_id}/members/", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/", dependencies=[Depends(require_admin)]
)
async def admin_create_token(request: Request, event_id: int, room_id: int, booth_id: int):
    form = await request.form()
    role = form.get("role", "").strip()
    label = form.get("label", "").strip()
    expires_hours = form.get("expires_hours", "").strip()
    expires_at = None
    if expires_hours:
        try:
            expires_at = utc_now() + timedelta(hours=int(expires_hours))
        except ValueError:
            pass
    if role:
        async with get_session() as session:
            await create_invite_token(session, booth_id=booth_id, role=role, label=label, expires_at=expires_at)
    return safe_redirect(
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post(
    "/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/{token_id}/revoke",
    dependencies=[Depends(require_admin)],
)
async def admin_revoke_token(request: Request, event_id: int, room_id: int, booth_id: int, token_id: str):
    async with get_session() as session:
        await revoke_invite_token(session, token_id)
    return safe_redirect(
        url=f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/api/admin/events/{event_id}/rooms/{room_id}/transcripts/{language_code}")
async def api_admin_get_transcripts(
    event_id: int,
    room_id: int,
    language_code: str,
    target_lang: str = Query(None),
    admin: bool = Depends(require_admin),
):
    from sqlalchemy import select

    from portal.database import get_session

    async with get_session() as session:
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
