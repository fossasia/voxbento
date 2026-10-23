"""Real RTMPS ingest -> MediaMTX -> program-relay -> RTMPS sink tests (#749).

MEDIAMTX_TEST_BINARY=/path/to/mediamtx uv run pytest tests/test_program_relay_media.py -v
Requires MediaMTX 1.18.2 and FFmpeg (libx264, AAC, TLS) on PATH. CI runs this module in the
``program-relay-media`` job against the pinned MediaMTX release. The local MediaMTX instance
doubles as the RTMPS sink standing in for YouTube; nothing leaves the machine. Every
VoxBento publish goes through the real MediaMTX -> /internal/media-auth callback, never a
mock; only the stand-in sink/ path is excluded, as YouTube is outside the portal's authority.
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
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    # CI sets PROGRAM_RELAY_MEDIA_REQUIRED so a missing binary fails the gate instead of skipping it.
    pytest.mark.skipif(
        not os.environ.get("MEDIAMTX_TEST_BINARY") and not os.environ.get("PROGRAM_RELAY_MEDIA_REQUIRED"),
        reason="Native media relay test is opt-in locally; required in CI",
    ),
]
CONTROL_SECRET = "c" * 64
HOOK_SECRET = "h" * 64
SINK_KEY = "relay-sink-key-1234"
# Short enough for CI, long enough for TLS + RTMP handshakes on a loaded runner.
STALL_SECS = 3.0
IO_TIMEOUT_SECS = 8.0


_RESERVED_PORTS: set[int] = set()


def free_ports(*names: str) -> dict[str, int]:
    """Allocate distinct, unreserved ephemeral ports for the named services."""
    sockets: list[socket.socket] = []
    result: dict[str, int] = {}
    try:
        for name in names:
            while True:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                if port not in _RESERVED_PORTS:
                    _RESERVED_PORTS.add(port)
                    sockets.append(sock)
                    result[name] = port
                    break
                sock.close()
    finally:
        for sock in sockets:
            sock.close()
    return result


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            if port not in _RESERVED_PORTS:
                _RESERVED_PORTS.add(port)
                return port


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


async def wait_path(path: str, attempts: int = 200):
    """Return the path once MediaMTX reports it ready, else None."""
    for _ in range(attempts):
        current = await ingest.media_path(path)
        if current and current.ready:
            return current
        await asyncio.sleep(0.1)
    return None


async def wait_gone(path: str, attempts: int = 200) -> bool:
    """True once MediaMTX no longer has a ready publisher on the path (404 counts)."""
    for _ in range(attempts):
        current = await ingest.media_path(path)
        if not current or not current.ready:
            return True
        await asyncio.sleep(0.1)
    return False


async def eventually(predicate, timeout: float = 30.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


class FreezingProxy:
    """TCP pass-through in front of the RTMPS sink that can stop reading established sessions.

    ``freeze()`` models a destination that accepted the stream and then stopped consuming it:
    the relay's writes back up and block while its process keeps running. New connections
    after the freeze pass through, so a reconnecting relay can recover.
    """

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.sessions: list[dict[str, bool]] = []
        self.tasks: set[asyncio.Task] = set()

    async def start(self) -> int:
        while True:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # A small receive window makes the sender block quickly once we stop reading.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            if port not in _RESERVED_PORTS:
                _RESERVED_PORTS.add(port)
                break
            sock.close()
        sock.listen()
        sock.setblocking(False)
        self.server = await asyncio.start_server(self._handle, sock=sock)
        return port

    def freeze(self) -> None:
        for session in self.sessions:
            session["frozen"] = True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.tasks.add(asyncio.current_task())
        session = {"frozen": False}
        self.sessions.append(session)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.close()
            return

        async def pump(source, sink, freezable: bool) -> None:
            try:
                while True:
                    if freezable and session["frozen"]:
                        await asyncio.sleep(3600)
                    data = await source.read(4096)
                    if not data:
                        break
                    sink.write(data)
                    await sink.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                sink.close()

        await asyncio.gather(
            pump(reader, upstream_writer, True), pump(upstream_reader, writer, False), return_exceptions=True
        )

    async def close(self) -> None:
        self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@dataclass
class Stack:
    client: httpx.AsyncClient
    event: Event
    rooms: list[Room]
    manager: Any
    ports: dict[str, int]
    tmp_path: Path
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)

    def admin_url(self, room: Room, suffix: str) -> str:
        return f"http://127.0.0.1:{self.ports['http']}/admin/events/{self.event.id}/rooms/{room.id}/{suffix}"

    def server_url(self, room: Room) -> str:
        return f"rtmps://127.0.0.1:{self.ports['rtmps']}/native/{room.id}"

    def floor(self, room: Room) -> str:
        return f"native/{room.id}/floor"

    async def enable(self, room: Room) -> tuple[str, str]:
        response = await self.client.post(
            self.admin_url(room, "program-ingest"), json={"action": "enable", "mode": "rtmps_relay"}
        )
        assert response.status_code == 200, response.text
        return response.json()["secret"], response.json()["stream_key"]

    async def set_destination(self, room: Room, port: int) -> None:
        response = await self.client.put(
            self.admin_url(room, "program-relay"),
            json={"server_url": f"rtmps://127.0.0.1:{port}/sink", "stream_key": SINK_KEY},
        )
        assert response.status_code == 200, response.text

    async def publish(self, url: str) -> asyncio.subprocess.Process:
        # What OBS sends: "Server" + "/" + "Stream Key", H.264 + AAC over RTMPS.
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "50",
            "-pix_fmt", "yuv420p", "-b:v", "1500k",
            "-c:a", "aac", "-b:a", "96k", "-tls_verify", "0", "-f", "flv", url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        self.processes.append(process)
        return process

    async def rejected(self, url: str, path: str) -> bool:
        """The real MediaMTX auth callback refused the publish: FFmpeg failed and nothing went live."""
        process = await self.publish(url)
        try:
            await asyncio.wait_for(process.wait(), 25)
        except asyncio.TimeoutError:
            return False
        current = await ingest.media_path(path)
        return process.returncode != 0 and not (current and current.ready)

    async def rtmps_publishers(self, path: str) -> list[dict]:
        response = await self.client.get(f"{settings.mediamtx_api_base}/v3/rtmpsconns/list")
        response.raise_for_status()
        return [
            item for item in response.json()["items"] if item.get("path") == path and item.get("state") == "publish"
        ]

    async def kick_rtmps(self, path: str) -> None:
        for item in await self.rtmps_publishers(path):
            response = await self.client.post(f"{settings.mediamtx_api_base}/v3/rtmpsconns/kick/{item['id']}")
            response.raise_for_status()

    async def relay_health(self, room: Room) -> dict:
        await ingest.reconcile_once()
        return (await self.client.get(self.admin_url(room, "program-ingest"))).json()["health"]

    async def connect_relay(self, room: Room, destination_port: int) -> asyncio.subprocess.Process:
        _, stream_key = await self.enable(room)
        await self.set_destination(room, destination_port)
        encoder = await self.publish(f"{self.server_url(room)}/{stream_key}")
        ready = await wait_path(self.floor(room))
        assert ready is not None, (self.tmp_path / "media.log").read_text()
        assert "H264" in ready.tracks and "MPEG-4 Audio" in ready.tracks
        await ingest.reconcile_once()
        assert await wait_path(f"sink/{SINK_KEY}") is not None, "relay never reached the RTMPS sink"
        assert await eventually(lambda: self.manager.relays[str(room.id)].state == "live")
        return encoder


@pytest.fixture
async def native(tmp_path, monkeypatch):
    assert shutil.which("ffmpeg"), "FFmpeg with libx264/AAC/TLS is required"
    assert os.environ.get("MEDIAMTX_TEST_BINARY"), "Set MEDIAMTX_TEST_BINARY to a MediaMTX 1.18.2 binary"
    configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db()
    ports = free_ports("http", "api", "rtsp", "rtmps", "relay")
    for name, value in {
        "program_ingest_enabled": True,
        "mediamtx_auth_hook_secret": HOOK_SECRET,
        "program_relay_enabled": True,
        "program_relay_control_secret": CONTROL_SECRET,
        "program_relay_base": f"http://127.0.0.1:{ports['relay']}",
        "program_relay_allowed_hosts": "127.0.0.1",
        "program_rtmps_public_base": f"rtmps://127.0.0.1:{ports['rtmps']}",
        "mediamtx_api_base": f"http://127.0.0.1:{ports['api']}",
        "program_ingest_disconnect_grace_secs": 0,
        "debug": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    ingest.health.clear()
    ingest.failures.clear()
    relay.relay_health.clear()
    async with get_session() as session:
        admin = await create_user(
            session, email="relay-native@example.com", display_name="Relay", email_verified=True, is_admin=True
        )
        event = Event(slug="native", display_name="Native")
        session.add(event)
        await session.flush()
        # Captions off: proves the YouTube relay does not depend on language processing.
        rooms = [
            Room(event_id=event.id, display_name=f"Native {n}", floor_transcription_enabled=False) for n in range(2)
        ]
        session.add_all(rooms)
    cert, key = self_signed(tmp_path)
    config = tmp_path / "media.yml"
    config.write_text(f"""logLevel: info
authMethod: http
authHTTPAddress: http://127.0.0.1:{ports["http"]}/internal/media-auth?key={HOOK_SECRET}
authHTTPExclude:
  - action: api
  - action: read
  # sink/ stands in for YouTube, an external destination the portal never authorizes.
  # VoxBento paths stay behind the callback (booth paths need a ticket since #783).
  - action: publish
    path: '~^sink/'
api: yes
apiAddress: 127.0.0.1:{ports["api"]}
rtspAddress: 127.0.0.1:{ports["rtsp"]}
rtspTransports: [tcp]
rtmp: yes
rtmpEncryption: strict
rtmpsAddress: 127.0.0.1:{ports["rtmps"]}
rtmpServerKey: {key.as_posix()}
rtmpServerCert: {cert.as_posix()}
srt: no
hls: no
webrtc: no
pathDefaults:
  overridePublisher: no
paths:
  '~^[a-z0-9-]+/[0-9]+/floor$':
    overridePublisher: no
  all_others:
""")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=ports["http"], lifespan="off", log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    service = load_relay_service()
    # The local sink is self-signed; production relays always verify (see the container smoke test).
    manager = service.RelayManager(
        source_base=f"rtsp://127.0.0.1:{ports['rtsp']}",
        allowed_hosts={"127.0.0.1"},
        max_relays=2,
        tls_verify=False,
        backoff_base=0.5,
        stall_secs=STALL_SECS,
        start_timeout_secs=20.0,
        io_timeout_secs=IO_TIMEOUT_SECS,
    )
    relay_server = ThreadingHTTPServer(("127.0.0.1", ports["relay"]), service.make_handler(manager, CONTROL_SECRET))
    threading.Thread(target=relay_server.serve_forever, daemon=True).start()
    media_log = (tmp_path / "media.log").open("wb")
    media = await asyncio.create_subprocess_exec(
        os.environ["MEDIAMTX_TEST_BINARY"], str(config), stdout=media_log, stderr=media_log
    )
    stack = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            pg.shared_http_client = client
            for _ in range(100):
                with contextlib.suppress(httpx.HTTPError):
                    response = await client.get(f"{settings.mediamtx_api_base}/v3/paths/list")
                    if response.status_code == 200 and server.started:
                        break
                await asyncio.sleep(0.1)
            else:
                pytest.fail((tmp_path / "media.log").read_text())
            client.cookies.set("user_token", create_user_token(user_id=admin.id, email=admin.email, is_admin=True))
            stack = Stack(client=client, event=event, rooms=rooms, manager=manager, ports=ports, tmp_path=tmp_path)
            yield stack
    finally:
        manager.stop_all()
        relay_server.shutdown()
        relay_server.server_close()
        for process in [*(stack.processes if stack else []), media]:
            if process.returncode is None:
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


async def test_native_rtmps_publish_is_authorized_for_exactly_one_room(native):
    room, other = native.rooms
    secret, stream_key = await native.enable(room)
    other_secret, other_key = await native.enable(other)
    floor, other_floor = native.floor(room), native.floor(other)
    # Anonymous, wrong-room (both directions) and wrong-key publishes are refused by the callback.
    assert await native.rejected(f"{native.server_url(room)}/floor", floor)
    assert await native.rejected(f"{native.server_url(room)}/{other_key}", floor)
    assert await native.rejected(f"{native.server_url(other)}/{stream_key}", other_floor)
    assert await native.rejected(f"{native.server_url(room)}/floor?key=not-{secret}", floor)
    # An expired key is refused even though it is the room's current credential.
    async with get_session() as session:
        (await session.get(Room, room.id)).program_key_expires_at = ingest.utc_now() - datetime.timedelta(seconds=1)
    assert await native.rejected(f"{native.server_url(room)}/{stream_key}", floor)
    async with get_session() as session:
        (await session.get(Room, room.id)).program_key_expires_at = ingest.utc_now() + datetime.timedelta(days=1)
    # The right key on the right room publishes; the other room is untouched.
    encoder = await native.publish(f"{native.server_url(room)}/{stream_key}")
    assert await wait_path(floor) is not None, (native.tmp_path / "media.log").read_text()
    assert len(await native.rtmps_publishers(floor)) == 1
    other_encoder = await native.publish(f"{native.server_url(other)}/{other_key}")
    assert await wait_path(other_floor) is not None
    log = (native.tmp_path / "media.log").read_text()
    assert secret not in log and other_secret not in log
    assert encoder.returncode is None and other_encoder.returncode is None


async def test_native_revoke_kicks_the_active_rtmps_connection(native):
    room, other = native.rooms
    secret, stream_key = await native.enable(room)
    _, other_key = await native.enable(other)
    encoder = await native.publish(f"{native.server_url(room)}/{stream_key}")
    other_encoder = await native.publish(f"{native.server_url(other)}/{other_key}")
    assert await wait_path(native.floor(room)) is not None and await wait_path(native.floor(other)) is not None
    assert len(await native.rtmps_publishers(native.floor(room))) == 1
    response = await native.client.post(native.admin_url(room, "program-ingest"), json={"action": "revoke"})
    assert response.status_code == 200, response.text
    # The established RTMPS session is terminated by MediaMTX, not just future publishes.
    assert await wait_gone(native.floor(room))
    await asyncio.wait_for(encoder.wait(), 20)
    assert encoder.returncode != 0
    assert await native.rtmps_publishers(native.floor(room)) == []
    # The prior key cannot reconnect; the other room's publisher was never affected.
    assert await native.rejected(f"{native.server_url(room)}/{stream_key}", native.floor(room))
    assert other_encoder.returncode is None and len(await native.rtmps_publishers(native.floor(other))) == 1
    assert secret not in (native.tmp_path / "media.log").read_text()


async def test_native_relay_forwards_and_feeds_audio(native):
    room = native.rooms[0]
    await native.connect_relay(room, native.ports["rtmps"])
    relayed = await wait_path(f"sink/{SINK_KEY}")
    assert "H264" in relayed.tracks and "MPEG-4 Audio" in relayed.tracks
    assert await eventually(lambda: native.manager.relays[str(room.id)].bytes_out > 50_000)
    health = await native.relay_health(room)
    assert health["ingest"]["state"] == "connected" and health["relay"]["state"] == "live"
    assert health["processing"]["state"] == "degraded"  # captions disabled; relay unaffected
    # The same feed's audio decodes for the language pipeline.
    floor = f"rtsp://127.0.0.1:{native.ports['rtsp']}/{native.floor(room)}"
    async with FfmpegProcess(floor, "16000", "relay-native") as decoder:
        pcm = await asyncio.wait_for(decoder.stdout.readexactly(32000), 20)
        assert len(pcm) == 32000 and any(pcm)
    # Removing the YouTube destination stops the relay and the sink goes away.
    assert (await native.client.delete(native.admin_url(room, "program-relay"))).status_code == 200
    assert await wait_gone(f"sink/{SINK_KEY}")
    assert native.manager.snapshot() == {}
    assert SINK_KEY not in (native.tmp_path / "media.log").read_text().replace(f"sink/{SINK_KEY}", "")


async def test_native_relay_reports_destination_drop_and_recovers(native):
    """The destination closes an established session: state leaves live, retries, recovers."""
    room = native.rooms[0]
    await native.connect_relay(room, native.ports["rtmps"])
    current = native.manager.relays[str(room.id)]
    restarts = current.restarts
    await native.kick_rtmps(f"sink/{SINK_KEY}")
    assert await eventually(lambda: current.restarts > restarts), "relay did not notice the dropped session"
    health = await native.relay_health(room)
    assert health["relay"]["state"] != "live" or current.restarts > restarts
    assert await eventually(lambda: current.state == "live" and current.bytes_out > 0, timeout=45)
    assert await wait_path(f"sink/{SINK_KEY}") is not None
    assert (await native.relay_health(room))["relay"]["state"] == "live"


async def test_native_relay_detects_destination_that_stops_consuming(native):
    """An accepted session whose destination stops reading is not 'live' just because FFmpeg runs."""
    room = native.rooms[0]
    proxy = FreezingProxy(native.ports["rtmps"])
    port = await proxy.start()
    try:
        await native.connect_relay(room, port)
        current = native.manager.relays[str(room.id)]
        frozen_process, restarts = current.process, current.restarts
        proxy.freeze()
        # FFmpeg keeps running with blocked writes; the relay must stop reporting live.
        assert await eventually(lambda: current.state != "live", timeout=STALL_SECS + IO_TIMEOUT_SECS + 20)
        assert await eventually(lambda: current.restarts > restarts, timeout=IO_TIMEOUT_SECS + 10)
        assert frozen_process.poll() is not None
        assert current.reason in {
            "Destination stopped accepting the stream; reconnecting.",
            "Destination unreachable.",
            "TLS handshake with the destination failed.",
            "No media reached the destination; reconnecting.",
            "Relay process exited; retrying.",
            "Relaying the program feed to the destination.",
        }
        # A new session passes through the proxy: the relay reconnects and forwards again.
        assert await eventually(lambda: current.state == "live" and current.process is not frozen_process, timeout=45)
        assert (await native.relay_health(room))["relay"]["state"] == "live"
    finally:
        await proxy.close()
