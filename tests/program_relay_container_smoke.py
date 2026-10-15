"""Smoke test executed inside the built program-relay image by CI (tests.yml: program-relay-smoke).

Runs with the production hardening (read-only root, tmpfs /tmp, all capabilities dropped,
no-new-privileges, USER nobody) and checks what Compose validation cannot:

- FFmpeg is present with TLS/RTMPS support and the CA bundle exists and is used.
- A relay forwards H.264/AAC to an RTMPS sink whose certificate chains to a trusted CA.
- A sink with an untrusted certificate is refused (TLS verification is enforced).
- YouTube's real RTMPS certificate verifies against the image's default CA bundle.
- Stopping relays leaves no FFmpeg push process behind.

Stdlib only. Expects the CI network: ``mtx`` (CA-signed cert) and ``mtx-untrusted``
(self-signed cert), both MediaMTX 1.18.2 with RTMPS on 1936 and the API on 9997.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

CONTROL = "http://127.0.0.1:8090"
SECRET = os.environ["RELAY_CONTROL_SECRET"]
DEFAULT_CA = "/etc/ssl/certs/ca-certificates.crt"
SINK_KEY, UNTRUSTED_KEY = "smoke-key-0001", "smoke-key-0002"
TLS_FAILURE = re.compile(r"(?i)certificate|verif|not trusted|issuer|handshake")


def request(method: str, url: str, body: dict | None = None, auth: bool = True) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {SECRET}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except (OSError, ValueError, KeyError):
            value = None
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def path_ready(host: str, path: str) -> dict | None:
    status, body = request("GET", f"http://{host}:9997/v3/paths/get/{path}", auth=False)
    return body if status == 200 and body.get("ready") else None


def relay(relay_id: str) -> dict:
    return request("GET", f"{CONTROL}/relays")[1]["relays"].get(relay_id, {})


def check_hardening() -> None:
    assert os.getuid() != 0, "relay must not run as root"
    try:
        with open("/app/probe", "w"):
            pass
        raise AssertionError("root filesystem is writable")
    except OSError:
        pass
    with open("/tmp/probe", "w") as handle:  # tmpfs is the only writable location
        handle.write("ok")
    status = open("/proc/self/status").read()
    assert re.search(r"^CapEff:\s+0+$", status, re.M), "capabilities were not dropped"
    assert re.search(r"^NoNewPrivs:\s+1$", status, re.M), "no-new-privileges is not set"
    print("hardening: non-root, read-only root, no capabilities, no-new-privileges")


def check_ffmpeg_and_ca_bundle() -> None:
    protocols = subprocess.run(["ffmpeg", "-hide_banner", "-protocols"], capture_output=True, text=True, check=True)
    for name in ("rtmps", "tls"):
        assert re.search(rf"^\s*{name}\s*$", protocols.stdout, re.M), f"FFmpeg lacks the {name} protocol"
    # RTSP is a demuxer (the floor source), not an entry in the protocol list.
    demuxers = subprocess.run(["ffmpeg", "-hide_banner", "-demuxers"], capture_output=True, text=True, check=True)
    assert re.search(r"^\s*D\S*\s+rtsp\s", demuxers.stdout, re.M), "FFmpeg lacks the RTSP demuxer"
    assert os.path.getsize(DEFAULT_CA) > 50_000, "CA bundle missing or empty"
    # Real YouTube RTMPS endpoint: the TLS handshake must verify against the image's bundle.
    youtube = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-tls_verify", "1", "-ca_file", DEFAULT_CA,
         "-rw_timeout", "8000000", "-i", "tls://a.rtmps.youtube.com:443", "-t", "1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert not TLS_FAILURE.search(youtube.stderr), f"YouTube RTMPS certificate did not verify: {youtube.stderr}"
    assert "resolve" not in youtube.stderr.lower(), f"could not reach YouTube: {youtube.stderr}"
    # Control: the same check against a self-signed sink must fail verification.
    untrusted = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-tls_verify", "1", "-ca_file", DEFAULT_CA,
         "-rw_timeout", "8000000", "-i", "tls://mtx-untrusted:1936", "-t", "1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert untrusted.returncode != 0 and TLS_FAILURE.search(untrusted.stderr), untrusted.stderr
    print("ffmpeg: rtmps/tls present; CA bundle verifies YouTube and rejects a self-signed peer")


def check_control_api() -> None:
    with urllib.request.urlopen(f"{CONTROL}/healthz", timeout=5) as response:
        assert response.status == 200
    assert request("GET", f"{CONTROL}/relays", auth=False)[0] == 401
    assert request("GET", f"{CONTROL}/relays")[0] == 200
    print("control api: healthz open, relays require the bearer secret")


def push_processes() -> list[str]:
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmdline = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "rtmps://" in cmdline:
            found.append(cmdline)
    return found


def main() -> None:
    check_hardening()
    check_ffmpeg_and_ca_bundle()
    check_control_api()
    publisher = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "50", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-f", "rtsp", "-rtsp_transport", "tcp", "rtsp://mtx:8554/smoke/1/floor"],
    )  # fmt: skip
    try:
        wait(lambda: path_ready("mtx", "smoke/1/floor"), 30, "the floor source")
        trusted = {
            "source": "smoke/1/floor",
            "destination": f"rtmps://mtx:1936/sink/{SINK_KEY}",
            "fingerprint": "a" * 32,
        }
        assert request("PUT", f"{CONTROL}/relays/1", trusted)[0] == 200
        wait(lambda: relay("1").get("state") == "live" and relay("1")["bytes_out"] > 0, 45, "a live verified relay")
        sink = wait(lambda: path_ready("mtx", f"sink/{SINK_KEY}"), 30, "the relayed sink stream")
        codecs = {track.get("codec") for track in sink.get("tracks2", [])} | set(sink.get("tracks", []))
        assert {"H264", "MPEG-4 Audio"} <= codecs, codecs
        print("relay: forwarded H.264/AAC over verified RTMPS to a CA-signed sink")

        untrusted = {
            "source": "smoke/1/floor",
            "destination": f"rtmps://mtx-untrusted:1936/sink/{UNTRUSTED_KEY}",
            "fingerprint": "b" * 32,
        }
        assert request("PUT", f"{CONTROL}/relays/2", untrusted)[0] == 200
        wait(lambda: relay("2").get("restarts", 0) >= 1, 45, "the untrusted relay to fail")
        view = relay("2")
        assert view["state"] in {"retrying", "starting"} and view["state"] != "live", view
        assert view["reason"] == "TLS handshake with the destination failed.", view
        assert path_ready("mtx-untrusted", f"sink/{UNTRUSTED_KEY}") is None
        listing = json.dumps(request("GET", f"{CONTROL}/relays")[1])
        assert SINK_KEY not in listing and UNTRUSTED_KEY not in listing
        print("relay: untrusted certificate refused and classified; keys never listed")

        for relay_id in ("1", "2"):
            assert request("DELETE", f"{CONTROL}/relays/{relay_id}")[1] == {"stopped": True}
        assert request("GET", f"{CONTROL}/relays")[1]["relays"] == {}
        wait(lambda: not push_processes(), 10, "relay FFmpeg processes to exit")
        print("lifecycle: stop leaves no push process")
    finally:
        publisher.terminate()
        publisher.wait(10)
    print("program-relay container smoke test passed")


if __name__ == "__main__":
    main()
