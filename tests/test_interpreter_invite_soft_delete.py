"""Regression test: invite booth lookup excludes soft-deleted events.

Covers GET /interpreter (lobby) with a session_token invite cookie:
- active event renders DB display names
- soft-deleted event falls back to token slug/language code (no DB display names)

Covers GET /interpreter/{slug}/{room_id}/{lang} (booth page):
- active event renders the room's configured jitsi_url
- soft-deleted event falls back to the default Jitsi URL (no deleted room config)
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestInviteBoothLookupSoftDelete:
    @pytest.mark.anyio
    async def test_soft_deleted_event_excluded_from_invite_lookup(self):
        from portal.auth import create_participant_token
        from portal.database import create_booth, create_event, create_room, get_session

        async with get_session() as s:
            event = await create_event(s, slug="softdel", display_name="SoftDel Display Name")
            room = await create_room(s, event_id=event.id, display_name="SoftDel Hall")
            booth = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="fr",
                language_name="French Display Name",
            )
            event_id = event.id
            room_id = room.id
            booth_id = booth.id

        token = create_participant_token(
            booth_id=booth_id,
            role="interpreter",
            event_slug="softdel",
            room_id=room_id,
            language_code="fr",
        )

        async with _client() as c:
            resp = await c.get("/interpreter", cookies={"session_token": token})
            assert resp.status_code == 200, resp.text
            assert "SoftDel Display Name" in resp.text
            assert "French Display Name" in resp.text

            async with get_session() as s:
                from portal.models import Event

                db_event = await s.get(Event, event_id)
                assert db_event is not None
                db_event.deleted_at = datetime.now(timezone.utc)

            resp_deleted = await c.get("/interpreter", cookies={"session_token": token})
            assert resp_deleted.status_code == 200, resp_deleted.text
            assert "SoftDel Display Name" not in resp_deleted.text
            assert "French Display Name" not in resp_deleted.text

    @pytest.mark.anyio
    async def test_soft_deleted_event_booth_page_falls_back_to_default_jitsi(self):
        from portal.auth import create_participant_token
        from portal.config import settings
        from portal.database import create_booth, create_event, create_room, get_session
        from portal.utils import _make_jitsi_url

        custom_jitsi = "https://jitsi.example/softdel-custom-room"
        async with get_session() as s:
            event = await create_event(s, slug="softdelbooth", display_name="SoftDel Booth Event")
            room = await create_room(s, event_id=event.id, display_name="SoftDel Booth Hall", jitsi_url=custom_jitsi)
            booth = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="de",
                language_name="German Display Name",
            )
            event_id = event.id
            room_id = room.id
            booth_id = booth.id

        token = create_participant_token(
            booth_id=booth_id,
            role="interpreter",
            event_slug="softdelbooth",
            room_id=room_id,
            language_code="de",
        )
        booth_path = f"/interpreter/softdelbooth/{room_id}/de"
        expected_default = _make_jitsi_url(settings.effective_jitsi_base_url, settings.default_jitsi_room)

        async with _client() as c:
            resp = await c.get(booth_path, cookies={"session_token": token})
            assert resp.status_code == 200, resp.text
            assert custom_jitsi in resp.text

            async with get_session() as s:
                from portal.models import Event

                db_event = await s.get(Event, event_id)
                assert db_event is not None
                db_event.deleted_at = datetime.now(timezone.utc)

            resp_deleted = await c.get(booth_path, cookies={"session_token": token})
            assert resp_deleted.status_code == 200, resp_deleted.text
            assert custom_jitsi not in resp_deleted.text
            assert expected_default in resp_deleted.text
