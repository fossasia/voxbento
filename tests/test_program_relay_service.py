"""program-relay media-plane service (#749): control API auth, lifecycle, capacity and redaction.

Uses a stand-in FFmpeg so it runs everywhere; tests/test_program_relay_media.py is the opt-in
real-media run against MediaMTX and a local RTMPS sink.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "program_relay_service", Path(__file__).parents[1] / "program-relay" / "relay.py"
)
service = importlib.util.module_from_spec(SPEC)
# Dataclasses resolve postponed annotations through sys.modules.
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

SECRET = "s" * 64
KEY = "abcd-efgh-ijkl-mnop-qrst"
DESTINATION = f"rtmps://a.rtmps.youtube.com/live2/{KEY}"
FAKE_FFMPEG = """
import sys, time
destination = sys.argv[-1]
if "reject" in destination:
    sys.stderr.write(f"[flv] Error opening output {destination}: Server returned 403 Forbidden\\n")
    sys.exit(1)
out = 0
while True:
    out += 50000
    print(f"out_time_us={out}", flush=True)
    print("progress=continue", flush=True)
    time.sleep(0.05)
"""


@pytest.fixture
def relay_service(tmp_path):
    fake = tmp_path / "fake_ffmpeg.py"
    fake.write_text(FAKE_FFMPEG)
    manager = service.RelayManager(
        source_base="rtsp://mediamtx:8554",
        allowed_hosts={"a.rtmps.youtube.com", "b.rtmps.youtube.com"},
        max_relays=1,
        ffmpeg=[sys.executable, str(fake)],
        backoff_base=0.05,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(manager, SECRET))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        headers={"Authorization": f"Bearer {SECRET}"},
        timeout=5,
    ) as client:
        yield client, manager
    manager.stop_all()
    server.shutdown()
    server.server_close()


def body(relay_id_source="relay-test/7/floor", destination=DESTINATION, fingerprint="a" * 32):
    return {"source": relay_id_source, "destination": destination, "fingerprint": fingerprint}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_destination_validation_fails_closed():
    allowed = {"a.rtmps.youtube.com"}
    service.validate_destination(DESTINATION, allowed)
    for destination in [
        f"rtmp://a.rtmps.youtube.com/live2/{KEY}",
        f"rtmps://evil.example/live2/{KEY}",
        f"rtmps://u:p@a.rtmps.youtube.com/live2/{KEY}",
        f"rtmps://a.rtmps.youtube.com/live2/{KEY}?x=1",
        "rtmps://a.rtmps.youtube.com/live2",
        f"rtmps://a.rtmps.youtube.com/{KEY}/../x",
        "file:///etc/passwd",
    ]:
        with pytest.raises(ValueError):
            service.validate_destination(destination, allowed)
    # Loopback sinks are only for the opt-in integration test.
    with pytest.raises(ValueError):
        service.validate_destination(f"rtmp://127.0.0.1:1935/sink/{KEY}", {"127.0.0.1"})
    service.validate_destination(f"rtmp://127.0.0.1:1935/sink/{KEY}", {"127.0.0.1"}, allow_insecure=True)


def test_command_relays_untouched_first_video_and_audio(relay_service):
    _, manager = relay_service
    relay = service.Relay(relay_id="7", source="relay-test/7/floor", destination=DESTINATION, fingerprint="a" * 32)
    argv = manager.command(relay)
    assert argv[argv.index("-i") + 1] == "rtsp://mediamtx:8554/relay-test/7/floor"
    assert argv[argv.index("-c") + 1] == "copy" and argv[argv.index("-f") + 1] == "flv"
    assert ["-map", "0:v:0", "-map", "0:a:0"] == argv[argv.index("-map") : argv.index("-map") + 4]
    assert argv[-1] == DESTINATION and "-rtsp_transport" in argv
    # TLS to the destination is always verified against the CA bundle (YouTube key protection).
    assert argv[argv.index("-tls_verify") + 1] == "1"
    assert argv[argv.index("-ca_file") + 1] == service.DEFAULT_CA_FILE
    assert argv.index("-tls_verify") < argv.index(DESTINATION)
    assert KEY not in repr(relay)


def test_control_api_requires_shared_secret(relay_service):
    client, _ = relay_service
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 200
    assert client.get("/relays", headers={"Authorization": ""}).status_code == 401
    assert client.get("/relays", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.put("/relays/7", json=body(), headers={"Authorization": f"Bearer {SECRET}x"}).status_code == 401
    assert client.delete("/relays/7", headers={"Authorization": ""}).status_code == 401


def test_lifecycle_idempotent_restart_capacity_and_stop(relay_service):
    client, manager = relay_service
    for invalid in [
        body(destination=f"rtmps://evil.example/live2/{KEY}"),
        body(relay_id_source="../../etc/floor"),
        body(fingerprint="not-hex"),
    ]:
        assert client.put("/relays/7", json=invalid).status_code == 400
    assert client.put("/relays/0", json=body()).status_code == 404
    assert client.put("/relays/7", json=body()).status_code == 200
    assert wait_for(lambda: client.get("/relays").json()["relays"]["7"]["state"] == "live")
    listing = client.get("/relays")
    assert KEY not in listing.text and '"destination"' not in listing.text
    first_process = manager.relays["7"].process
    # Same desired state: no restart. New destination: exactly one replacement process.
    assert client.put("/relays/7", json=body()).status_code == 200
    assert manager.relays["7"].process is first_process
    assert (
        client.put(
            "/relays/7", json=body(destination=DESTINATION.replace(KEY, "new-key-1234"), fingerprint="b" * 32)
        ).status_code
        == 200
    )
    assert first_process.poll() is not None
    assert wait_for(lambda: manager.relays["7"].state == "live")
    # Capacity is per service and fails closed for a new room.
    assert client.put("/relays/8", json=body("relay-test/8/floor")).status_code == 503
    process = manager.relays["7"].process
    assert client.delete("/relays/7").json() == {"stopped": True}
    assert process.poll() is not None and client.get("/relays").json()["relays"] == {}
    assert client.delete("/relays/7").json() == {"stopped": False}


def test_rejected_destination_is_classified_and_redacted(relay_service, caplog):
    client, manager = relay_service
    caplog.set_level(logging.INFO)
    rejecting = f"rtmps://a.rtmps.youtube.com/live2/reject-{KEY}"
    assert client.put("/relays/7", json=body(destination=rejecting)).status_code == 200
    assert wait_for(lambda: manager.relays["7"].restarts >= 2)
    view = client.get("/relays").json()["relays"]["7"]
    assert view["state"] == "retrying"
    assert view["reason"] == "Destination rejected the stream key."
    assert KEY not in caplog.text and "reject-" not in caplog.text
    assert service.redact(f"error {rejecting}", [rejecting]) == "error [redacted]"
