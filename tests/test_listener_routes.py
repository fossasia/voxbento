"""
Tests for listener routes.

Covers:
- Missing event
- Join page rendering
- Join code validation
- Cookie creation
- Audio delay endpoint authorization
- Missing room
"""

from __future__ import annotations

import json
import os
import re

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for the FastAPI app."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def seed_event():
    """Seed an event, room, and booth."""
    from portal.database import create_booth, create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
    return event, room, booth


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestListenerRoutes:
    @pytest.mark.anyio
    async def test_missing_event_returns_404(self):
        async with _client() as c:
            resp = await c.get("/listener/does-not-exist")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_listener_without_code_renders_join_form(self, seed_event):
        event, _, _ = seed_event

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}")

        assert resp.status_code == 200
        assert b"Join Event" in resp.content
        assert b"Enter the join code" in resp.content
        assert b"cdn.tailwindcss.com" not in resp.content
        assert b"css/listener-join.css" in resp.content

    @pytest.mark.anyio
    async def test_invalid_join_code_shows_error(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=WRONG")

        assert resp.status_code == 200
        assert b"Invalid join code." in resp.content

    @pytest.mark.anyio
    async def test_valid_join_code_sets_cookie(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        assert "listener_code_testcon" in resp.headers["set-cookie"]

    @pytest.mark.anyio
    async def test_listener_status_is_announced(self, seed_event):
        """Screen reader users must hear when the stream goes live or drops."""
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        assert '<div id="status" role="status">' in resp.text

    @pytest.mark.anyio
    async def test_listener_page_has_share_button(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        assert b'id="share-event-btn"' in resp.content

    @pytest.mark.anyio
    async def test_listener_page_share_url_is_canonical_without_join_code(self, seed_event):
        event, _, _ = seed_event

        from portal.config import settings
        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        expected_url = f"{settings.public_base_url}/listener/{event.slug}"
        assert f'"shareUrl": "{expected_url}"'.encode() in resp.content
        assert b"?code=" not in resp.content.split(b'"shareUrl"')[1].split(b"\n")[0]

    @pytest.mark.anyio
    async def test_audio_delay_requires_listener_access(self, seed_event):
        event, room, _ = seed_event

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}/rooms/{room.id}/audio-delay")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_audio_delay_unknown_room_returns_404(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}/rooms/9999/audio-delay?code=ROOM42")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_listener_includes_floor_audio_when_captions_disabled(self, seed_event):
        """Test A: Floor Audio (Original) is included for valid Jitsi source when floor_transcription_enabled is False."""
        event, room, _ = seed_event

        from portal.database import create_booth, get_session
        from portal.models import Room

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "jitsi_bot"
            db_room.meeting_agent_enabled = True
            db_room.jitsi_url = "https://meet.jit.si/test-room"
            db_room.floor_transcription_enabled = False
            await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="es",
                language_name="Spanish",
            )
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Spanish" in booth_names
        assert "Floor Audio (Original)" in booth_names

    @pytest.mark.anyio
    async def test_listener_includes_floor_audio_when_captions_enabled(self, seed_event):
        """Test B: Floor Audio (Original) is included when floor_transcription_enabled is True."""
        event, room, _ = seed_event

        from portal.database import get_session
        from portal.models import Room

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "jitsi_bot"
            db_room.meeting_agent_enabled = False
            db_room.jitsi_url = None
            db_room.floor_transcription_enabled = True
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" in booth_names

    @pytest.mark.anyio
    async def test_listener_omits_floor_audio_when_no_source_configured(self, seed_event):
        """Test C: Floor Audio (Original) is not exposed when no floor source is configured."""
        event, room, _ = seed_event

        from portal.database import get_session
        from portal.models import Room

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "none"
            db_room.jitsi_url = None
            db_room.meeting_agent_enabled = False
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" not in booth_names

    @pytest.mark.anyio
    async def test_listener_includes_floor_audio_as_waiting_entry_before_agent_starts(self, seed_event):
        """Floor audio is available as a waiting entry when jitsi_url is configured, before the agent starts."""
        event, room, _ = seed_event

        from portal.database import get_session
        from portal.models import Room

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "jitsi_bot"
            db_room.meeting_agent_enabled = False
            db_room.jitsi_url = "https://meet.jit.si/test-room"
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" in booth_names

    @pytest.mark.anyio
    async def test_listener_omits_floor_audio_when_jitsi_bot_has_no_url_or_agent(self, seed_event):
        """Floor audio is omitted when jitsi_url is None and meeting_agent_enabled is False."""
        event, room, _ = seed_event

        from portal.database import get_session
        from portal.models import Room

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "jitsi_bot"
            db_room.meeting_agent_enabled = False
            db_room.jitsi_url = None
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" not in booth_names

    @pytest.mark.anyio
    async def test_listener_includes_floor_audio_for_configured_program_ingest(self, seed_event, monkeypatch):
        """Program ingest with valid credentials exposes Floor Audio (Original)."""
        event, room, _ = seed_event
        from datetime import datetime, timedelta, timezone

        from portal.config import settings
        from portal.database import get_session
        from portal.models import Room

        monkeypatch.setattr(settings, "program_ingest_enabled", True)

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "program_ingest"
            db_room.program_key_hash = "a" * 64
            db_room.program_key_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" in booth_names

    @pytest.mark.anyio
    async def test_listener_omits_floor_audio_for_unconfigured_program_ingest(self, seed_event, monkeypatch):
        """Program ingest without credentials or session does not expose floor audio."""
        event, room, _ = seed_event

        from portal.config import settings
        from portal.database import get_session
        from portal.models import Room

        monkeypatch.setattr(settings, "program_ingest_enabled", True)

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "program_ingest"
            db_room.program_key_hash = None
            db_room.program_session_secret_hash = None
            db_room.program_session_id = None
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" not in booth_names

    @pytest.mark.anyio
    async def test_listener_omits_floor_audio_for_relay_mode_when_relay_disabled(self, seed_event, monkeypatch):
        """Relay mode with valid credential omits floor audio when program_relay_enabled is False."""
        event, room, _ = seed_event
        from datetime import datetime, timedelta, timezone

        from portal import program_relay
        from portal.config import settings
        from portal.database import get_session
        from portal.models import Room

        monkeypatch.setattr(settings, "program_ingest_enabled", True)
        monkeypatch.setattr(settings, "program_relay_enabled", False)

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "program_ingest"
            db_room.program_ingest_mode = program_relay.RELAY_MODE
            db_room.program_key_hash = "a" * 64
            db_room.program_key_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" not in booth_names

    @pytest.mark.anyio
    async def test_listener_includes_floor_audio_for_reconnected_session_with_expired_credential(
        self, seed_event, monkeypatch
    ):
        """Active reconnected session with expired credential keeps Floor Audio (Original) visible."""
        event, room, _ = seed_event
        from datetime import datetime, timedelta, timezone

        from portal.config import settings
        from portal.database import get_session
        from portal.models import Room

        monkeypatch.setattr(settings, "program_ingest_enabled", True)

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"
            db_room = await s.get(Room, room.id)
            db_room.floor_source = "program_ingest"
            # Session is actively connected
            db_room.program_session_id = "session-active-456"
            # Earlier disconnect timestamp exists from previous session
            db_room.program_disconnected_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            # Short-lived credential has expired
            db_room.program_session_secret_hash = "b" * 64
            db_room.program_session_secret_expires_at = datetime.now(timezone.utc) - timedelta(minutes=2)
            db_room.program_key_hash = None
            db_room.floor_transcription_enabled = False
            await s.commit()

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        match = re.search(r'"booths":\s*(\[.*?\])\s*,', resp.text, re.DOTALL)
        assert match is not None
        booths = json.loads(match.group(1))
        booth_names = [b["language_name"] for b in booths]
        assert "Floor Audio (Original)" in booth_names
