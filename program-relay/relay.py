"""VoxBento program relay: the media-plane half of room-scoped RTMPS relay mode (#749).

Pulls one room's canonical floor stream from MediaMTX over private RTSP and pushes it,
unmodified (``-c copy``), to that room's configured YouTube RTMPS destination. The portal
decides which relays should exist and drives this service over an authenticated control
API on the private Docker network; media never passes through the portal process.

Stdlib only. The destination URL embeds the YouTube stream key, so it is never logged,
never returned by the API and redacted from every FFmpeg message this service keeps.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("program-relay")

RELAY_ID = re.compile(r"[1-9][0-9]{0,9}")
SOURCE_PATH = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*/[1-9][0-9]*/floor")
STREAM_KEY = re.compile(r"[A-Za-z0-9_-]{4,128}")
FINGERPRINT = re.compile(r"[0-9a-f]{16,64}")
MAX_BODY = 4096
# FFmpeg reports progress every ~0.5 s. A relay whose output bytes stop growing for this long is
# no longer forwarding (the destination stopped accepting data): kill it and reconnect.
STALL_SECS = 15.0
# Socket I/O timeout for the RTSP source and the RTMPS destination; a peer that stops
# reading or answering makes FFmpeg fail instead of blocking forever.
IO_TIMEOUT_SECS = 10.0
STALLED_REASON = "Destination stopped accepting the stream; reconnecting."
# A new run must connect to the source, probe it (up to a keyframe) and finish the TLS and
# RTMP handshakes before any media flows. That is startup, not a stall, and gets its own limit.
START_TIMEOUT_SECS = 30.0
NOT_FORWARDING_REASON = "No media reached the destination; reconnecting."
GENERIC_REASON = "Relay process exited; retrying."
BACKOFF_MAX_SECS = 30.0
# Debian's ca-certificates bundle, present in the relay image.
DEFAULT_CA_FILE = "/etc/ssl/certs/ca-certificates.crt"
TERMINATE_TIMEOUT_SECS = 3.0
# Error text is classified, never forwarded: FFmpeg can echo the destination URL (and key).
ERROR_CLASSES = (
    (re.compile(r"(?i)401|403|unauthori[sz]ed|forbidden|access denied"), "Destination rejected the stream key."),
    (
        re.compile(r"(?i)connection refused|timed out|no route|network is unreachable|name or service"),
        "Destination unreachable.",
    ),
    (re.compile(r"(?i)tls|ssl|certificate|handshake"), "TLS handshake with the destination failed."),
    (re.compile(r"(?i)rtsp|404 not found|server returned 404"), "Floor stream unavailable at the media server."),
    (
        re.compile(r"(?i)codec|could not find tag|not supported|invalid data"),
        "Feed is not H.264/AAC; relay requires that input contract.",
    ),
)


def redact(text: str, secrets_: list[str]) -> str:
    for value in secrets_:
        if value:
            text = text.replace(value, "[redacted]")
    return text


def classify(line: str) -> str:
    for pattern, reason in ERROR_CLASSES:
        if pattern.search(line):
            return reason
    return GENERIC_REASON


def validate_destination(destination: str, allowed_hosts: set[str], allow_insecure: bool = False) -> None:
    """Fail closed unless the URL is RTMPS to an allow-listed host with a plain stream key path."""
    try:
        parts = urlsplit(destination)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid destination") from None
    schemes = {"rtmps", "rtmp"} if allow_insecure else {"rtmps"}
    if parts.scheme not in schemes or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Destination must be an rtmps:// URL without credentials or query")
    if (parts.hostname or "").casefold() not in allowed_hosts:
        raise ValueError("Destination host is not allow-listed")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid destination port")
    segments = parts.path.strip("/").split("/")
    if len(segments) < 2 or not STREAM_KEY.fullmatch(segments[-1]):
        raise ValueError("Destination must end with a stream key")


@dataclass
class Relay:
    relay_id: str
    source: str
    destination: str = field(repr=False)
    fingerprint: str
    state: str = "starting"
    reason: str = "Connecting to the floor stream and destination."
    started_at: float = field(default_factory=time.time)
    restarts: int = 0
    last_progress: float = field(default_factory=time.monotonic)
    out_time_us: int = -1
    # Bytes FFmpeg has written to the destination in the current run. Growth is the evidence of
    # forwarding: a blocked or rejecting destination stops it even while FFmpeg keeps running.
    bytes_out: int = -1
    stalled: bool = False
    # Set once this run has forwarded media past the FLV header.
    forwarding: bool = False
    process: subprocess.Popen | None = field(default=None, repr=False)
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "source": self.source,
            "fingerprint": self.fingerprint,
            "state": self.state,
            "reason": self.reason,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "out_time_ms": max(self.out_time_us, 0) // 1000,
            "bytes_out": max(self.bytes_out, 0),
        }


class RelayManager:
    def __init__(
        self,
        source_base: str,
        allowed_hosts: set[str],
        max_relays: int,
        ffmpeg: list[str] | None = None,
        allow_insecure: bool = False,
        backoff_base: float = 1.0,
        ca_file: str | None = DEFAULT_CA_FILE,
        tls_verify: bool = True,
        stall_secs: float = STALL_SECS,
        start_timeout_secs: float = START_TIMEOUT_SECS,
        io_timeout_secs: float = IO_TIMEOUT_SECS,
    ):
        self.source_base = source_base.rstrip("/")
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}
        self.max_relays = max_relays
        self.ffmpeg = ffmpeg or ["ffmpeg"]
        self.allow_insecure = allow_insecure
        self.backoff_base = backoff_base
        self.ca_file = ca_file
        self.tls_verify = tls_verify
        self.stall_secs = stall_secs
        self.start_timeout_secs = start_timeout_secs
        self.io_timeout_secs = io_timeout_secs
        self.relays: dict[str, Relay] = {}
        self.lock = threading.Lock()
        # Process factory; tests substitute one that widens startup races.
        self.spawn = subprocess.Popen

    def tls_options(self) -> list[str]:
        # FFmpeg builds before 8.0 do not verify peers by default; an unverified push would
        # hand the YouTube stream key to anyone able to intercept the connection.
        if not self.tls_verify:
            return ["-tls_verify", "0"]
        return ["-tls_verify", "1", *(["-ca_file", self.ca_file] if self.ca_file else [])]

    def command(self, relay: Relay) -> list[str]:
        return [
            *self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str(int(self.io_timeout_secs * 1_000_000)),
            "-i",
            f"{self.source_base}/{relay.source}",
            # The program feed is relayed untouched: first video and first audio track only.
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            *self.tls_options(),
            "-rw_timeout",
            str(int(self.io_timeout_secs * 1_000_000)),
            "-f",
            "flv",
            "-flvflags",
            "no_duration_filesize",
            "-progress",
            "pipe:1",
            "-nostats",
            relay.destination,
        ]

    def ensure(self, relay_id: str, source: str, destination: str, fingerprint: str) -> tuple[int, dict]:
        """Idempotently run exactly one relay per room. Returns (status, public view)."""
        if not RELAY_ID.fullmatch(relay_id) or not SOURCE_PATH.fullmatch(source):
            return 400, {"error": "Invalid relay or source"}
        if not FINGERPRINT.fullmatch(fingerprint):
            return 400, {"error": "Invalid fingerprint"}
        try:
            validate_destination(destination, self.allowed_hosts, self.allow_insecure)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        with self.lock:
            current = self.relays.get(relay_id)
            if current and current.source == source and current.fingerprint == fingerprint:
                return 200, current.public()
            if current is None and len(self.relays) >= self.max_relays:
                return 503, {"error": "Relay capacity reached"}
            if current:
                self._stop_locked(current)
            relay = Relay(relay_id=relay_id, source=source, destination=destination, fingerprint=fingerprint)
            self.relays[relay_id] = relay
            relay.thread = threading.Thread(target=self._supervise, args=(relay,), daemon=True)
            relay.thread.start()
            view = relay.public()
        # The replaced supervisor may be mid-spawn; wait until it has reaped its process so no
        # old FFmpeg outlives a successful replacement (one owned process per room).
        self._join(current)
        logger.info("relay started relay_id=%s source=%s", relay_id, source)
        return 200, view

    def stop(self, relay_id: str) -> bool:
        with self.lock:
            relay = self.relays.pop(relay_id, None)
            if relay:
                self._stop_locked(relay)
        self._join(relay)
        if relay:
            logger.info("relay stopped relay_id=%s", relay_id)
        return relay is not None

    @staticmethod
    def _join(relay: Relay | None) -> None:
        if relay and relay.thread and relay.thread is not threading.current_thread():
            relay.thread.join(TERMINATE_TIMEOUT_SECS * 2 + 1)

    def stop_all(self) -> None:
        for relay_id in list(self.relays):
            self.stop(relay_id)

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return {relay_id: relay.public() for relay_id, relay in self.relays.items()}

    def _stop_locked(self, relay: Relay) -> None:
        relay.stop.set()
        relay.state, relay.reason = "stopped", "Relay stopped."
        self._terminate(relay.process)

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(TERMINATE_TIMEOUT_SECS)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    def _supervise(self, relay: Relay) -> None:
        attempt = 0
        secrets_ = [relay.destination, relay.destination.rstrip("/").rsplit("/", 1)[-1]]
        while not relay.stop.is_set():
            reason = GENERIC_REASON
            try:
                process = self.spawn(  # nosec B603 - fixed argv, validated destination, no shell
                    self.command(relay),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=os.name == "posix",
                )
            except OSError:
                relay.state, relay.reason = "failed", "FFmpeg is not available in the relay service."
                return
            with self.lock:
                if relay.stop.is_set():
                    self._terminate(process)
                    return
                relay.process = process
                # Progress is per FFmpeg run: each run reports output time and size from zero.
                relay.out_time_us = -1
                relay.bytes_out = -1
                relay.stalled = False
                relay.forwarding = False
                relay.last_progress = time.monotonic()

            def drain_errors() -> None:
                # Keep the most specific classification seen; FFmpeg's last line is often generic.
                nonlocal reason
                assert process.stderr is not None
                for line in process.stderr:
                    if line.strip():
                        kind = classify(redact(line.strip(), secrets_))
                        if kind != GENERIC_REASON:
                            reason = kind

            def watchdog() -> None:
                # A destination that stops reading blocks FFmpeg's writes: bytes stop growing
                # although the process is alive. Treat that as not forwarding and reconnect.
                while process.poll() is None and not relay.stop.wait(0.25):
                    limit = self.stall_secs if relay.forwarding else self.start_timeout_secs
                    if time.monotonic() - relay.last_progress > limit:
                        relay.stalled = True
                        relay.state = "stalled"
                        relay.reason = STALLED_REASON if relay.forwarding else NOT_FORWARDING_REASON
                        self._terminate(process)
                        return

            errors = threading.Thread(target=drain_errors, daemon=True)
            errors.start()
            threading.Thread(target=watchdog, daemon=True).start()
            assert process.stdout is not None
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    relay.out_time_us = max(relay.out_time_us, int(value))
                elif key == "total_size" and value.isdigit() and int(value) > relay.bytes_out:
                    first = relay.bytes_out < 0
                    relay.bytes_out = int(value)
                    # The first report is the FLV header; forwarding needs bytes after it.
                    if not first:
                        relay.forwarding = True
                        relay.last_progress = time.monotonic()
                        if relay.state != "live" and not relay.stalled:
                            relay.state, relay.reason = "live", "Relaying the program feed to the destination."
                            attempt = 0
            process.wait()
            errors.join(1)
            if relay.stop.is_set():
                return
            attempt += 1
            relay.restarts += 1
            if relay.stalled:
                reason = STALLED_REASON if relay.forwarding else NOT_FORWARDING_REASON
            relay.state, relay.reason = "retrying", reason
            logger.warning(
                "relay exited relay_id=%s restarts=%s reason=%s", relay.relay_id, relay.restarts, relay.reason
            )
            relay.stop.wait(min(BACKOFF_MAX_SECS, self.backoff_base * 2 ** min(attempt - 1, 5)))


def make_handler(manager: RelayManager, control_secret: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "voxbento-program-relay"

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            # Default access logs would include nothing secret, but stay quiet like the portal hook.
            return

        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self) -> bool:
            presented = self.headers.get("Authorization", "")
            expected = f"Bearer {control_secret}"
            if not control_secret or not hmac.compare_digest(presented.encode(), expected.encode()):
                self._send(401, {"error": "Unauthorized"})
                return False
            return True

        def _relay_id(self) -> str | None:
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if len(parts) == 2 and parts[0] == "relays" and RELAY_ID.fullmatch(parts[1]):
                return parts[1]
            return None

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/healthz":
                self._send(200, {"ok": True})
                return
            if not self._authorized():
                return
            if self.path.split("?", 1)[0].rstrip("/") == "/relays":
                self._send(200, {"relays": manager.snapshot(), "capacity": manager.max_relays})
                return
            self._send(404, {"error": "Not found"})

        def do_PUT(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            relay_id = self._relay_id()
            if relay_id is None:
                self._send(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = MAX_BODY + 1
            if not 0 < length <= MAX_BODY:
                self._send(413, {"error": "Invalid body size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                source, destination, fingerprint = body["source"], body["destination"], body["fingerprint"]
                if not all(isinstance(value, str) for value in (source, destination, fingerprint)):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                self._send(400, {"error": "Invalid body"})
                return
            status, view = manager.ensure(relay_id, source, destination, fingerprint)
            self._send(status, view)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            relay_id = self._relay_id()
            if relay_id is None:
                self._send(404, {"error": "Not found"})
                return
            self._send(200, {"stopped": manager.stop(relay_id)})

    return Handler


def main() -> None:
    control_secret = os.environ.get("RELAY_CONTROL_SECRET", "")
    if len(control_secret) < 32:
        raise SystemExit("RELAY_CONTROL_SECRET must be set to at least 32 characters (openssl rand -hex 32).")
    hosts = {
        host.strip()
        for host in os.environ.get("RELAY_ALLOWED_HOSTS", "a.rtmps.youtube.com,b.rtmps.youtube.com").split(",")
        if host.strip()
    }
    manager = RelayManager(
        source_base=os.environ.get("RELAY_SOURCE_BASE", "rtsp://mediamtx:8554"),
        allowed_hosts=hosts,
        max_relays=int(os.environ.get("RELAY_MAX", "4")),
        ca_file=os.environ.get("RELAY_CA_FILE", DEFAULT_CA_FILE) or None,
    )
    bind = ("0.0.0.0", int(os.environ.get("RELAY_PORT", "8090")))  # nosec B104 - private Docker network only
    server = ThreadingHTTPServer(bind, make_handler(manager, control_secret))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    logger.info("program relay listening max_relays=%s", manager.max_relays)
    try:
        server.serve_forever()
    finally:
        manager.stop_all()


if __name__ == "__main__":
    main()
