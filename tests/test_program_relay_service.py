"""program-relay media-plane service (#749): control API auth, lifecycle, capacity and redaction.

Uses a stand-in FFmpeg so it runs everywhere; tests/test_program_relay_media.py is the opt-in
real-media run against MediaMTX and a local RTMPS sink.
"""

from __future__ import annotations

import importlib.util
import logging
import random
import subprocess
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


def report(out, size):
    # Same shape as FFmpeg -progress: output time and bytes written to the destination.
    print(f"out_time_us={out}", flush=True)
    print(f"total_size={size}", flush=True)
    print("progress=continue", flush=True)


if "reject" in destination:
    # The specific cause is not FFmpeg's last line; classification must still find it.
    sys.stderr.write(f"[flv] Error opening output {destination}: Server returned 403 Forbidden\\n")
    sys.stderr.write("Error opening output files: Input/output error\\n")
    sys.exit(1)
if "flaky" in destination:
    # A short-lived push, as when the destination drops the connection.
    for n in range(1, 5):
        report(n * 50000, 13 + n * 4096)
        time.sleep(0.05)
    sys.exit(1)
if "nobytes" in destination:
    # FFmpeg runs and its clock advances, but nothing reaches the destination after the header.
    out = 0
    while True:
        out += 50000
        report(out, 13)
        time.sleep(0.05)
if "frozen" in destination:
    # Forwards briefly, then the destination stops reading: writes block, progress stops.
    for n in range(1, 8):
        report(n * 50000, 13 + n * 4096)
        time.sleep(0.05)
    time.sleep(3600)
out, size = 0, 13
while True:
    out += 50000
    size += 4096
    report(out, size)
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
        stall_secs=1.0,
        start_timeout_secs=1.5,
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
    # Neither a silent source nor a destination that stops reading can block FFmpeg forever.
    assert argv[argv.index("-timeout") + 1] == "10000000" and argv.index("-timeout") < argv.index("-i")
    assert argv[argv.index("-rw_timeout") + 1] == "10000000" and argv.index("-rw_timeout") > argv.index("-i")
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


def test_restarted_relay_reports_live_again(relay_service):
    """Progress is per FFmpeg run: a restart must not wait to exceed the previous run's output time."""
    client, manager = relay_service
    flaky = f"rtmps://a.rtmps.youtube.com/live2/flaky-{KEY}"
    assert client.put("/relays/7", json=body(destination=flaky)).status_code == 200
    assert wait_for(lambda: manager.relays["7"].restarts >= 2)
    assert wait_for(lambda: manager.relays["7"].state == "live", timeout=3)


class Spawner:
    """Records every FFmpeg process and can widen the window between spawn and ownership."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.processes: list[subprocess.Popen] = []
        self.lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        with self.lock:
            self.processes.append(process)
        time.sleep(self.delay)
        return process

    def alive(self) -> list[subprocess.Popen]:
        with self.lock:
            return [process for process in self.processes if process.poll() is None]


def test_live_requires_bytes_reaching_the_destination(relay_service):
    """A running FFmpeg whose clock advances is not forwarding until output bytes grow."""
    client, manager = relay_service
    states = set()
    assert client.put("/relays/7", json=body(destination=DESTINATION.replace(KEY, f"nobytes-{KEY}"))).status_code == 200
    assert wait_for(
        lambda: states.add(client.get("/relays").json()["relays"]["7"]["state"]) or manager.relays["7"].restarts >= 1
    )
    assert "live" not in states
    view = client.get("/relays").json()["relays"]["7"]
    assert view["reason"] == service.NOT_FORWARDING_REASON and view["bytes_out"] <= 13


def test_destination_that_stops_consuming_is_detected_and_reconnected(relay_service):
    client, manager = relay_service
    assert client.put("/relays/7", json=body(destination=DESTINATION.replace(KEY, f"frozen-{KEY}"))).status_code == 200
    assert wait_for(lambda: manager.relays["7"].state == "live")
    first = manager.relays["7"].process
    # Progress stops while the process is still alive; the watchdog must kill and retry it.
    assert wait_for(lambda: manager.relays["7"].restarts >= 1, timeout=6)
    assert first.poll() is not None
    assert manager.relays["7"].reason in {service.STALLED_REASON, "Relaying the program feed to the destination."}
    # The reconnect forwards again and is reported live, then stalls again.
    assert wait_for(lambda: manager.relays["7"].state == "live" and manager.relays["7"].process is not first)


def test_rejected_destination_classified_from_any_stderr_line(relay_service):
    client, manager = relay_service
    assert client.put("/relays/7", json=body(destination=DESTINATION.replace(KEY, f"reject-{KEY}"))).status_code == 200
    assert wait_for(lambda: manager.relays["7"].restarts >= 1)
    assert manager.relays["7"].reason == "Destination rejected the stream key."


def test_stop_during_process_startup_leaves_no_process(relay_service):
    _, manager = relay_service
    spawner = Spawner(delay=0.6)
    manager.spawn = spawner
    assert manager.ensure("7", "relay-test/7/floor", DESTINATION, "a" * 32)[0] == 200
    # FFmpeg exists but the supervisor has not taken ownership of it yet.
    assert wait_for(lambda: len(spawner.processes) == 1)
    assert manager.stop("7") is True
    assert spawner.alive() == []
    assert manager.snapshot() == {}


def test_destination_change_during_startup_replaces_exactly_one_process(relay_service):
    _, manager = relay_service
    spawner = Spawner(delay=0.6)
    manager.spawn = spawner
    manager.ensure("7", "relay-test/7/floor", DESTINATION, "a" * 32)
    assert wait_for(lambda: len(spawner.processes) == 1)
    old = spawner.processes[0]
    status, _ = manager.ensure("7", "relay-test/7/floor", DESTINATION.replace(KEY, "new-key-1234"), "b" * 32)
    assert status == 200
    # The replacement returns only after the old run is gone.
    assert old.poll() is not None
    assert wait_for(lambda: manager.relays["7"].state == "live")
    assert spawner.alive() == [manager.relays["7"].process]
    assert manager.relays["7"].fingerprint == "b" * 32


def test_overlapping_stop_and_start_keep_one_owned_process(relay_service):
    _, manager = relay_service
    spawner = Spawner(delay=0.05)
    manager.spawn = spawner
    fingerprints = ["a" * 32, "b" * 32, "c" * 32]
    errors: list[BaseException] = []

    def churn(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(12):
                if rng.random() < 0.4:
                    manager.stop("7")
                else:
                    fp = rng.choice(fingerprints)
                    manager.ensure("7", "relay-test/7/floor", DESTINATION.replace(KEY, f"key-{fp[0]}-1234"), fp)
                time.sleep(rng.random() * 0.05)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(seed,)) for seed in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors and all(not thread.is_alive() for thread in threads)
    relay = manager.relays.get("7")
    if relay is not None:
        assert wait_for(lambda: relay.process is not None and relay.state == "live")
        # Whatever won the race owns the only running FFmpeg.
        assert wait_for(lambda: spawner.alive() == [relay.process])
    else:
        assert wait_for(lambda: spawner.alive() == [])
    manager.stop("7")
    assert spawner.alive() == []


def test_slow_startup_is_not_mistaken_for_a_stall(relay_service, tmp_path):
    """Probe + TLS + RTMP handshakes can outlast the stall window; only the start timeout applies."""
    client, manager = relay_service
    slow = tmp_path / "slow_ffmpeg.py"
    slow.write_text(
        "import sys, time\n"
        "time.sleep(1.2)  # longer than stall_secs (1.0), shorter than start_timeout_secs (1.5)\n"
        "size = 13\n"
        "while True:\n"
        "    size += 4096\n"
        "    print(f'total_size={size}', flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    manager.ffmpeg = [sys.executable, str(slow)]
    assert client.put("/relays/7", json=body()).status_code == 200
    assert wait_for(lambda: manager.relays["7"].state == "live", timeout=6)
    assert manager.relays["7"].restarts == 0
