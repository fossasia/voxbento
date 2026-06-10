"""Integration tests simulating massive concurrency, API key isolation, and race condition prevention."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from fastapi_app import app
from portal.crypto import encrypt_val
from portal.database import configure, dispose, init_db, get_session
from portal.models import Event, DBBooth, Room
from portal.transcription.worker import active_workers, active_processes
from portal.auth import create_user_token

# Bypass token requirements for WS/REST endpoints where applicable,
# and use an admin token to satisfy _require_access.
os.environ['BOOTH_ACCESS_TOKEN'] = ''

@pytest.fixture(autouse=True)
def setup_db():
    configure('sqlite+aiosqlite://')
    import anyio
    anyio.run(init_db)
    yield
    anyio.run(dispose)

class MockProvider:
    def __init__(self, name):
        self.name = name
        self.received_configs = []

    async def process_chunk(self, chunk, language_code, model_variant, config):
        pass

    async def run_stream(self, process, language_code, model_variant, config, broadcast_callback, booth_id):
        # Log the config key to assert cross-contamination did not occur
        self.received_configs.append({
            "booth_id": booth_id,
            "api_key": config.get_key()
        })
        # Simulate a long-lived streaming connection to keep the worker active
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

mock_providers = {
    'local': MockProvider('local'),
    'openai': MockProvider('openai'),
    'deepgram': MockProvider('deepgram'),
    'nvidia': MockProvider('nvidia'),
}

@pytest.fixture(autouse=True)
def patch_transcription_dependencies():
    with patch('portal.transcription.worker.PROVIDERS', mock_providers):
        # Mock ffmpeg subprocess creation
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stderr.readline = AsyncMock(side_effect=[b"ffmpeg logs", b""])
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            yield

async def seed_data():
    async with get_session() as session:
        e1 = Event(slug="event-alpha", display_name="Event Alpha", transcription_api_enabled=True)
        e1.encrypted_openai_api_key = encrypt_val("openai-key-alpha")
        e1.encrypted_deepgram_api_key = encrypt_val("deepgram-key-alpha")
        session.add(e1)
        
        e2 = Event(slug="event-beta", display_name="Event Beta", transcription_api_enabled=True)
        e2.encrypted_openai_api_key = encrypt_val("openai-key-beta")
        e2.encrypted_nvidia_api_key = encrypt_val("nvidia-key-beta")
        session.add(e2)
        
        e3 = Event(slug="event-gamma", display_name="Event Gamma", transcription_api_enabled=False)
        session.add(e3)
        
        await session.flush()
        
        booths = []
        
        openai_langs = ["en", "fr", "de", "es", "it", "pt"]
        nvidia_langs = ["ru", "zh", "ja", "ko", "ar", "hi"]
        local_langs = ["tr", "vi", "th", "nl"]
        
        # Event 1: 6 OpenAI booths
        for i, lang_code in enumerate(openai_langs):
            r = Room(event_id=e1.id, display_name=f"Room {i}")
            session.add(r)
            await session.flush()
            b = DBBooth(
                event_id=e1.id, language_name=f"Lang {i}", language_code=lang_code, room_id=r.id,
                transcription_enabled=True, transcription_provider="openai", transcription_model="gpt-4o"
            )
            session.add(b)
            booths.append((e1.slug, b.language_code, f"{e1.slug}-{b.language_code}", "openai"))
            
        # Event 2: 6 NVIDIA booths
        for i, lang_code in enumerate(nvidia_langs):
            r = Room(event_id=e2.id, display_name=f"Room {i+20}")
            session.add(r)
            await session.flush()
            b = DBBooth(
                event_id=e2.id, language_name=f"Lang {i}", language_code=lang_code, room_id=r.id,
                transcription_enabled=True, transcription_provider="nvidia", transcription_model="parakeet-rnnt"
            )
            session.add(b)
            booths.append((e2.slug, b.language_code, f"{e2.slug}-{b.language_code}", "nvidia"))
            
        # Event 3: 4 Local booths
        for i, lang_code in enumerate(local_langs):
            r = Room(event_id=e3.id, display_name=f"Room {i+40}")
            session.add(r)
            await session.flush()
            b = DBBooth(
                event_id=e3.id, language_name=f"Lang {i}", language_code=lang_code, room_id=r.id,
                transcription_enabled=True, transcription_provider="local", transcription_model="tiny"
            )
            session.add(b)
            booths.append((e3.slug, b.language_code, f"{e3.slug}-{b.language_code}", "local"))
            
        await session.flush()
        return booths

@pytest.mark.anyio
async def test_high_concurrency_isolation_and_capacity_limits():
    """
    Spawns 16 concurrent POST requests to start transcription booths across 3 events.
    Verifies:
    1. Lock-safe global worker dictionaries (`active_workers`, `active_processes`).
    2. Capacity limit (12 external booths requested -> 10 succeed, 2 hit 429 error).
    3. API Key Isolation (Event A's worker gets Event A's decrypted key, no cross-contamination).
    """
    booths = await seed_data()
    
    # Ensure fresh state
    active_workers.clear()
    active_processes.clear()
    for p in mock_providers.values():
        p.received_configs.clear()

    tok = create_user_token(user_id=1, email='admin@test.com', is_admin=True)
    cookies = {'user_token': tok}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Launch all 16 POST requests simultaneously
        tasks = []
        for slug, lang, booth_id, provider in booths:
            data = {"event_slug": slug, "language_code": lang}
            tasks.append(client.post(f"/api/booth/{booth_id}/transcription/start", json=data, cookies=cookies))
            
        responses = await asyncio.gather(*tasks)
        
    # Analyze Responses
    status_codes = [r.status_code for r in responses]
    
    # 16 total booths were fired (6 OpenAI, 6 NVIDIA, 4 Local).
    # MAX_TOTAL_WORKERS = 10, so exactly 6 requests must hit 429 Too Many Requests.
    assert status_codes.count(429) == 6, "Exactly 6 booths should be rate-limited."
    assert status_codes.count(200) == 10, "Exactly 10 booths should succeed."
    
    # Give the background tasks a tiny fraction of a second to spin up and populate the provider logs
    await asyncio.sleep(0.1)
    
    # 1. Verify Global Locking limits worked
    assert len(active_processes) == 10

    # 2. Verify API Key Cross-Contamination did not occur
    openai_provider = mock_providers['openai']
    nvidia_provider = mock_providers['nvidia']
    
    for config in openai_provider.received_configs:
        assert config["booth_id"].startswith("event-alpha")
        assert config["api_key"] == "openai-key-alpha", "OpenAI booth got incorrect key!"
        
    for config in nvidia_provider.received_configs:
        assert config["booth_id"].startswith("event-beta")
        assert config["api_key"] == "nvidia-key-beta", "NVIDIA booth got incorrect key!"
        
    # 3. Simulate Concurrent Shutdown
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stop_tasks = []
        # Try to stop all 16 booths (the 2 rate-limited ones should just gracefully do nothing)
        for slug, lang, booth_id, provider in booths:
            stop_tasks.append(client.post(f"/api/booth/{booth_id}/transcription/stop", cookies=cookies))
            
        await asyncio.gather(*stop_tasks)
        
    # Verify cleanup is completely clean with no ProcessLookupErrors or deadlocks
    assert len(active_workers) == 0
    assert len(active_processes) == 0
