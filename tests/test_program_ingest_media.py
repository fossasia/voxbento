"""Opt-in real WHIP -> MediaMTX -> RTSP -> PCM test, no cloud API or production.

MEDIAMTX_TEST_BINARY=/path/to/mediamtx uv run pytest tests/test_program_ingest_media.py -v
Requires MediaMTX 1.18.2 and an FFmpeg build with the WHIP muxer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from fastapi_app import app
from portal import globals as pg
from portal import program_ingest as ingest
from portal.auth import create_admin_token
from portal.config import settings
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room
from portal.transcription.process import FfmpegProcess

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("MEDIAMTX_TEST_BINARY"), reason="Native media smoke test is opt-in"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_native_whip_auth_decode_revoke(tmp_path, monkeypatch):
    assert shutil.which("ffmpeg")
    configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db()
    http_port, api_port, whip_port, rtsp_port, ice_port = [free_port() for _ in range(5)]
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "mediamtx_api_base", f"http://127.0.0.1:{api_port}")
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 0)
    async with get_session() as session:
        event = Event(slug="native", display_name="Native")
        session.add(event)
        await session.flush()
        room = Room(event_id=event.id, display_name="Native", floor_source="program_ingest")
        session.add(room)
    config = tmp_path / "media.yml"
    config.write_text(f"""logLevel: warn
authMethod: http
authHTTPAddress: http://127.0.0.1:{http_port}/internal/media-auth
authHTTPExclude:
  - action: api
  - action: read
api: yes
apiAddress: 127.0.0.1:{api_port}
rtspAddress: 127.0.0.1:{rtsp_port}
rtspTransports: [tcp]
rtmp: no
srt: no
hls: no
webrtcAddress: 127.0.0.1:{whip_port}
webrtcLocalUDPAddress: 127.0.0.1:{ice_port}
webrtcAdditionalHosts: [127.0.0.1]
pathDefaults:
  overridePublisher: no
paths:
  all_others:
""")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, lifespan="off", log_level="error"))
    server_task = asyncio.create_task(server.serve())
    media_log = (tmp_path / "media.log").open("wb")
    media = await asyncio.create_subprocess_exec(
        os.environ["MEDIAMTX_TEST_BINARY"], str(config), stdout=media_log, stderr=media_log
    )
    encoder = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            pg.shared_http_client = client
            for _ in range(100):
                try:
                    response = await client.get(f"{settings.mediamtx_api_base}/v3/paths/list")
                    if response.status_code == 200 and server.started:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail((tmp_path / "media.log").read_text())
            url = f"http://127.0.0.1:{http_port}/admin/events/{event.id}/rooms/{room.id}/program-ingest"
            client.cookies.set("admin_token", create_admin_token())
            response = await client.post(url, json={"action": "enable"})
            assert response.status_code == 200, response.text
            token = response.json()["secret"]
            path = f"native/{room.id}/floor"

            async def publish(secret):
                return await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "libopus",
                    "-f",
                    "whip",
                    "-authorization",
                    secret,
                    f"http://127.0.0.1:{whip_port}/{path}/whip",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )

            invalid = await publish("invalid")
            await asyncio.wait_for(invalid.wait(), 15)
            assert invalid.returncode != 0
            encoder = await publish(token)
            for _ in range(100):
                ready = await ingest.media_path(path)
                if ready and ready.ready:
                    break
                await asyncio.sleep(0.1)
            else:
                details = ""
                if encoder.returncode is not None:
                    _, stderr = await encoder.communicate()
                    details = stderr.decode().replace(token, '[REDACTED]')
                pytest.fail("WHIP did not publish: " + details + (tmp_path / "media.log").read_text())
            assert "Opus" in ready.tracks
            async with FfmpegProcess(f"rtsp://127.0.0.1:{rtsp_port}/{path}", "16000", "native-test") as decoder:
                pcm = await asyncio.wait_for(decoder.stdout.readexactly(32000), 15)
                assert len(pcm) == 32000 and any(pcm)
            assert (await client.post(url, json={"action": "revoke"})).status_code == 200
            for _ in range(30):
                ready = await ingest.media_path(path)
                if not ready or not ready.ready:
                    break
                await asyncio.sleep(0.1)
            assert not ready or not ready.ready
            # Native MediaMTX must reject the previously accepted credential.
            invalid = await publish(token)
            await asyncio.wait_for(invalid.wait(), 15)
            assert invalid.returncode != 0
            assert token not in (tmp_path / "media.log").read_text()
    finally:
        for process in [encoder, media]:
            if process and process.returncode is None:
                process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 5)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        media_log.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        pg.shared_http_client = None
        ingest.health.clear()
        ingest.failures.clear()
        await dispose()
