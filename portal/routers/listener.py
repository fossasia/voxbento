from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.templating import Jinja2Templates

from portal.auth import decode_token, get_booth_session
from portal.config import settings
from portal.database import (
    get_event_by_slug,
    get_room_by_id,
    get_session,
    list_booths_for_event,
    list_rooms_for_event,
)
from portal.globals import _JS_CACHE_BUST
from portal.utils import _ensure_mediamtx_path

# Allowlists for embed theming params — no free-text values accepted.
_ALLOWED_THEMES = {"dark", "light"}
_ALLOWED_FONTS = {"inter", "roboto", "outfit"}
_PRIMARY_COLOR_RE = re.compile(r"^[0-9a-fA-F]{3,6}$")
_DEFAULT_PRIMARY = "3b82f6"  # VoxBento blue

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()

_JOIN_CODE_RATE_LIMIT = 10
_JOIN_CODE_RATE_WINDOW_SECONDS = 60
_join_code_attempts: dict[str, tuple[int, float]] = {}


def _register_failed_attempt(client_ip: str) -> bool:
    """Record a failed join-code attempt for `client_ip`.

    Returns True if the client has exceeded the allowed attempts within the window.
    """
    now = time.monotonic()
    count, window_start = _join_code_attempts.get(client_ip, (0, now))
    if now - window_start > _JOIN_CODE_RATE_WINDOW_SECONDS:
        count, window_start = 0, now
    count += 1
    _join_code_attempts[client_ip] = (count, window_start)
    return count > _JOIN_CODE_RATE_LIMIT


def _reset_attempts(client_ip: str) -> None:
    _join_code_attempts.pop(client_ip, None)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def has_listener_access(request: Request, event_slug: str, listener_join_code: str | None, code: str | None) -> bool:
    payload = get_booth_session(request)
    if payload and payload.get("user"):
        return True

    cookie_code = request.cookies.get(f"listener_code_{event_slug}")
    active_code = code or cookie_code
    if bool(listener_join_code and active_code == listener_join_code):
        _reset_attempts(_client_ip(request))
        return True
    return False


@router.get("/listener/{event_slug}")
async def listen_event_page(request: Request, event_slug: str, code: str | None = None) -> Any:
    """Listener page scoped by event, allowing users to select room and language."""
    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

        if not has_listener_access(request, event_slug, ev.listener_join_code, code):
            if _register_failed_attempt(_client_ip(request)):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many join attempts. Please try again later.",
                )
            return templates.TemplateResponse(
                request, "listener_join.html", {"event": ev, "error": "Invalid join code." if code else None}
            )

        rooms = await list_rooms_for_event(session, ev.id)
        db_booths = await list_booths_for_event(session, ev.id)

    booths_data = []
    ensure_tasks = []
    for b in db_booths:
        channel_id = b.mediamtx_path
        booth_lang_data = [
            {"code": lang.language_code, "name": lang.language_name} for lang in b.translation_languages if lang.enabled
        ]
        booths_data.append(
            {
                "id": b.id,
                "room_id": b.room_id,
                "language_code": b.language_code,
                "language_name": b.language_name,
                "channel_id": channel_id,
                "whep_url": f"{settings.mediamtx_whip_base}/{channel_id}/whep",
                "audio_delay_ms": b.room.audio_delay_ms,
                "translation_enabled": getattr(b, "translation_enabled", False),
                "translation_languages": booth_lang_data,
            }
        )
        ensure_tasks.append(_ensure_mediamtx_path(channel_id))

    rooms_data = []
    for r in rooms:
        lang_data = [
            {"code": lang.language_code, "name": lang.language_name} for lang in r.translation_languages if lang.enabled
        ]
        rooms_data.append(
            {
                "id": r.id,
                "audio_delay_ms": r.audio_delay_ms,
                "floor_translation_enabled": r.floor_translation_enabled,
                "floor_tts_enabled": r.floor_tts_enabled,
                "translation_languages": lang_data,
            }
        )

        if r.floor_transcription_enabled:
            from portal.booth_identity import make_mediamtx_path

            channel_id = make_mediamtx_path(ev.slug, r.id, "floor")
            booths_data.append(
                {
                    "id": f"floor_{r.id}",
                    "room_id": r.id,
                    "language_code": "floor",
                    "language_name": "Floor Audio (Original)",
                    "channel_id": channel_id,
                    "whep_url": f"{settings.mediamtx_whip_base}/{channel_id}/whep",
                    "audio_delay_ms": r.audio_delay_ms,
                    "translation_enabled": r.floor_translation_enabled,
                    "translation_languages": lang_data,
                }
            )
            ensure_tasks.append(_ensure_mediamtx_path(channel_id))

    if ensure_tasks:
        await asyncio.gather(*ensure_tasks)

    response = templates.TemplateResponse(
        request,
        "listener-event.html",
        {
            "event": ev,
            "rooms": rooms,
            "rooms_json": json.dumps(rooms_data),
            "booths_json": json.dumps(booths_data),
            "js_version": _JS_CACHE_BUST,
        },
    )
    if code and code == ev.listener_join_code:
        response.set_cookie(f"listener_code_{event_slug}", code, httponly=True, max_age=31536000)
    return response


@router.get("/listener/{event_slug}/rooms/{room_id}/audio-delay")
async def listener_room_audio_delay(
    request: Request, event_slug: str, room_id: int, code: str | None = None
) -> dict[str, int]:
    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        if not has_listener_access(request, event_slug, ev.listener_join_code, code):
            if _register_failed_attempt(_client_ip(request)):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many join attempts. Please try again later.",
                )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Listener access required")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != ev.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
        return {"audio_delay_ms": room.audio_delay_ms}


async def _embed_listener_impl(
    request: Request,
    event_slug: str,
    language_code: str,
    room_id: int | None,
    token: str,
    theme: str,
    primary_color: str,
    font: str,
    captions: bool,
    custom_css_url: str | None,
    headless: bool,
    target_lang: str | None,
):
    # ── Authentication ────────────────────────────────────────────────────
    if not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing embed token.")

    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Embed token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid embed token.")

    # Explicit claim checks — .get() only, never bare key access
    if payload.get("role") != "listener":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token is not a listener token.")
    if payload.get("purpose") != "embed":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token must be an embed token.")
    if payload.get("event_slug") != event_slug:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token is not valid for this event.")

    # ── Resolve booth ─────────────────────────────────────────────────────
    from portal.booth_identity import make_booth_id, make_mediamtx_path
    from portal.database import list_booths_for_event, list_rooms_for_event

    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")

        rooms = await list_rooms_for_event(session, ev.id)
        if room_id is None:
            if not rooms:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No rooms found for this event.")
            if len(rooms) > 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This event has multiple rooms. Please use the /embed/{event_slug}/{room_id}/{language_code} URL instead.",
                )
            resolved_room_id = rooms[0].id
            audio_delay_ms = rooms[0].audio_delay_ms
        else:
            resolved_room_id = room_id
            room = next((r for r in rooms if r.id == resolved_room_id), None)
            if not room:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
            audio_delay_ms = room.audio_delay_ms

        try:
            booth_id = make_booth_id(ev.slug, resolved_room_id, language_code.lower())
            channel_id = make_mediamtx_path(ev.slug, resolved_room_id, language_code.lower())
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid language code.")

        if language_code.lower() != "floor":
            db_booths = await list_booths_for_event(session, ev.id)
            booth = next(
                (
                    b
                    for b in db_booths
                    if b.language_code.lower() == language_code.lower() and b.room_id == resolved_room_id
                ),
                None,
            )
            if booth is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No booth for language '{language_code}' in event '{event_slug}'.",
                )

    whep_url = f"{settings.mediamtx_whip_base}/{channel_id}/whep"
    host = settings.public_base_url.replace("https://", "").replace("http://", "")
    caption_url = f"wss://{host}/ws/captions/{booth_id}"

    # ── Sanitize theming params ───────────────────────────────────────────
    safe_theme = theme if theme in _ALLOWED_THEMES else "dark"
    safe_font = font if font in _ALLOWED_FONTS else "inter"
    safe_primary = primary_color if _PRIMARY_COLOR_RE.match(primary_color) else _DEFAULT_PRIMARY

    safe_custom_css = None
    if custom_css_url and custom_css_url.startswith("https://"):
        safe_custom_css = custom_css_url

    # ── Security headers ─────────────────────────────────────────────────
    allowed_origins_list = [o.strip() for o in settings.embed_allowed_origins.split(",") if o.strip()]

    if allowed_origins_list:
        origins = " ".join(allowed_origins_list)
        frame_ancestors = f"frame-ancestors {origins}"
    else:
        frame_ancestors = "frame-ancestors *"

    if len(allowed_origins_list) == 1:
        postmessage_target_origin = allowed_origins_list[0]
    else:
        postmessage_target_origin = "*"

    response_headers = {
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": frame_ancestors,
    }

    context = {
        "event_slug": event_slug,
        "language_code": language_code,
        "whep_url": whep_url,
        "caption_url": caption_url,
        "token": token,
        "theme": safe_theme,
        "primary_color": safe_primary,
        "font_family": safe_font.capitalize(),
        "captions_enabled": captions,
        "custom_css_url": safe_custom_css,
        "target_lang_code": target_lang.lower() if target_lang else language_code.lower(),
        "js_version": _JS_CACHE_BUST,
        "headless": headless,
        "postmessage_target_origin": postmessage_target_origin,
        "allowed_origins_list": allowed_origins_list,
        "audio_delay_ms": audio_delay_ms,
    }

    return templates.TemplateResponse(request, "embed.html", context, headers=response_headers)


@router.get("/embed/{event_slug}/{language_code}")
async def embed_listener_legacy(
    request: Request,
    event_slug: str,
    language_code: str,
    token: str = Query(""),
    theme: str = Query("dark"),
    primary_color: str = Query(_DEFAULT_PRIMARY, alias="primaryColor"),
    font: str = Query("inter"),
    captions: bool = Query(False),
    custom_css_url: str | None = Query(None, alias="customCssUrl"),
    headless: bool = Query(False),
    target_lang: str | None = Query(None, alias="targetLang"),
):
    """Serve a standalone, iframe-safe listener embed."""
    return await _embed_listener_impl(
        request,
        event_slug,
        language_code,
        None,
        token,
        theme,
        primary_color,
        font,
        captions,
        custom_css_url,
        headless,
        target_lang,
    )


@router.get("/embed/{event_slug}/{room_id}/{language_code}")
async def embed_listener_scoped(
    request: Request,
    event_slug: str,
    room_id: int,
    language_code: str,
    token: str = Query(""),
    theme: str = Query("dark"),
    primary_color: str = Query(_DEFAULT_PRIMARY, alias="primaryColor"),
    font: str = Query("inter"),
    captions: bool = Query(False),
    custom_css_url: str | None = Query(None, alias="customCssUrl"),
    headless: bool = Query(False),
    target_lang: str | None = Query(None, alias="targetLang"),
):
    """Serve a standalone, iframe-safe listener embed (scoped to a room)."""
    return await _embed_listener_impl(
        request,
        event_slug,
        language_code,
        room_id,
        token,
        theme,
        primary_color,
        font,
        captions,
        custom_css_url,
        headless,
        target_lang,
    )
