from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from portal.auth import get_booth_session
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
            channel_id = f"{ev.slug}/floor"
            booths_data.append(
                {
                    "id": f"floor_{r.id}",
                    "room_id": r.id,
                    "language_code": "floor",
                    "language_name": "🌍 Floor Audio (Original)",
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
