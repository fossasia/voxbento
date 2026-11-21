"""Tests for YouTube relay failure classification and retry hardening (#750).

Verifies fatal vs transient error classification, retry loop halting on fatal errors,
backoff on transient errors, and stream key/secret redaction.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "program_relay_service_failures", Path(__file__).parents[1] / "program-relay" / "relay.py"
)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

KEY = "test-secret-stream-key-1234"
DEST_FATAL_AUTH = f"rtmps://a.rtmps.youtube.com/live2/fatal-auth-{KEY}"
DEST_FATAL_ENDED = f"rtmps://a.rtmps.youtube.com/live2/fatal-ended-{KEY}"
DEST_FATAL_PROGRESS = f"rtmps://a.rtmps.youtube.com/live2/fatal-progress-{KEY}"
DEST_TRANSIENT = f"rtmps://a.rtmps.youtube.com/live2/transient-{KEY}"

FAKE_FFMPEG_SCRIPT = """
import sys, time
destination = sys.argv[-1]

if "fatal-auth" in destination:
    sys.stderr.write(f"[flv] Error opening output {destination}: Server returned 401 Unauthorized\\n")
    sys.exit(1)
elif "fatal-ended" in destination:
    sys.stderr.write(f"[flv] Error output {destination}: broadcast complete, stream ended\\n")
    sys.exit(1)
elif "fatal-progress" in destination:
    sys.stderr.write("[flv] Error opening output: 401 Unauthorized stream key invalid\\n")
    sys.stdout.write("out_time_us=1000000\\ntotal_size=1024\\ntotal_size=2048\\n")
    sys.stdout.flush()
    time.sleep(0.05)
    sys.exit(1)
elif "transient" in destination:
    sys.stderr.write(f"[flv] Error output {destination}: Connection refused: network is unreachable\\n")
    sys.exit(1)
else:
    sys.stderr.write("Unknown error occurred\\n")
    sys.exit(1)
"""


@pytest.fixture
def fake_ffmpeg(tmp_path):
    script = tmp_path / "fake_ffmpeg_failures.py"
    script.write_text(FAKE_FFMPEG_SCRIPT)
    return str(script)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestClassifyUnit:
    """Unit tests for classify() covering fatal, transient, and generic error patterns."""

    @pytest.mark.parametrize(
        "line",
        [
            "HTTP 401 Unauthorized",
            "Server returned 403 Forbidden",
            "access denied to destination",
            "unauthorised access",
            "stream key invalid: rejected",
            "stream_key_invalid",
        ],
    )
    def test_fatal_auth_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is True
        assert reason == "Destination rejected the stream key — update the YouTube stream key."

    @pytest.mark.parametrize(
        "line",
        [
            "stream ended by remote host",
            "broadcast complete; stream terminated",
            "already ended",
            "YouTube broadcast already ended",
        ],
    )
    def test_fatal_ended_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is True
        assert reason == "YouTube broadcast has already ended — create a new live stream."

    @pytest.mark.parametrize(
        "line",
        [
            "connection refused",
            "connection timed out",
            "no route to host",
            "network is unreachable",
            "name or service not known",
        ],
    )
    def test_transient_unreachable_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is False
        assert reason == "Destination temporarily unreachable."

    @pytest.mark.parametrize(
        "line",
        [
            "tls handshake failed",
            "ssl handshake failure",
            "certificate verify failed",
        ],
    )
    def test_transient_tls_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is False
        assert reason == "TLS handshake with the destination failed."

    @pytest.mark.parametrize(
        "line",
        [
            "broken pipe",
            "connection reset by peer",
            "unexpected eof while reading",
            "[tls @ 0x7f403123] Connection reset by peer",
            "[tls @ 0x7f401abc] broken pipe",
        ],
    )
    def test_transient_peer_drop_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is False
        assert reason == "Connection dropped by peer."

    @pytest.mark.parametrize(
        "line",
        [
            "rtsp 404 not found",
            "server returned 404",
        ],
    )
    def test_transient_floor_unavailable_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is False
        assert reason == "Floor stream unavailable at the media server."

    @pytest.mark.parametrize(
        "line",
        [
            "codec not supported",
            "could not find tag for codec",
            "invalid data found when processing input",
        ],
    )
    def test_transient_codec_errors(self, line: str):
        reason, is_fatal = service.classify(line)
        assert is_fatal is False
        assert reason == "Feed is not H.264/AAC; relay requires that input contract."

    def test_generic_unknown_error(self):
        reason, is_fatal = service.classify("Some unrecognized ffmpeg error message")
        assert is_fatal is False
        assert reason == service.GENERIC_REASON


class TestRelayLifecycle:
    """RelayManager lifecycle tests verifying fatal vs transient behavior."""

    def test_fatal_error_halts_retries_immediately(self, fake_ffmpeg):
        manager = service.RelayManager(
            source_base="rtsp://mediamtx:8554",
            allowed_hosts={"a.rtmps.youtube.com"},
            max_relays=2,
            ffmpeg=[sys.executable, fake_ffmpeg],
            backoff_base=0.05,
        )
        try:
            status, view = manager.ensure(
                relay_id="101",
                source="test/101/floor",
                destination=DEST_FATAL_AUTH,
                fingerprint="a" * 32,
            )
            assert status == 200
            relay = manager.relays["101"]

            # Must transition to failed state immediately without restarting
            assert wait_for(lambda: relay.state == "failed")
            assert relay.reason == "Destination rejected the stream key — update the YouTube stream key."
            assert relay.restarts == 0
            assert relay.stop.is_set()

            # Ensure process was terminated and does not retry after waiting
            time.sleep(0.3)
            assert relay.restarts == 0
            assert relay.state == "failed"
            assert relay.process is not None and relay.process.poll() is not None
        finally:
            manager.stop_all()

    def test_fatal_broadcast_ended_halts_retries(self, fake_ffmpeg):
        manager = service.RelayManager(
            source_base="rtsp://mediamtx:8554",
            allowed_hosts={"a.rtmps.youtube.com"},
            max_relays=2,
            ffmpeg=[sys.executable, fake_ffmpeg],
            backoff_base=0.05,
        )
        try:
            status, view = manager.ensure(
                relay_id="102",
                source="test/102/floor",
                destination=DEST_FATAL_ENDED,
                fingerprint="b" * 32,
            )
            assert status == 200
            relay = manager.relays["102"]

            assert wait_for(lambda: relay.state == "failed")
            assert relay.reason == "YouTube broadcast has already ended — create a new live stream."
            assert relay.restarts == 0
            assert relay.stop.is_set()

            time.sleep(0.3)
            assert relay.restarts == 0
            assert relay.state == "failed"
        finally:
            manager.stop_all()

    def test_fatal_error_with_trailing_stdout_progress_retains_failed_state(self, fake_ffmpeg):
        manager = service.RelayManager(
            source_base="rtsp://mediamtx:8554",
            allowed_hosts={"a.rtmps.youtube.com"},
            max_relays=2,
            ffmpeg=[sys.executable, fake_ffmpeg],
            backoff_base=0.05,
        )
        try:
            status, view = manager.ensure(
                relay_id="109",
                source="test/109/floor",
                destination=DEST_FATAL_PROGRESS,
                fingerprint="f" * 32,
            )
            assert status == 200
            relay = manager.relays["109"]

            assert wait_for(lambda: relay.state == "failed")
            assert relay.stop.is_set()
            time.sleep(0.2)
            # Must remain failed, not overwritten by live from trailing stdout progress
            assert relay.state == "failed"
            assert relay.reason == "Destination rejected the stream key — update the YouTube stream key."
        finally:
            manager.stop_all()

    def test_transient_error_retries_with_backoff(self, fake_ffmpeg):
        manager = service.RelayManager(
            source_base="rtsp://mediamtx:8554",
            allowed_hosts={"a.rtmps.youtube.com"},
            max_relays=2,
            ffmpeg=[sys.executable, fake_ffmpeg],
            backoff_base=0.05,
        )
        try:
            status, view = manager.ensure(
                relay_id="103",
                source="test/103/floor",
                destination=DEST_TRANSIENT,
                fingerprint="c" * 32,
            )
            assert status == 200
            relay = manager.relays["103"]

            # Must retry on transient error
            assert wait_for(lambda: relay.restarts >= 2)
            assert relay.state == "retrying"
            assert relay.reason == "Destination temporarily unreachable."
        finally:
            manager.stop_all()


class TestSecretRedaction:
    """Verifies that secrets (stream keys and URLs) are redacted and never logged."""

    def test_redact_function(self):
        secret_key = "very-confidential-stream-key"
        secret_dest = f"rtmps://a.rtmps.youtube.com/live2/{secret_key}"
        raw = f"ffmpeg error while connecting to {secret_dest}: invalid stream key {secret_key}"

        redacted = service.redact(raw, [secret_dest, secret_key])
        assert secret_key not in redacted
        assert secret_dest not in redacted
        assert "[redacted]" in redacted
        assert redacted == "ffmpeg error while connecting to [redacted]: invalid stream key [redacted]"

    def test_secrets_never_logged_during_relay_failure(self, fake_ffmpeg, caplog):
        caplog.set_level(logging.DEBUG)
        manager = service.RelayManager(
            source_base="rtsp://mediamtx:8554",
            allowed_hosts={"a.rtmps.youtube.com"},
            max_relays=2,
            ffmpeg=[sys.executable, fake_ffmpeg],
            backoff_base=0.05,
        )
        try:
            status, _ = manager.ensure(
                relay_id="104",
                source="test/104/floor",
                destination=DEST_FATAL_AUTH,
                fingerprint="d" * 32,
            )
            assert status == 200
            relay = manager.relays["104"]
            assert wait_for(lambda: relay.state == "failed")

            # Secret key must not appear anywhere in logs or public output
            assert KEY not in caplog.text
            public_view = relay.public()
            assert KEY not in str(public_view)
            assert "destination" not in public_view
        finally:
            manager.stop_all()
