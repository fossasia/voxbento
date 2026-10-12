"""Exercise the running floor-bot image, including its real capture subprocess.

Run inside the container after its normal entrypoint has started:
    python /tmp/floor_bot_container_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class AudioPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""<!doctype html><title>Floor audio fixture</title>
<script>
const context = new AudioContext();
const oscillator = context.createOscillator();
oscillator.frequency.value = 440;
oscillator.connect(context.destination);
oscillator.start();
context.resume();
</script>""")


def bot_request(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8080" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def main() -> None:
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
    try:
        result = bot_request("/start", {
            **room,
            "jitsi_url": "http://127.0.0.1:8090/",
            "mediamtx_rtsp_base": "rtsp://127.0.0.1:1",
        })
        assert result["status"] == "started", result
        deadline = time.monotonic() + 45
        while True:
            status = bot_request("/status")["active_rooms"].get("ci-audio-1")
            assert status and status["state"] != "dead", f"Capture subprocess failed: {status}"
            if status["stage"] == "in_meeting":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Chromium never reached the audio page: {status}")
            time.sleep(0.5)

        # Capture the fixture tone from the actual browser's PulseAudio sink.
        audio = subprocess.run(  # nosec B603 B607
            ["ffmpeg", "-v", "error", "-f", "pulse", "-i", "sink_ci-audio_1.monitor",
             "-t", "1", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            env={**os.environ, "PULSE_SERVER": "unix:/tmp/pulse-ci-audio-1.sock"},
            capture_output=True, check=True, timeout=10,
        ).stdout
        assert len(audio) >= 16000, "FFmpeg did not capture audio from PulseAudio"
        assert any(audio), "Chromium produced only silence; floor audio capture is broken"
        print("Floor-bot entrypoint, Chromium launch, PulseAudio and FFmpeg capture passed")
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
