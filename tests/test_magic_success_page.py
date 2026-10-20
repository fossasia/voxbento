"""magic_success.html must stay free of inline scripts/styles (CSP, AGENTS.md invariant #5)."""

from __future__ import annotations

import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.database import configure, create_auth_token, create_user, dispose, get_session, init_db

os.environ["BOOTH_ACCESS_TOKEN"] = ""


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


async def _redeem_magic_link(next_url: str):
    async with get_session() as session:
        user = await create_user(session, email="magic@example.com", display_name="Magic", email_verified=True)
        await create_auth_token(session, jti="magic-jti", user_id=user.id, token_type="magic_link")  # nosec B106
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/auth/magic/magic-jti", params={"next": next_url})


@pytest.mark.anyio
async def test_magic_success_has_no_inline_script_or_style():
    resp = await _redeem_magic_link("/oauth/authorize?client_id=abc&state=xyz")

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Successfully Signed In" in body
    assert re.search(r'<script type="module" src="[^"]*/js/magic-success\.js', body)
    assert "css/auth.css" in body
    # base.html's shared toast script is out of scope; check only this template's content block.
    content = body.split('<main class="portal-main">')[1].split("</main>")[0]
    assert "<script" not in content
    assert "style=" not in content


@pytest.mark.anyio
async def test_magic_success_passes_next_url_as_escaped_data_attribute():
    resp = await _redeem_magic_link('/oauth/authorize?state="><img src=x onerror=alert(1)>')

    assert resp.status_code == 200, resp.text
    assert "<img src=x" not in resp.text
    assert 'data-next-url="/oauth/authorize?state=&#34;&gt;&lt;img' in resp.text
