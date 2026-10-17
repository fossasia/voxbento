"""Exercise the running floor-bot image, including its real capture subprocess.

Run inside the container after its normal entrypoint has started:
    python /tmp/floor_bot_container_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class AudioPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Serve a stable tone so decoding can prove the complete media path."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        # The bot only streams after Jitsi reports a joined conference.
        self.wfile.write(b"""<!doctype html><title>Floor audio fixture</title>
<script>
window.APP = {conference: {isJoined: () => true}};
const context = new AudioContext();
const oscillator = context.createOscillator();
oscillator.frequency.value = 440;
oscillator.connect(context.destination);
oscillator.start();
context.resume();
</script>""")


def bot_request(path: str, payload: dict | None = None) -> dict:
    """Call the production bot HTTP API with a bounded network wait."""
    request = urllib.request.Request(
        "http://127.0.0.1:8080" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def assert_fixture_tone(audio: bytes) -> None:
    """Require sustained 440 Hz energy, rejecting silence, spikes and other audio."""
    samples = [sample[0] for sample in struct.iter_unpack("<h", audio)]
    window_size = 4000  # 250 ms at the decoded 16 kHz sample rate
    window_count = len(samples) // window_size
    assert window_count >= 4, "RTSP did not deliver at least one second of decoded audio"
    coefficient = 2 * math.cos(2 * math.pi * 440 / 16000)
    tone_windows = 0
    for start in range(0, window_count * window_size, window_size):
        window = samples[start:start + window_size]
        energy = sum(sample * sample for sample in window)
        previous = previous_previous = 0.0
        for sample in window:
            current = sample + coefficient * previous - previous_previous
            previous_previous, previous = previous, current
        tone_energy = previous * previous + previous_previous * previous_previous - coefficient * previous * previous_previous
        rms = math.sqrt(energy / window_size) / 32768
        tone_fraction = 2 * tone_energy / (window_size * energy) if energy else 0
        if rms >= 0.02 and tone_fraction >= 0.7:
            tone_windows += 1
    assert tone_windows >= math.ceil(0.75 * window_count), (
        f"Expected sustained 440 Hz fixture tone; found it in {tone_windows}/{window_count} windows"
    )


def capture_published_audio(rtsp_url: str) -> bytes:
    """Read the bot's production Opus/RTSP publisher rather than its local sink."""
    deadline = time.monotonic() + 30
    failure_detail = "No RTSP read attempt completed"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = subprocess.run(  # nosec B603 B607
                ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", rtsp_url,
                 "-t", "2", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                capture_output=True, timeout=min(15, remaining),
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            failure_detail = f"FFmpeg RTSP read timed out after {exc.timeout:g} seconds"
            if stderr:
                failure_detail += f": {stderr}"
        else:
            if result.returncode == 0:
                return result.stdout
            failure_detail = result.stderr.decode(errors="replace")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    raise AssertionError("Production FFmpeg never published readable RTSP audio: " + failure_detail)


def main() -> None:
    """Verify real room startup, published audio and shutdown in the built image."""
    deadline = time.monotonic() + 60
    while True:
        try:
            bot_request("/status")
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise TimeoutError("Floor-bot entrypoint never became ready") from None
            time.sleep(1)

    fixture = HTTPServer(("127.0.0.1", 8090), AudioPageHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    room = {"event_slug": "ci-audio", "room_id": 1}
    rtsp_base = os.environ.get("SMOKE_MEDIAMTX_RTSP_BASE", "rtsp://floor-bot-mtx:8554")
    try:
        result = bot_request("/start", {
            **room,
            "jitsi_url": "http://127.0.0.1:8090/",
            "mediamtx_rtsp_base": rtsp_base,
        })
        assert result["status"] == "started", result
        deadline = time.monotonic() + 45
        while True:
            status = bot_request("/status")["active_rooms"].get("ci-audio-1")
            assert status and status["state"] != "dead", f"Capture subprocess failed: {status}"
            if status["stage"] == "in_meeting":
                assert (status.get("event_slug"), status.get("room_id")) == ("ci-audio", 1), status
                expected = hashlib.sha256(b"http://127.0.0.1:8090/").hexdigest()[:16]
                assert status.get("url_fingerprint") == expected, status
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Chromium never reached the audio page: {status}")
            time.sleep(0.5)

        audio = capture_published_audio(f"{rtsp_base}/ci-audio/1/floor")
        assert_fixture_tone(audio)
        print("Floor-bot Chromium, PulseAudio, production Opus/RTSP publishing and tone verification passed")
    finally:
        try:
            bot_request("/stop", room)
            status = bot_request("/status")["active_rooms"]
            assert "ci-audio-1" not in status, status
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
