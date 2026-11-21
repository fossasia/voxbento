from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest
from fastapi.testclient import TestClient

from fastapi_app import app

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


def test_local_page_navigation_collapses_at_tablet_widths():
    response = client.get("/local")
    assert response.status_code == 200
    html = response.text
    assert 'id="local-desktop-nav" class="hidden xl:!flex' in html
    assert 'class="relative xl:!hidden"' in html
    assert 'aria-label="Toggle navigation menu"' in html
    assert 'id="local-mobile-menu"' in html
    assert 'class="neo-btn-primary hidden sm:!inline-flex' in html
