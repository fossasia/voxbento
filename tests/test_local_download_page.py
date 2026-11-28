from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import anyio
import pytest
from fastapi.testclient import TestClient

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import create_user, get_session

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    import anyio

    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    anyio.run(init_db)
    yield
    anyio.run(dispose)


def test_voxbento_local_page_renders_successfully():
    response = client.get("/local")
    assert response.status_code == 200
    html = response.text
    assert "VoxBento Local" in html
    assert "Desktop Console" in html
    assert "Apple macOS" in html
    assert "Microsoft Windows" in html
    assert "Linux / Ubuntu" in html
    assert "github.com/ArnavBallinCode/voxa" in html


def test_voxbento_local_page_trailing_slash_renders_successfully():
    response = client.get("/local/")
    assert response.status_code == 200
    assert "VoxBento Local" in response.text


def test_home_page_contains_local_download_links():
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "/local" in html
    assert "Local App" in html
    assert "Download App" in html


def test_local_page_renders_responsive_navigation_markup():
    response = client.get("/local")
    assert response.status_code == 200
    html = response.text
    assert 'id="local-desktop-nav" class="hidden xl:!flex' in html
    assert 'class="relative xl:!hidden"' in html
    assert 'aria-label="Toggle navigation menu"' in html
    assert 'id="local-mobile-menu"' in html
    assert 'class="neo-btn-primary hidden sm:!inline-flex' in html
    assert 'js/local-nav.js' in html
    assert 'border-[var(--vb-color-border-strong)] bg-[var(--vb-color-bg)]' in html
    assert 'shadow-[var(--vb-shadow-md)]' in html


def test_local_page_keeps_dashboard_label_for_signed_in_users():
    async def create_account() -> int:
        async with get_session() as session:
            user = await create_user(
                session,
                email="local-nav@example.com",
                display_name="Local Nav",
                password_hash=hash_password("local-nav-password"),
                email_verified=True,
            )
            return user.id

    user_id = anyio.run(create_account)
    token = create_user_token(user_id=user_id, email="local-nav@example.com")
    response = client.get("/local", cookies={"user_token": token})

    assert response.status_code == 200
    assert '<span class="hidden sm:!inline">Dashboard</span>' in response.text
    assert '<span class="sm:hidden">Dash</span>' in response.text
