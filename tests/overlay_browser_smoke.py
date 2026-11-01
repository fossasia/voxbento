"""Real Chromium/HTTP/WebSocket smoke test, isolated from production services.

Run: uv run --with playwright python -m playwright install chromium
     PYTHONPATH=. uv run --with playwright python tests/overlay_browser_smoke.py
Optional OVERLAY_ARTIFACT_DIR saves screenshots and a browser recording.
This exercises the browser-source page, not the native OBS/vMix compositors.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "overlay-smoke-test-encryption-key-32-chars")
os.environ.setdefault("SECRET_KEY", "overlay-smoke-test-jwt-key-for-isolated-tests")

import uvicorn
from playwright.async_api import async_playwright, expect

from fastapi_app import app
from portal.auth import create_user_token
from portal.config import settings
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
)
from portal.models import Event, RoomTranslationLanguage
from portal.websockets.manager import listener_manager


async def main() -> None:
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        event = await create_event(s, slug="caption-demo", display_name="Caption overlay rehearsal")
        event.listener_join_code = "DEMO42"
        room = await create_room(s, event_id=event.id, display_name="Main stage")
        room.floor_transcription_enabled = True
        room.floor_transcription_provider = "local"
        room.floor_translation_enabled = True
        for code, name in [("ar", "Arabic"), ("hi", "Hindi"), ("zh", "Chinese"), ("ru", "Russian")]:
            s.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True))
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="en", language_name="English")
        admin = await create_user(
            s, email="owner@example.test", display_name="Owner", password_hash="unused", is_admin=True
        )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    artifacts = Path(os.environ["OVERLAY_ARTIFACT_DIR"]) if os.environ.get("OVERLAY_ARTIFACT_DIR") else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            options = {"viewport": {"width": 1920, "height": 1080}}
            if artifacts:
                options["record_video_dir"] = str(artifacts)
            admin_context = await browser.new_context(**options)
            await admin_context.add_cookies(
                [
                    {
                        "name": "user_token",
                        "value": create_user_token(user_id=admin.id, email=admin.email, is_admin=True),
                        "url": origin,
                    }
                ]
            )
            builder = await admin_context.new_page()
            errors = []
            builder.on("pageerror", lambda error: errors.append(str(error)))
            await builder.goto(f"{origin}/workspace/events/{event.id}/")
            await builder.locator("#overlay-channel").select_option(f"{room.id}/ar")
            await expect(builder.locator("#overlay-status")).to_contain_text("Connected")
            await builder.locator("#overlay-test").click()
            preview = builder.frame_locator("#overlay-preview-frame")
            await expect(preview.locator(".overlay-line")).to_have_text("Welcome everyone, we will begin in a moment.")
            await builder.locator("#overlay-font-size").fill("56")
            await builder.locator('input[name="overlay-position"][value="top"]').check()
            await expect(preview.locator("#overlay")).to_have_class("overlay overlay--top")
            await expect(preview.locator("#overlay")).to_have_css("--overlay-font-size", "56px")
            url = await builder.locator("#overlay-url").input_value()
            assert "/ar?" in url and "fontSize=56" in url and "position=top" in url
            # A separate, cookie-free browser context models an OBS/vMix source.
            source_context = await browser.new_context(**options)
            source = await source_context.new_page()
            source.on("pageerror", lambda error: errors.append(str(error)))
            await source.goto(url)
            await expect(source.locator("#overlay")).to_have_attribute("data-status", "connected")
            await expect(source.locator(".overlay-line")).to_have_count(0)
            await expect(source.locator("body")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            samples = {
                "ar": "مرحباً بكم في المؤتمر",
                "hi": "सम्मेलन में आपका स्वागत है",
                "zh": "欢迎参加本次会议",
                "ru": "Добро пожаловать на конференцию",
                "en": "Welcome to the conference",
                "floor": "Room audio captions",
            }
            for code, text in samples.items():
                await source.goto(f"{origin}/overlay/{event.slug}/{room.id}/{code}?code=DEMO42&lines=2")
                await expect(source.locator("#overlay")).to_have_attribute("data-status", "connected")
                bid = f"{event.slug}-{room.id}-{code}"
                await listener_manager.broadcast(
                    bid,
                    {
                        "type": "translated_caption" if code in {"ar", "hi", "zh", "ru"} else "caption",
                        "status": "final",
                        "segment_id": code,
                        "text": text,
                    },
                )
                await expect(source.locator(".overlay-line")).to_have_text(text)
                if code == "ar":
                    await expect(source.locator(".overlay-line")).to_have_css("direction", "rtl")
                if artifacts:
                    await source.screenshot(path=str(artifacts / f"overlay-{code}.png"), omit_background=True)
            # Test the actual server's expired-JWT close, refresh route and recovery.
            old_expiry = settings.listener_token_expiry_seconds
            settings.listener_token_expiry_seconds = -1
            try:
                await source.goto(f"{origin}/overlay/{event.slug}/{room.id}/ar?code=DEMO42")
                await expect(source.locator("#overlay")).to_have_attribute("data-status", "reconnecting")
            finally:
                settings.listener_token_expiry_seconds = old_expiry
            await expect(source.locator("#overlay")).to_have_attribute("data-status", "connected", timeout=10000)
            bid = f"{event.slug}-{room.id}-ar"
            await listener_manager.broadcast(
                bid, {"type": "translated_caption", "text": samples["ar"], "segment_id": "recovered"}
            )
            await expect(source.locator(".overlay-line")).to_have_text(samples["ar"])
            # Code rotation is picked up on reconnect and stops retrying permanently.
            async with get_session() as s:
                stored = await s.get(Event, event.id)
                stored.listener_join_code = "ROTATED"
            for ws in list(listener_manager._rooms.get(bid, set())):
                await ws.close(code=1012)
            await expect(source.locator("#overlay")).to_have_attribute("data-status", "denied", timeout=10000)
            assert not errors, errors
            if artifacts:
                await builder.screenshot(path=str(artifacts / "overlay-builder.png"), full_page=True)
            await source_context.close()
            await admin_context.close()
            await browser.close()
        print(
            "PASS: preview controls, test-caption isolation, transparent output, six channels, RTL, expired-token recovery, revoked-code denial; no browser JS errors"
        )
    finally:
        server.should_exit = True
        await task
        sock.close()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
