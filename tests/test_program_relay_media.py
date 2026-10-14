"""Opt-in real RTMPS ingest -> MediaMTX -> program-relay -> RTMPS sink test (#749).

MEDIAMTX_TEST_BINARY=/path/to/mediamtx uv run pytest tests/test_program_relay_media.py -v
Requires MediaMTX 1.18.2 and FFmpeg (libx264, AAC, TLS) on PATH. The local MediaMTX
instance doubles as the RTMPS sink standing in for YouTube; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import importlib.util
import os
import shutil
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from fastapi_app import app
from portal import globals as pg
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal.auth import create_user_token
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room
from portal.transcription.process import FfmpegProcess

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("MEDIAMTX_TEST_BINARY"), reason="Native media relay test is opt-in"),
]
CONTROL_SECRET = "c" * 64
HOOK_SECRET = "h" * 64
SINK_KEY = "relay-sink-key-1234"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def self_signed(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "rtmps.crt", directory / "rtmps.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def load_relay_service():
    spec = importlib.util.spec_from_file_location(
        "program_relay_service_native", Path(__file__).parents[1] / "program-relay" / "relay.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def wait_path(path: str, attempts: int = 150):
    """Return the path once MediaMTX reports it ready, else None."""
    for _ in range(attempts):
        current = await ingest.media_path(path)
        if current and current.ready:
            return current
        await asyncio.sleep(0.1)
    return None


async def wait_gone(path: str, attempts: int = 150) -> bool:
    """True once MediaMTX no longer has a ready publisher on the path (404 counts)."""
    for _ in range(attempts):
        current = await ingest.media_path(path)
        if not current or not current.ready:
            return True
        await asyncio.sleep(0.1)
    return False


async def test_native_rtmps_ingest_relays_to_sink_and_feeds_audio(tmp_path, monkeypatch):
    assert shutil.which("ffmpeg")
    configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db()
    http_port, api_port, rtsp_port, rtmps_port, relay_port = [free_port() for _ in range(5)]
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", HOOK_SECRET)
    monkeypatch.setattr(settings, "program_relay_enabled", True)
    monkeypatch.setattr(settings, "program_relay_control_secret", CONTROL_SECRET)
    monkeypatch.setattr(settings, "program_relay_base", f"http://127.0.0.1:{relay_port}")
    monkeypatch.setattr(settings, "program_relay_allowed_hosts", "127.0.0.1")
    monkeypatch.setattr(settings, "program_rtmps_public_base", f"rtmps://127.0.0.1:{rtmps_port}")
    monkeypatch.setattr(settings, "mediamtx_api_base", f"http://127.0.0.1:{api_port}")
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 0)
    async with get_session() as session:
        admin = await create_user(
            session, email="relay-native@example.com", display_name="Relay", email_verified=True, is_admin=True
        )
        event = Event(slug="native", display_name="Native")
        session.add(event)
        await session.flush()
        # Captions off: proves YouTube relay does not depend on language processing.
        room = Room(event_id=event.id, display_name="Native", floor_transcription_enabled=False)
        session.add(room)
    cert, key = self_signed(tmp_path)
    config = tmp_path / "media.yml"
    config.write_text(f"""logLevel: info
authMethod: http
authHTTPAddress: http://127.0.0.1:{http_port}/internal/media-auth?key={HOOK_SECRET}
authHTTPExclude:
  - action: api
  - action: read
api: yes
apiAddress: 127.0.0.1:{api_port}
rtspAddress: 127.0.0.1:{rtsp_port}
rtspTransports: [tcp]
rtmp: yes
rtmpEncryption: strict
rtmpsAddress: 127.0.0.1:{rtmps_port}
rtmpServerKey: {key.as_posix()}
rtmpServerCert: {cert.as_posix()}
srt: no
hls: no
webrtc: no
pathDefaults:
  overridePublisher: no
paths:
  all_others:
""")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, lifespan="off", log_level="error"))
    server_task = asyncio.create_task(server.serve())
    service = load_relay_service()
    manager = service.RelayManager(
        source_base=f"rtsp://127.0.0.1:{rtsp_port}", allowed_hosts={"127.0.0.1"}, max_relays=2, tls_verify=False
    )
    relay_server = ThreadingHTTPServer(("127.0.0.1", relay_port), service.make_handler(manager, CONTROL_SECRET))
    threading.Thread(target=relay_server.serve_forever, daemon=True).start()
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
            base = f"http://127.0.0.1:{http_port}/admin/events/{event.id}/rooms/{room.id}"
            client.cookies.set("user_token", create_user_token(user_id=admin.id, email=admin.email, is_admin=True))
            enabled = await client.post(f"{base}/program-ingest", json={"action": "enable", "mode": "rtmps_relay"})
            assert enabled.status_code == 200, enabled.text
            secret, stream_key = enabled.json()["secret"], enabled.json()["stream_key"]
            server_url = (await client.get(f"{base}/program-ingest")).json()["rtmps_server"]
            destination = await client.put(
                f"{base}/program-relay",
                json={"server_url": f"rtmps://127.0.0.1:{rtmps_port}/sink", "stream_key": SINK_KEY},
            )
            assert destination.status_code == 200, destination.text
            floor, sink = f"native/{room.id}/floor", f"sink/{SINK_KEY}"

            async def publish(url):
                # What OBS sends: "Server" + "/" + "Stream Key", H.264 + AAC over RTMPS.
                return await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "50", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "96k", "-tls_verify", "0", "-f", "flv", url,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )  # fmt: skip

            anonymous = await publish(f"{server_url}/floor")
            await asyncio.wait_for(anonymous.wait(), 20)
            assert anonymous.returncode != 0
            encoder = await publish(f"{server_url}/{stream_key}")
            ready = await wait_path(floor)
            if ready is None:
                details = ""
                if encoder.returncode is not None:
                    _, stderr = await encoder.communicate()
                    details = stderr.decode(errors="replace")
                log = (tmp_path / "media.log").read_text()
                pytest.fail(f"RTMPS did not publish: {details}{log}".replace(secret, "[REDACTED]"))
            assert "H264" in ready.tracks and "MPEG-4 Audio" in ready.tracks
            await ingest.reconcile_once()
            relayed = await wait_path(sink)
            assert relayed is not None, "relay never reached the RTMPS sink"
            assert "H264" in relayed.tracks and "MPEG-4 Audio" in relayed.tracks
            for _ in range(50):
                await ingest.reconcile_once()
                health = (await client.get(f"{base}/program-ingest")).json()["health"]
                if health["relay"]["state"] == "live":
                    break
                await asyncio.sleep(0.2)
            assert health["ingest"]["state"] == "connected" and health["relay"]["state"] == "live"
            assert health["processing"]["state"] == "degraded"  # captions disabled; relay unaffected
            # The same feed's audio decodes for the language pipeline.
            async with FfmpegProcess(f"rtsp://127.0.0.1:{rtsp_port}/{floor}", "16000", "relay-native") as decoder:
                pcm = await asyncio.wait_for(decoder.stdout.readexactly(32000), 20)
                assert len(pcm) == 32000 and any(pcm)
            # Revoking the YouTube destination stops the relay and the sink goes away.
            assert (await client.delete(f"{base}/program-relay")).status_code == 200
            assert await wait_gone(sink)
            assert manager.snapshot() == {}
            # Revoking the ingest credential disconnects the encoder; the old key is refused.
            assert (await client.post(f"{base}/program-ingest", json={"action": "revoke"})).status_code == 200
            assert await wait_gone(floor)
            replay = await publish(f"{server_url}/{stream_key}")
            await asyncio.wait_for(replay.wait(), 20)
            assert replay.returncode != 0
            log = (tmp_path / "media.log").read_text()
            assert secret not in log and SINK_KEY not in log.replace(f"sink/{SINK_KEY}", "")
    finally:
        manager.stop_all()
        relay_server.shutdown()
        relay_server.server_close()
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
        relay.relay_health.clear()
        await dispose()
