from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from portal import live_demo
from portal.auth import require_admin, require_admin_csrf
from portal.config import settings
from portal.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/demo/manifest")
async def demo_manifest() -> JSONResponse:
    """Return the pre-baked demo manifest.

    ``status`` is ``"ready"`` when audio files exist, ``"generating"`` when the
    background task is still running, ``"failed"`` if the last generation
    attempt raised, or ``"pending"`` before first generation.
    The frontend polls until status is ``"ready"``.
    """
    from portal.tts.demo_gen import DEMO_VIDEO_URL, load_manifest

    manifest = load_manifest()
    if manifest is not None:
        return JSONResponse({**manifest, "status": "ready"})

    # Not generated yet — check if a generation task is running or failed.
    from portal.tts import demo_gen as dg

    if getattr(dg, "_generating", False):
        status = "generating"
    elif getattr(dg, "_generation_error", None):
        status = "failed"
    else:
        status = "pending"

    return JSONResponse(
        {
            "status": status,
            "video_url": DEMO_VIDEO_URL,
            "languages": [],
        }
    )


@router.post("/admin/demo/regenerate", dependencies=[Depends(require_admin), Depends(require_admin_csrf)])
async def regenerate_demo() -> JSONResponse:
    """Re-generate the demo audio assets (admin only). Runs in the background."""
    import asyncio

    from portal.tts import demo_gen as dg
    from portal.tts.demo_gen import MANIFEST_PATH, generate_demo_assets

    async with dg._generation_lock:
        if dg._generating:
            return JSONResponse({"ok": False, "detail": "Already generating"})
        dg._generating = True

    MANIFEST_PATH.unlink(missing_ok=True)

    async def _run() -> None:
        try:
            await generate_demo_assets()
            dg._generation_error = None
        except Exception as exc:
            logger.exception("[Demo] Regeneration failed")
            dg._generation_error = str(exc)
        finally:
            dg._generating = False

    dg.track_task(asyncio.create_task(_run()))
    return JSONResponse({"ok": True, "detail": "Generation started"})


# ── Live demo (home page "Your voice") ───────────────────────────────────────

#: Time allowed for the browser's start message after the socket opens.
_START_TIMEOUT_SECONDS = 10.0
#: Time allowed for the last captions, translations and voice after the visitor stops.
_FINISH_TIMEOUT_SECONDS = 30.0


@router.get("/api/demo/live/config")
async def live_demo_config() -> JSONResponse:
    """Languages, limits and site glossary for the home-page live demo; ``enabled: false`` when it is off."""
    demo = await live_demo.current_settings()
    return JSONResponse(live_demo.public_config(demo), headers={"Cache-Control": "no-store"})


def _origin_allowed(websocket: WebSocket) -> bool:
    """Only pages served by this portal may open the demo socket, so other sites can't spend its CPU."""
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    public = urlparse(settings.public_base_url)
    ws_scheme = {"ws": "http", "wss": "https"}.get(websocket.url.scheme, websocket.url.scheme)
    allowed = {(public.scheme, public.netloc), (ws_scheme, websocket.url.netloc)}
    return (parsed.scheme, parsed.netloc) in allowed


async def _refuse(websocket: WebSocket, code: str, message: str, close_code: int) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message})
    await websocket.close(code=close_code)


@router.websocket("/ws/demo/live")
async def ws_live_demo(websocket: WebSocket) -> None:
    """Stream microphone PCM in; stream captions, translations and synthesized speech out.

    Protocol: the browser sends one JSON ``start`` message, then binary 16 kHz s16le mono
    frames, optional ``listen`` messages and finally ``stop``. The server answers with
    ``ready``, ``partial``/``final`` captions, ``translation`` rows, ``audio`` headers each
    followed by one binary 24 kHz PCM frame, and ``done`` before closing.
    """
    if not _origin_allowed(websocket):
        await websocket.close(code=4003)
        return
    await websocket.accept()

    demo = await live_demo.current_settings()
    if not demo.live:
        await _refuse(websocket, "disabled", "The live demo is switched off right now.", 1008)
        return
    if not live_demo.try_admit(demo.max_sessions):
        await _refuse(websocket, "busy", "The demo is busy with other visitors. Try again in a minute.", 1013)
        return

    session: live_demo.LiveDemoSession | None = None
    try:
        client_ip = websocket.client.host if websocket.client else "unknown"
        if not check_rate_limit(
            "live_demo", client_ip, max_requests=demo.sessions_per_ip_per_hour, window_seconds=3600
        ):
            await _refuse(websocket, "rate_limited", "You've used the demo a lot this hour. Try again later.", 1008)
            return

        try:
            start = json.loads(await asyncio.wait_for(websocket.receive_text(), _START_TIMEOUT_SECONDS))
            if not isinstance(start, dict) or start.get("type") != "start":
                raise live_demo.DemoRequestError("Expected a start message.")
            request = live_demo.parse_start(start, demo)
        except (TimeoutError, json.JSONDecodeError, KeyError):
            await _refuse(websocket, "invalid", "The demo couldn't start. Reload the page and try again.", 1008)
            return
        except live_demo.DemoRequestError as exc:
            await _refuse(websocket, "invalid", str(exc), 1008)
            return

        await websocket.send_json({"type": "status", "state": "warming"})
        try:
            await live_demo.warm_up(demo)
        except Exception:
            # Model download or load failures surface as many exception types (OSError, RuntimeError, HF errors).
            logger.exception("[LiveDemo] Models could not be loaded")
            await _refuse(websocket, "unavailable", "The speech models aren't ready on this server yet.", 1011)
            return

        session = live_demo.LiveDemoSession(
            request, websocket.send_json, websocket.send_bytes, engines=live_demo.LOCAL_ENGINES
        )
        session.start()
        max_seconds = demo.max_session_seconds
        await websocket.send_json({"type": "ready", "max_seconds": max_seconds})

        reason = await _pump(websocket, session, time.monotonic() + max_seconds)
        if reason is None:
            return
        try:
            await asyncio.wait_for(session.finish(), _FINISH_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("[LiveDemo] Session results did not finish in %ss", _FINISH_TIMEOUT_SECONDS)
        await websocket.send_json({"type": "done", "reason": reason})
        await websocket.close(code=1000)
    except (WebSocketDisconnect, RuntimeError):
        # The visitor closed the tab; RuntimeError is Starlette's "send after close".
        pass
    finally:
        if session is not None:
            session.cancel()
        live_demo.release()


async def _pump(websocket: WebSocket, session: live_demo.LiveDemoSession, deadline: float) -> str | None:
    """Feed the session until the visitor stops, goes quiet or runs out of time.

    Returns why the run ended, or None when the browser disconnected.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "time_limit"
        try:
            message = await asyncio.wait_for(websocket.receive(), min(remaining, live_demo.IDLE_TIMEOUT_SECONDS))
        except TimeoutError:
            return "time_limit" if time.monotonic() >= deadline else "idle"

        if message["type"] == "websocket.disconnect":
            return None
        frame = message.get("bytes")
        if frame is not None:
            if 0 < len(frame) <= live_demo.MAX_FRAME_BYTES and len(frame) % 2 == 0:
                session.push_audio(frame)
            continue
        try:
            control = json.loads(message.get("text") or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(control, dict):
            continue
        if control.get("type") == "listen":
            session.set_listen(control.get("lang"))
        elif control.get("type") == "stop":
            return "stopped"
