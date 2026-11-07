from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.templating import Jinja2Templates
from starlette.responses import JSONResponse

from portal.auth import _current_user, create_listener_token, decode_token, get_booth_session, is_listener_token
from portal.booth_identity import make_booth_id
from portal.config import settings
from portal.database import (
    get_event_by_slug,
    get_room_by_id,
    get_session,
    list_booths_for_event,
    list_booths_for_room,
    list_rooms_for_event,
)
from portal.globals import _JS_CACHE_BUST, asset_version_context
from portal.join_code_limit import JoinCodeThrottled, verify_join_code
from portal.overlay_channels import overlay_channels
from portal.utils import _ensure_mediamtx_path

# Allowlists for embed theming params — no free-text values accepted.
_ALLOWED_THEMES = {"dark", "light"}
_ALLOWED_FONTS = {"inter", "roboto", "outfit"}
_PRIMARY_COLOR_RE = re.compile(r"^[0-9a-fA-F]{3,6}$")
_DEFAULT_PRIMARY = "3b82f6"  # VoxBento blue

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"), context_processors=[asset_version_context])

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _submitted_code(request: Request, event_slug: str, code: str | None) -> str | None:
    """The join code this request submits, from the query parameter or the stored cookie."""
    return code or request.cookies.get(f"listener_code_{event_slug}")


async def has_listener_access(
    request: Request, event_slug: str, listener_join_code: str | None, code: str | None
) -> bool:
    """Authenticate live accounts or a rate-limited event code before issuing access."""
    payload = get_booth_session(request)
    if payload and payload.get("user") and await _current_user(payload):
        return True
    try:
        return verify_join_code(_client_ip(request), _submitted_code(request, event_slug, code), listener_join_code)
    except JoinCodeThrottled as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many join attempts. Please try again later.",
            headers={"Retry-After": str(exc.retry_after), "Cache-Control": "no-store"},
        ) from exc


@router.get("/listener/{event_slug}")
async def listen_event_page(request: Request, event_slug: str, code: str | None = None) -> Any:
    """Listener page scoped by event, allowing users to select room and language."""
    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

        if not await has_listener_access(request, event_slug, ev.listener_join_code, code):
            return templates.TemplateResponse(
                request=request,
                name="listener_join.html",
                context={"event": ev, "error": "Invalid join code." if code else None},
            )

        rooms = await list_rooms_for_event(session, ev.id, limit=None)
        db_booths = await list_booths_for_event(session, ev.id, limit=None)

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
            {"code": lang.language_code, "name": lang.language_name, "tts_enabled": lang.tts_enabled}
            for lang in r.translation_languages
            if lang.enabled
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
            if r.floor_source != "program_ingest":
                ensure_tasks.append(_ensure_mediamtx_path(channel_id))

    if ensure_tasks:
        await asyncio.gather(*ensure_tasks)

    response = templates.TemplateResponse(
        request=request,
        name="listener-event.html",
        context={
            "event": ev,
            "rooms": rooms,
            "rooms_json": json.dumps(rooms_data),
            "booths_json": json.dumps(booths_data),
            "js_version": _JS_CACHE_BUST,
            # Same canonical-base-URL pattern as email.py / admin.py invite links, without the
            # join code so sharing the link doesn't hand out access to the event.
            "share_url": f"{settings.public_base_url}/listener/{ev.slug}",
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
        if not await has_listener_access(request, event_slug, ev.listener_join_code, code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Listener access required")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != ev.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
        return {"audio_delay_ms": room.audio_delay_ms}


@router.get("/overlay/{event_slug}/{room_id}/{language_code}/session")
@router.get("/overlay/{event_slug}/{room_id}/{language_code}")
async def subtitle_overlay(
    request: Request, event_slug: str, room_id: int, language_code: str, code: str | None = None
) -> Any:
    """Transparent caption layer for OBS / vMix browser sources, gated by the listener join code."""
    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        if not await has_listener_access(request, event_slug, ev.listener_join_code, code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Listener access required")
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != ev.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
        channels = overlay_channels([room], await list_booths_for_room(session, room.id))
        channel = next((ch for ch in channels if ch.code == language_code.lower()), None)
        if channel is None:
            raise HTTPException(status_code=404, detail="Caption channel not found")
        booth_id = make_booth_id(ev.slug, room.id, channel.code)

    token = create_listener_token(event_slug=ev.slug)
    if request.url.path.endswith("/session"):
        return JSONResponse(
            {"token": token, "booth_id": booth_id},
            headers={"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"},
        )
    response = templates.TemplateResponse(
        request=request,
        name="overlay.html",
        context={"event": ev, "booth_id": booth_id, "token": token, "language_code": channel.code},
        headers={
            "Cache-Control": "no-store, private",
            "Referrer-Policy": "no-referrer",
            # The workspace previews the overlay in an iframe; nothing else should frame it.
            "Content-Security-Policy": "frame-ancestors 'self'",
        },
    )
    if code and code == ev.listener_join_code:
        response.set_cookie(f"listener_code_{event_slug}", code, httponly=True, max_age=31536000)
    return response


def _verify_embed_token(token: str, event_slug: str) -> None:
    """Validate the embed token and its listener/embed claims for ``event_slug``.

    Raises ``HTTPException(403)`` for a missing, expired, invalid, or mismatched token.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing embed token.")

    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Embed token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid embed token.")

    # Explicit claim checks — .get() only, never bare key access
    if not is_listener_token(payload):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token is not a listener token.")
    if payload.get("purpose") != "embed":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token must be an embed token.")
    if payload.get("event_slug") != event_slug:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token is not valid for this event.")


async def _resolve_embed_room(session: Any, ev: Any, room_id: int | None) -> tuple[int, int]:
    """Resolve the target room id and its audio delay for an embed request."""
    rooms = await list_rooms_for_event(session, ev.id, limit=None)
    if room_id is None:
        if not rooms:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No rooms found for this event.")
        if len(rooms) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This event has multiple rooms. Please use the /embed/{event_slug}/{room_id}/{language_code} URL instead.",
            )
        return rooms[0].id, rooms[0].audio_delay_ms

    room = next((r for r in rooms if r.id == room_id), None)
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
    return room.id, room.audio_delay_ms


async def _resolve_embed_channel(
    session: Any, ev: Any, event_slug: str, room_id: int, language_code: str
) -> tuple[str, str]:
    """Resolve the booth id and mediamtx channel id for the requested language."""
    from portal.booth_identity import make_booth_id, make_mediamtx_path

    try:
        booth_id = make_booth_id(ev.slug, room_id, language_code.lower())
        channel_id = make_mediamtx_path(ev.slug, room_id, language_code.lower())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid language code.")

    if language_code.lower() != "floor":
        db_booths = await list_booths_for_event(session, ev.id, limit=None)
        booth = next(
            (b for b in db_booths if b.language_code.lower() == language_code.lower() and b.room_id == room_id),
            None,
        )
        if booth is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No booth for language '{language_code}' in event '{event_slug}'.",
            )

    return booth_id, channel_id


def _sanitize_embed_theme(
    theme: str, font: str, primary_color: str, custom_css_url: str | None
) -> tuple[str, str, str, str | None]:
    """Sanitize embed theming params: theme, font, and color are allowlisted; custom CSS is accepted from arbitrary HTTPS URLs."""
    safe_theme = theme if theme in _ALLOWED_THEMES else "dark"
    safe_font = font if font in _ALLOWED_FONTS else "inter"
    safe_primary = primary_color if _PRIMARY_COLOR_RE.match(primary_color) else _DEFAULT_PRIMARY

    safe_custom_css = None
    if custom_css_url and custom_css_url.startswith("https://"):
        safe_custom_css = custom_css_url

    return safe_theme, safe_font, safe_primary, safe_custom_css


def _build_embed_frame_policy() -> tuple[str, list[str], str]:
    """Build the frame-ancestors CSP value, allowed origins list, and postMessage target origin."""
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

    return frame_ancestors, allowed_origins_list, postmessage_target_origin


def _build_embed_context(
    event_slug: str,
    language_code: str,
    whep_url: str,
    caption_url: str,
    token: str,
    safe_theme: str,
    safe_primary: str,
    safe_font: str,
    captions: bool,
    safe_custom_css: str | None,
    target_lang: str | None,
    headless: bool,
    postmessage_target_origin: str,
    allowed_origins_list: list[str],
    audio_delay_ms: int,
) -> dict[str, Any]:
    """Assemble the Jinja2 context for the embed template."""
    return {
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
    # Authentication
    _verify_embed_token(token, event_slug)

    # Resolve booth
    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")

        resolved_room_id, audio_delay_ms = await _resolve_embed_room(session, ev, room_id)
        booth_id, channel_id = await _resolve_embed_channel(session, ev, event_slug, resolved_room_id, language_code)

    whep_url = f"{settings.mediamtx_whip_base}/{channel_id}/whep"
    host = settings.public_base_url.replace("https://", "").replace("http://", "")
    caption_url = f"wss://{host}/ws/captions/{booth_id}"

    # Sanitize theming params
    safe_theme, safe_font, safe_primary, safe_custom_css = _sanitize_embed_theme(
        theme, font, primary_color, custom_css_url
    )

    # Security headers
    frame_ancestors, allowed_origins_list, postmessage_target_origin = _build_embed_frame_policy()
    response_headers = {
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": frame_ancestors,
    }

    context = _build_embed_context(
        event_slug=event_slug,
        language_code=language_code,
        whep_url=whep_url,
        caption_url=caption_url,
        token=token,
        safe_theme=safe_theme,
        safe_primary=safe_primary,
        safe_font=safe_font,
        captions=captions,
        safe_custom_css=safe_custom_css,
        target_lang=target_lang,
        headless=headless,
        postmessage_target_origin=postmessage_target_origin,
        allowed_origins_list=allowed_origins_list,
        audio_delay_ms=audio_delay_ms,
    )

    return templates.TemplateResponse(request=request, name="embed.html", context=context, headers=response_headers)


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
