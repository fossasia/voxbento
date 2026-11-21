"""Responsive Local navbar browser smoke test.

Run with: uv run --with playwright python -m playwright install chromium
          PYTHONPATH=. uv run --with playwright python tests/local_nav_browser_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import socket

os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "local-nav-smoke-test-encryption-key-32-chars")
os.environ.setdefault("SECRET_KEY", "local-nav-smoke-test-jwt-key-for-isolated-tests")

import uvicorn
from playwright.async_api import async_playwright, expect

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import configure, create_user, dispose, get_session, init_db


async def main() -> None:
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        user = await create_user(
            session,
            email="local-nav@example.com",
            display_name="Local Nav",
            password_hash=hash_password("local-nav-password"),
            email_verified=True,
        )
        user_token = create_user_token(user_id=user.id, email=user.email)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 360, "height": 800})
                await page.goto(f"{origin}/local", wait_until="load")

                for width in (360, 768, 1024, 1279, 1280):
                    await page.set_viewport_size({"width": width, "height": 800})
                    desktop_nav = page.locator("#local-desktop-nav")
                    menu_toggle = page.locator("#navbar details > summary")
                    assert (await desktop_nav.is_visible()) == (width >= 1280)
                    assert (await menu_toggle.is_visible()) == (width < 1280)
                    layout = await page.evaluate(
                        """() => {
                          const header = document.querySelector('#navbar > div');
                          const brand = header.querySelector(':scope > a').getBoundingClientRect();
                          const controls = header.querySelector(':scope > div:last-child').getBoundingClientRect();
                          return {
                            width: window.innerWidth,
                            pageWidth: document.documentElement.scrollWidth,
                            brandRight: brand.right,
                            controlsLeft: controls.left,
                            controlsRight: controls.right,
                          };
                        }"""
                    )
                    assert layout["pageWidth"] <= width, layout
                    assert layout["brandRight"] <= layout["controlsLeft"], layout
                    assert layout["controlsRight"] <= width, layout

                await page.context.add_cookies([{"name": "user_token", "value": user_token, "url": origin}])
                await page.reload(wait_until="load")
                account_labels = page.locator('#navbar a[href="/account"] span')
                for width, expected in ((360, (False, True)), (768, (True, False))):
                    await page.set_viewport_size({"width": width, "height": 800})
                    visibility = [await account_labels.nth(index).is_visible() for index in range(2)]
                    assert visibility == list(expected), {"width": width, "visibility": visibility}

                await page.set_viewport_size({"width": 360, "height": 800})
                details = page.locator("#navbar details")
                await page.locator("#navbar details > summary").click()
                await expect(page.locator("#local-mobile-menu")).to_be_visible()
                await page.locator('#local-mobile-menu a[href="#features"]').click()
                assert not await details.evaluate("element => element.open")
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await task
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
