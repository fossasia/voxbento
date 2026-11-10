"""Integration tests simulating massive concurrency, API key isolation, and race condition prevention."""

from __future__ import annotations

import asyncio
import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["API_KEY_ENCRYPTION_KEY"] = "test-key-encryption-key-for-transcription"

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import configure, dispose, get_session, init_db
from portal.models import DBBooth, Event, Room
from portal.transcription.worker import active_workers

# Bypass token requirements for WS/REST endpoints where applicable,
# and use an admin token to satisfy _require_access.


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    configure("sqlite+aiosqlite://")
    import anyio

    from portal.config import settings

    monkeypatch.setattr(settings, "debug", True)
    anyio.run(init_db)
    yield
    anyio.run(dispose)


@pytest.fixture(autouse=True)
async def clean_registry():
    for booth_id, session in list(active_workers.items()):
        session.stop()
        await session.wait_until_stopped()
    active_workers.clear()
    yield
    for booth_id, session in list(active_workers.items()):
        session.stop()
        await session.wait_until_stopped()
    active_workers.clear()


class MockProvider:
    def __init__(self, name):
        self.name = name
        self.received_configs = []
        self._config_cond: asyncio.Condition | None = None

    def _get_cond(self) -> asyncio.Condition:
        if self._config_cond is None:
            self._config_cond = asyncio.Condition()
        return self._config_cond

    def reset_configs(self) -> None:
        self.received_configs.clear()
        self._config_cond = None

    async def wait_for_configs(self, count: int, timeout: float = 2.0) -> None:
        cond = self._get_cond()
        async with cond:
            await asyncio.wait_for(cond.wait_for(lambda: len(self.received_configs) >= count), timeout=timeout)

    async def process_chunk(self, chunk, language_code, model_variant, config):
        pass

    async def run_stream(
        self, process, language_code, model_variant, config, broadcast_callback, booth_id, room_id=None
    ):
        # Log the config key to assert cross-contamination did not occur
        cond = self._get_cond()
        async with cond:
            self.received_configs.append({"booth_id": booth_id, "api_key": config.get_key()})
            cond.notify_all()
        # Simulate a long-lived streaming connection to keep the worker active
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise


mock_providers = {
    "local": MockProvider("local"),
    "openai": MockProvider("openai"),
    "deepgram": MockProvider("deepgram"),
    "nvidia": MockProvider("nvidia"),
    "sarvam": MockProvider("sarvam"),
}


@pytest.fixture(autouse=True)
def patch_transcription_dependencies():
    with patch("portal.transcription.worker.PROVIDERS", mock_providers):
        # Instead of mocking create_subprocess_exec (which leaves a real pid=MagicMock
        # that causes os.killpg() to signal the wrong process group and crash WSL),
        # we patch the entire FfmpegProcess context manager to return a safe dummy.
        class _DummyFfmpegCM:
            """A safe, no-op async context manager replacing FfmpegProcess in tests."""

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                # Return a minimal object with a real integer pid and returncode.
                class _FakeProcess:
                    pid = 99999  # unreachable fake pid — never passed to os.killpg
                    returncode = 0

                    class stdout:
                        async def read(n=-1):
                            return b""

                return _FakeProcess()

            async def __aexit__(self, *args):
                pass  # No cleanup needed — no real process was ever spawned.

        with patch("portal.transcription.worker.FfmpegProcess", _DummyFfmpegCM):
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
                event_id=e1.id,
                language_name=f"Lang {i}",
                language_code=lang_code,
                room_id=r.id,
                transcription_enabled=True,
                transcription_provider="openai",
                transcription_model="gpt-4o",
            )
            session.add(b)
            booths.append((e1.slug, r.id, b.language_code, f"{e1.slug}-{r.id}-{b.language_code}", "openai"))

        # Event 2: 6 NVIDIA booths
        for i, lang_code in enumerate(nvidia_langs):
            r = Room(event_id=e2.id, display_name=f"Room {i + 20}")
            session.add(r)
            await session.flush()
            b = DBBooth(
                event_id=e2.id,
                language_name=f"Lang {i}",
                language_code=lang_code,
                room_id=r.id,
                transcription_enabled=True,
                transcription_provider="nvidia",
                transcription_model="parakeet-rnnt",
            )
            session.add(b)
            booths.append((e2.slug, r.id, b.language_code, f"{e2.slug}-{r.id}-{b.language_code}", "nvidia"))

        # Event 3: 4 Local booths
        for i, lang_code in enumerate(local_langs):
            r = Room(event_id=e3.id, display_name=f"Room {i + 40}")
            session.add(r)
            await session.flush()
            b = DBBooth(
                event_id=e3.id,
                language_name=f"Lang {i}",
                language_code=lang_code,
                room_id=r.id,
                transcription_enabled=True,
                transcription_provider="local",
                transcription_model="tiny",
            )
            session.add(b)
            booths.append((e3.slug, r.id, b.language_code, f"{e3.slug}-{r.id}-{b.language_code}", "local"))

        await session.flush()
        return booths


@pytest.mark.anyio
async def test_high_concurrency_isolation_and_capacity_limits():
    """
    Spawns 16 concurrent POST requests to start transcription booths across 3 events.
    """
    booths = await seed_data()

    # Ensure fresh state
    for p in mock_providers.values():
        p.received_configs.clear()

    tok = create_user_token(user_id=1, email="admin@test.com", is_admin=True)
    cookies = {"user_token": tok}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}
    ) as client:
        # Launch all 16 POST requests simultaneously
        tasks = []
        for slug, room_id, lang, booth_id, provider in booths:
            data = {"event_slug": slug, "language_code": lang}
            tasks.append(
                client.post(
                    f"/api/events/{slug}/rooms/{room_id}/booths/{lang}/transcription/start", json=data, cookies=cookies
                )
            )

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
    assert len(active_workers) == 10

    # 2. Verify API Key Cross-Contamination did not occur
    openai_provider = mock_providers["openai"]
    nvidia_provider = mock_providers["nvidia"]

    for config in openai_provider.received_configs:
        assert config["booth_id"].startswith("event-alpha")
        assert config["api_key"] == "openai-key-alpha", "OpenAI booth got incorrect key!"

    for config in nvidia_provider.received_configs:
        assert config["booth_id"].startswith("event-beta")
        assert config["api_key"] == "nvidia-key-beta", "NVIDIA booth got incorrect key!"

    # 3. Simulate Concurrent Shutdown
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}
    ) as client:
        stop_tasks = []
        # Try to stop all 16 booths (the 2 rate-limited ones should just gracefully do nothing)
        for slug, room_id, lang, booth_id, provider in booths:
            stop_tasks.append(
                client.post(f"/api/events/{slug}/rooms/{room_id}/booths/{lang}/transcription/stop", cookies=cookies)
            )

        await asyncio.gather(*stop_tasks)

    # Verify cleanup is completely clean with no deadlocks
    assert len(active_workers) == 0


@pytest.mark.anyio
async def test_stop_before_task_starts_still_runs_cleanup():
    """Stopping a session before its task's first step must still release the model ref."""
    from portal.transcription.providers import local
    from portal.transcription.providers.base import ProviderConfig
    from portal.transcription.worker import State, start_transcription_worker, stop_transcription_worker

    async def _noop_broadcast(*args, **kwargs):
        pass

    local._active_booths_per_model["tiny"] = 0
    await start_transcription_worker("ev", "en", "ev-en", _noop_broadcast, "local", "tiny", ProviderConfig(api_key="x"))
    session = active_workers["ev-en"]

    # No event-loop yield between start and stop: the task has not run yet.
    await stop_transcription_worker("ev-en")

    assert session.state == State.STOPPED
    assert local._active_booths_per_model["tiny"] == 0


@pytest.mark.anyio
async def test_sarvam_api_key_isolation_across_concurrent_events():
    """Concurrent Sarvam booths on separate events receive only their own decrypted API key."""
    async with get_session() as session:
        e1 = Event(slug="sarvam-alpha", display_name="Sarvam Alpha", transcription_api_enabled=True)
        e1.encrypted_sarvam_api_key = encrypt_val("sarvam-key-alpha")
        e2 = Event(slug="sarvam-beta", display_name="Sarvam Beta", transcription_api_enabled=True)
        e2.encrypted_sarvam_api_key = encrypt_val("sarvam-key-beta")
        session.add_all([e1, e2])
        await session.flush()

        r1 = Room(event_id=e1.id, display_name="Hall 1")
        r2 = Room(event_id=e2.id, display_name="Hall 2")
        session.add_all([r1, r2])
        await session.flush()

        b1 = DBBooth(
            event_id=e1.id,
            room_id=r1.id,
            language_code="hi",
            language_name="Hindi",
            transcription_enabled=True,
            transcription_provider="sarvam",
            transcription_model="saaras:v3-realtime",
        )
        b2 = DBBooth(
            event_id=e2.id,
            room_id=r2.id,
            language_code="ta",
            language_name="Tamil",
            transcription_enabled=True,
            transcription_provider="sarvam",
            transcription_model="saaras:v3-realtime",
        )
        session.add_all([b1, b2])
        await session.flush()
        targets = [(e1.slug, r1.id, "hi"), (e2.slug, r2.id, "ta")]

    mock_providers["sarvam"].reset_configs()
    tok = create_user_token(user_id=1, email="admin@test.com", is_admin=True)
    cookies = {"user_token": tok}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}
    ) as client:
        responses = await asyncio.gather(
            *[
                client.post(f"/api/events/{slug}/rooms/{rid}/booths/{lang}/transcription/start", cookies=cookies)
                for slug, rid, lang in targets
            ]
        )
        assert [r.status_code for r in responses] == [200, 200]
        await mock_providers["sarvam"].wait_for_configs(2)

        configs = {c["booth_id"]: c["api_key"] for c in mock_providers["sarvam"].received_configs}
        assert configs[f"sarvam-alpha-{targets[0][1]}-hi"] == "sarvam-key-alpha"
        assert configs[f"sarvam-beta-{targets[1][1]}-ta"] == "sarvam-key-beta"

        await asyncio.gather(
            *[
                client.post(f"/api/events/{slug}/rooms/{rid}/booths/{lang}/transcription/stop", cookies=cookies)
                for slug, rid, lang in targets
            ]
        )

    assert len(active_workers) == 0
