from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
from collections import deque

from portal.transcription.providers.base import (
    BoothTranscriptionState,
    FatalTranscriptionError,
    ProviderConfig,
    TranscriptionProvider,
)

logger = logging.getLogger(__name__)

SARVAM_REALTIME_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
SARVAM_DEFAULT_MODEL = "saaras:v3-realtime"
SARVAM_ALLOWED_MODELS = frozenset({SARVAM_DEFAULT_MODEL})
SARVAM_SAMPLE_RATE = 16000
AUDIO_CHUNK_BYTES = 4096
PING_INTERVAL_SECONDS = 15.0
SESSION_BEGIN_TIMEOUT_SECONDS = 5.0
STABLE_SESSION_SECONDS = 10.0
EOF_DRAIN_TIMEOUT_SECONDS = 5.0
MAX_RECONNECT_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 16.0
# ~2s of 16 kHz mono PCM (4096 bytes/chunk ≈ 0.128s/chunk); bounds live latency during reconnects.
MAX_AUDIO_BUFFER_CHUNKS = 16

# Statuses that represent transient server/rate-limit conditions when closing a stream.
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# Handshake/rejection HTTP statuses that indicate permanent auth, quota, or config failures.
FATAL_HTTP_STATUS_CODES = frozenset({400, 401, 402, 403, 404, 422})
TRANSIENT_ERROR_CODES = frozenset(
    {
        "rate_limit_exceeded_error",
        "internal_server_error",
        "model_call_error",
        "gateway_timeout_error",
        "billing_service_unavailable_error",
        "service_unavailable",
        "rate_limited",
        "timeout",
    }
)
PERMANENT_ERROR_CODES = frozenset(
    {
        "invalid_request_error",
        "insufficient_quota_error",
        "invalid_api_key_error",
        "authentication_error",
        "unprocessable_entity_error",
        "not_found_error",
        "unauthorized",
        "forbidden",
        "invalid_api_key",
        "invalid_request",
        "invalid_config",
        "unsupported_language",
        "invalid_model",
        "quota_exceeded",
    }
)

# Official 23 BCP-47 language codes supported by Sarvam realtime STT (note: Odia is or-IN, not legacy od-IN).
SUPPORTED_SARVAM_LANGUAGE_CODES: tuple[str, ...] = (
    "en-IN",
    "hi-IN",
    "bn-IN",
    "kn-IN",
    "ml-IN",
    "mr-IN",
    "or-IN",
    "pa-IN",
    "ta-IN",
    "te-IN",
    "gu-IN",
    "as-IN",
    "ur-IN",
    "ne-IN",
    "kok-IN",
    "ks-IN",
    "sd-IN",
    "sa-IN",
    "sat-IN",
    "mni-IN",
    "brx-IN",
    "mai-IN",
    "doi-IN",
)

_SARVAM_BCP47_MAP: dict[str, str] = {code.lower(): code for code in SUPPORTED_SARVAM_LANGUAGE_CODES}
_SARVAM_ISO_MAP: dict[str, str] = {code.split("-", 1)[0].lower(): code for code in SUPPORTED_SARVAM_LANGUAGE_CODES}


class _SarvamFatalError(Exception):
    """Non-retryable authentication, quota, or configuration error reported by Sarvam."""


class _SarvamTransientError(Exception):
    """Retryable stream disconnection or transient server failure."""


class _StdoutReadError(Exception):
    """Local FFmpeg stdout read failure; must propagate to restart FFmpeg rather than retry WS."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def validate_sarvam_model(model_variant: str | None) -> str:
    """Validate and normalize the Sarvam realtime model identifier."""
    model = (model_variant or "").strip() or SARVAM_DEFAULT_MODEL
    if model not in SARVAM_ALLOWED_MODELS:
        raise ValueError(
            f"Unsupported Sarvam model '{model}'. Allowed models: {sorted(SARVAM_ALLOWED_MODELS)}"
        )
    return model


def map_sarvam_language_code(language_code: str | None) -> str:
    """Map a VoxBento language code to a Sarvam realtime BCP-47 code or 'auto'.

    Raises ``ValueError`` for explicit language codes not supported by Sarvam realtime STT.
    """
    if not language_code:
        return "auto"
    lower = language_code.strip().replace("_", "-").lower()
    if not lower or lower in ("floor", "auto"):
        return "auto"
    if lower in _SARVAM_BCP47_MAP:
        return _SARVAM_BCP47_MAP[lower]
    if lower in _SARVAM_ISO_MAP:
        return _SARVAM_ISO_MAP[lower]
    raise ValueError(
        f"Unsupported Sarvam language code '{language_code}'. "
        f"Supported codes: {', '.join(SUPPORTED_SARVAM_LANGUAGE_CODES)}"
    )


def is_supported_sarvam_language(language_code: str | None) -> bool:
    """Return True when ``language_code`` is supported by Sarvam realtime STT (or floor/auto)."""
    try:
        map_sarvam_language_code(language_code)
    except ValueError:
        return False
    return True


def build_sarvam_ws_url(language_code: str | None, model_variant: str | None) -> str:
    """Construct the Sarvam realtime WebSocket URL with verified query parameters."""
    model = validate_sarvam_model(model_variant)
    # TODO(#1164): Pass room/booth glossary terms via Sarvam's 'prompt' parameter once glossary integration is available.
    params = {
        "language_code": map_sarvam_language_code(language_code),
        "model": model,
        "encoding": "linear16",
        "sample_rate": str(SARVAM_SAMPLE_RATE),
        "stream_type": "balanced",
        "endpointing": "vad",
        "mode": "transcribe",
    }
    return f"{SARVAM_REALTIME_WS_URL}?{urllib.parse.urlencode(params)}"


def _http_status_from_exc(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _ws_close_code_from_exc(exc: BaseException) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _ws_close_reason_from_exc(exc: BaseException) -> str:
    rcvd = getattr(exc, "rcvd", None)
    reason = getattr(rcvd, "reason", None)
    if isinstance(reason, str):
        return reason
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) else ""


def _is_fatal_error_event(data: dict) -> bool:
    """Classify a server error event; any is_fatal=True error is fatal unless explicitly transient."""
    if not data.get("is_fatal"):
        return False
    code = str(data.get("code") or "").strip().lower()
    status_code = data.get("status_code")
    if code in PERMANENT_ERROR_CODES:
        return True
    if isinstance(status_code, int) and status_code in TRANSIENT_HTTP_STATUS_CODES:
        return False
    if code in TRANSIENT_ERROR_CODES:
        return False
    return True


def _is_fatal_ws_exception(exc: BaseException) -> bool:
    http_status = _http_status_from_exc(exc)
    if isinstance(http_status, int) and http_status in FATAL_HTTP_STATUS_CODES:
        return True

    close_code = _ws_close_code_from_exc(exc)
    reason = _ws_close_reason_from_exc(exc).lower()
    if close_code == 4000:
        return not any(tok in reason for tok in ("unavailable", "grpc", "backend", "timeout"))
    if close_code == 1003:
        return any(
            tok in reason
            for tok in ("invalid", "key", "auth", "unauthor", "forbid", "quota", "credit", "billing", "balance")
        )
    return False


def _is_ws_usable(ws: object, exc: BaseException | None) -> bool:
    if exc is not None:
        if isinstance(exc, (OSError, _SarvamFatalError, _SarvamTransientError)):
            return False
        if _ws_close_code_from_exc(exc) is not None or _http_status_from_exc(exc) is not None:
            return False
        if "ConnectionClosed" in type(exc).__name__:
            return False
    if getattr(ws, "closed", False):
        return False
    state_name = getattr(getattr(ws, "state", None), "name", None)
    return state_name not in ("CLOSING", "CLOSED")


async def _cancel_and_await(*tasks: asyncio.Task) -> None:
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class _BoundedAudioBuffer:
    """Bounded FIFO audio buffer coordinating persistent stdout draining and in-flight replay.

    Total unsent audio (one optional in-flight ``pending`` chunk plus ``queue``) is capped at
    ``max_chunks``. On overflow, the oldest audio is evicted first: ``pending`` if present
    (since it precedes every queued chunk), otherwise the oldest queued chunk.
    """

    def __init__(self, max_chunks: int) -> None:
        self._max_chunks = max(1, max_chunks)
        self._queue: deque[tuple[int, bytes]] = deque()
        self._pending: tuple[int, bytes] | None = None
        self._next_seq = 0
        self._eof = False
        self._error: BaseException | None = None
        self._cond = asyncio.Condition()
        self.stream_ended_event = asyncio.Event()
        self.dropped_chunks = 0

    @property
    def error(self) -> BaseException | None:
        return self._error

    def _update_stream_ended(self) -> None:
        if self._eof and self._pending is None and not self._queue:
            self.stream_ended_event.set()
        else:
            self.stream_ended_event.clear()

    def has_unsent(self) -> bool:
        return self._pending is not None or bool(self._queue)

    def is_at_clean_eof(self) -> bool:
        return self._eof and self._error is None and not self.has_unsent()

    async def push(self, chunk: bytes) -> None:
        async with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            while (len(self._queue) + (1 if self._pending is not None else 0)) >= self._max_chunks:
                self.dropped_chunks += 1
                if self._pending is not None:
                    self._pending = None
                elif self._queue:
                    self._queue.popleft()
            self._queue.append((seq, chunk))
            self._update_stream_ended()
            self._cond.notify_all()

    async def mark_eof(self) -> None:
        async with self._cond:
            self._eof = True
            self._update_stream_ended()
            self._cond.notify_all()

    async def mark_error(self, exc: BaseException) -> None:
        async with self._cond:
            self._error = exc
            self._eof = True
            self._update_stream_ended()
            self._cond.notify_all()

    async def next_chunk_or_eof(self) -> tuple[int, bytes] | None:
        async with self._cond:
            while self._pending is None and not self._queue and not self._eof:
                await self._cond.wait()
            if self._pending is not None:
                return self._pending
            if self._queue:
                self._pending = self._queue.popleft()
                return self._pending
            if self._error is not None:
                raise self._error
            return None

    def ack_sent(self, seq: int) -> None:
        if self._pending is not None and self._pending[0] == seq:
            self._pending = None
            self._update_stream_ended()


class SarvamProvider(TranscriptionProvider):
    """Streaming speech-to-text provider backed by Sarvam AI's realtime WebSocket API."""

    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        raise NotImplementedError("SarvamProvider is streaming-only; use run_stream().")

    async def run_stream(
        self,
        process: asyncio.subprocess.Process,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        broadcast_callback,
        booth_id: str,
        room_id: int | None = None,
    ) -> None:
        import websockets
        from websockets.exceptions import WebSocketException

        from portal.transcription.aggregator import CaptionAggregator

        api_key = (config.get_key() or "").strip() if config else ""
        if not api_key:
            logger.error(f"[{booth_id}] Sarvam API key missing")
            raise FatalTranscriptionError("Sarvam API key missing")

        try:
            url = build_sarvam_ws_url(language_code, model_variant)
        except ValueError as exc:
            raise FatalTranscriptionError(str(exc)) from exc

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        headers = {"Api-Subscription-Key": api_key}
        buffer = _BoundedAudioBuffer(MAX_AUDIO_BUFFER_CHUNKS)

        async def stdout_reader() -> None:
            pending_byte = b""
            try:
                while True:
                    chunk = await process.stdout.read(AUDIO_CHUNK_BYTES)
                    if not chunk:
                        # Discard an incomplete trailing byte if stdout closes mid-sample.
                        await buffer.mark_eof()
                        return
                    if pending_byte:
                        chunk = pending_byte + chunk
                        pending_byte = b""
                    if len(chunk) & 1:
                        pending_byte = chunk[-1:]
                        chunk = chunk[:-1]
                    if chunk:
                        await buffer.push(chunk)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                await buffer.mark_error(_StdoutReadError(exc))

        stdout_reader_task = asyncio.create_task(stdout_reader())
        consecutive_errors = 0
        outage_logged = False
        attempt = 0

        try:
            while not stdout_reader_task.done() or buffer.has_unsent():
                if attempt > 0:
                    if buffer.error is not None and not buffer.has_unsent():
                        raise buffer.error
                    if buffer.is_at_clean_eof():
                        return
                attempt += 1

                session_started = False
                session_ended_cleanly = False
                transcript_received = False
                connected_at = time.monotonic()

                try:
                    async with websockets.connect(url, additional_headers=headers) as ws:
                        send_lock = asyncio.Lock()
                        session_ready = asyncio.Event()
                        receiver_done = asyncio.Event()

                        async def send_json(payload: dict) -> None:
                            async with send_lock:
                                await ws.send(json.dumps(payload))

                        async def sender() -> str:
                            while True:
                                item = await buffer.next_chunk_or_eof()
                                if item is None:
                                    return "EOF"
                                seq, chunk = item
                                await send_json(
                                    {
                                        "event": "audio_input",
                                        "audio": base64.b64encode(chunk).decode("utf-8"),
                                    }
                                )
                                buffer.ack_sent(seq)

                        async def pinger() -> str:
                            while True:
                                await asyncio.sleep(PING_INTERVAL_SECONDS)
                                await send_json({"event": "ping"})

                        async def receiver() -> str:
                            nonlocal consecutive_errors, outage_logged, session_started, session_ended_cleanly, transcript_received
                            try:
                                async for raw_msg in ws:
                                    try:
                                        data = json.loads(raw_msg)
                                    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                                        logger.warning(f"[{booth_id}] Ignoring malformed JSON frame from Sarvam: {exc}")
                                        continue

                                    if not isinstance(data, dict):
                                        logger.warning(
                                            f"[{booth_id}] Ignoring non-object Sarvam WebSocket frame of type "
                                            f"{type(data).__name__}"
                                        )
                                        continue

                                    event = data.get("event")
                                    if not isinstance(event, str):
                                        logger.warning(
                                            f"[{booth_id}] Ignoring Sarvam frame with invalid event type: {event!r}"
                                        )
                                        continue

                                    if event == "session.begin":
                                        session_started = True
                                        outage_logged = False
                                        session_ready.set()
                                        logger.info(
                                            f"[{booth_id}] Sarvam session started (request_id={data.get('request_id')})"
                                        )
                                    elif event == "transcript.partial":
                                        raw_text = data.get("text")
                                        if raw_text is not None and not isinstance(raw_text, str):
                                            logger.warning(
                                                f"[{booth_id}] Ignoring Sarvam transcript.partial with non-string text: "
                                                f"{type(raw_text).__name__}"
                                            )
                                            continue
                                        if session_started:
                                            transcript_received = True
                                            consecutive_errors = 0
                                        text = raw_text.strip() if isinstance(raw_text, str) else ""
                                        if text:
                                            await aggregator.handle_partial(booth_id, text)
                                    elif event == "transcript.final":
                                        raw_text = data.get("text")
                                        if raw_text is not None and not isinstance(raw_text, str):
                                            logger.warning(
                                                f"[{booth_id}] Ignoring Sarvam transcript.final with non-string text: "
                                                f"{type(raw_text).__name__}"
                                            )
                                            continue
                                        if session_started:
                                            transcript_received = True
                                            consecutive_errors = 0
                                        text = raw_text.strip() if isinstance(raw_text, str) else ""
                                        if text:
                                            await aggregator.handle_final(booth_id, text)
                                        else:
                                            await aggregator.handle_clear(booth_id)
                                    elif event == "session.end":
                                        session_ended_cleanly = True
                                        logger.info(
                                            f"[{booth_id}] Sarvam session ended "
                                            f"(request_id={data.get('request_id')}, "
                                            f"audio_duration_s={data.get('audio_duration_s')})"
                                        )
                                        return "SESSION_END"
                                    elif event == "error":
                                        code = data.get("code")
                                        status_code = data.get("status_code")
                                        is_fatal = bool(data.get("is_fatal", False))
                                        message = data.get("message")
                                        if message is None:
                                            message = data.get("error", "")
                                        if is_fatal:
                                            if _is_fatal_error_event(data):
                                                logger.error(
                                                    f"[{booth_id}] Sarvam WS error "
                                                    f"(code={code}, status_code={status_code}, is_fatal={is_fatal}): "
                                                    f"{message}"
                                                )
                                                raise _SarvamFatalError(
                                                    f"Sarvam fatal error (code={code}, status={status_code}): {message}"
                                                )
                                            logger.debug(
                                                f"[{booth_id}] Sarvam WS transient error event "
                                                f"(code={code}, status_code={status_code}, is_fatal={is_fatal}): "
                                                f"{message}"
                                            )
                                            raise _SarvamTransientError(
                                                f"Sarvam stream error (code={code}, status={status_code}): {message}"
                                            )
                                        logger.warning(
                                            f"[{booth_id}] Sarvam WS non-fatal error "
                                            f"(code={code}, status_code={status_code}): {message}"
                                        )
                                    elif event in ("pong", "vad.speech_start", "vad.speech_end", "config.updated"):
                                        logger.debug(f"[{booth_id}] Sarvam event {event}: {data}")
                                    else:
                                        logger.debug(f"[{booth_id}] Ignoring unknown Sarvam event '{event}': {data}")
                                return "CLOSED"
                            finally:
                                receiver_done.set()

                        async def handle_eof(
                            recv_task: asyncio.Task, ping_t: asyncio.Task | None
                        ) -> None:
                            if ping_t is not None:
                                await _cancel_and_await(ping_t)

                            if recv_task.done():
                                recv_exc = None if recv_task.cancelled() else recv_task.exception()
                                if isinstance(recv_exc, _SarvamFatalError):
                                    raise recv_exc
                                if recv_exc is not None and _is_ws_usable(ws, recv_exc):
                                    logger.warning(
                                        f"[{booth_id}] Receiver failed at EOF while WS usable; sending end: {recv_exc}"
                                    )
                                    try:
                                        await send_json({"event": "end"})
                                    except (WebSocketException, OSError, TimeoutError, RuntimeError) as exc:
                                        logger.debug(f"[{booth_id}] Sarvam end signal failed during EOF: {exc}")
                                else:
                                    logger.debug(
                                        f"[{booth_id}] Stream ended at EOF with receiver already finished: "
                                        f"{recv_exc or recv_task.result()}"
                                    )
                                return

                            if _is_ws_usable(ws, None):
                                try:
                                    await send_json({"event": "end"})
                                except (WebSocketException, OSError, TimeoutError, RuntimeError) as exc:
                                    logger.debug(f"[{booth_id}] Sarvam end signal failed during EOF: {exc}")

                            if not recv_task.done():
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(recv_task),
                                        timeout=EOF_DRAIN_TIMEOUT_SECONDS,
                                    )
                                except TimeoutError:
                                    logger.warning(f"[{booth_id}] Timed out waiting for Sarvam session.end after EOF")
                                except _SarvamFatalError:
                                    raise
                                except (WebSocketException, _SarvamTransientError, OSError, RuntimeError) as exc:
                                    logger.debug(f"[{booth_id}] Receiver closed during EOF drain: {exc}")

                            if recv_task.done() and not recv_task.cancelled():
                                recv_exc = recv_task.exception()
                                if isinstance(recv_exc, _SarvamFatalError):
                                    raise recv_exc

                        receiver_task = asyncio.create_task(receiver())
                        sender_task: asyncio.Task | None = None
                        ping_task: asyncio.Task | None = None

                        try:
                            ready_wait = asyncio.create_task(session_ready.wait())
                            done_wait = asyncio.create_task(receiver_done.wait())
                            try:
                                await asyncio.wait(
                                    (ready_wait, done_wait),
                                    timeout=SESSION_BEGIN_TIMEOUT_SECONDS,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                            finally:
                                await _cancel_and_await(ready_wait, done_wait)

                            if not session_ready.is_set():
                                if receiver_done.is_set():
                                    recv_exc = None if receiver_task.cancelled() else receiver_task.exception()
                                    if isinstance(recv_exc, _SarvamFatalError):
                                        raise recv_exc
                                    if not stdout_reader_task.done():
                                        await asyncio.sleep(0)
                                    if buffer.error is not None and not buffer.has_unsent():
                                        raise buffer.error
                                    if buffer.is_at_clean_eof():
                                        await handle_eof(receiver_task, None)
                                        return
                                    if recv_exc is not None:
                                        raise recv_exc
                                    raise _SarvamTransientError("Sarvam WebSocket closed before session.begin")
                                raise _SarvamTransientError("Timed out waiting for Sarvam session.begin")

                            sender_task = asyncio.create_task(sender())
                            ping_task = asyncio.create_task(pinger())
                            tasks = (sender_task, receiver_task, ping_task)

                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                            sender_eof = (
                                sender_task in done
                                and not sender_task.cancelled()
                                and sender_task.exception() is None
                                and sender_task.result() == "EOF"
                            )
                            if sender_eof:
                                await handle_eof(receiver_task, ping_task)
                                return

                            for task in done:
                                if not task.cancelled():
                                    exc = task.exception()
                                    if exc is not None:
                                        raise exc

                            if buffer.is_at_clean_eof():
                                return

                            raise _SarvamTransientError("Sarvam WebSocket closed unexpectedly before stream EOF")
                        finally:
                            cleanup_tasks = [t for t in (sender_task, receiver_task, ping_task) if t is not None]
                            await _cancel_and_await(*cleanup_tasks)

                except asyncio.CancelledError:
                    raise
                except _StdoutReadError:
                    raise
                except _SarvamFatalError as exc:
                    logger.error(f"[{booth_id}] Sarvam fatal configuration/authentication error; not reconnecting: {exc}")
                    raise FatalTranscriptionError(str(exc)) from exc
                except (_SarvamTransientError, WebSocketException, OSError, TimeoutError, EOFError) as exc:
                    if buffer.error is not None and not buffer.has_unsent():
                        raise buffer.error

                    if _is_fatal_ws_exception(exc):
                        logger.error(
                            f"[{booth_id}] Sarvam fatal rejection "
                            f"(http_status={_http_status_from_exc(exc)}, "
                            f"close_code={_ws_close_code_from_exc(exc)}); not reconnecting: {exc}"
                        )
                        raise FatalTranscriptionError(str(exc)) from exc

                    if session_started and (
                        transcript_received or (time.monotonic() - connected_at) >= STABLE_SESSION_SECONDS
                    ):
                        consecutive_errors = 0

                    consecutive_errors += 1
                    if not outage_logged:
                        logger.error(
                            f"[{booth_id}] Sarvam WS connection error ({consecutive_errors}/{MAX_RECONNECT_ATTEMPTS}): {exc}"
                        )
                        outage_logged = True
                    else:
                        logger.warning(
                            f"[{booth_id}] Sarvam WS reconnect attempt failed "
                            f"({consecutive_errors}/{MAX_RECONNECT_ATTEMPTS}): {exc}"
                        )

                    if consecutive_errors >= MAX_RECONNECT_ATTEMPTS:
                        logger.error(f"[{booth_id}] Sarvam Realtime connection repeatedly failed. Giving up.")
                        raise FatalTranscriptionError(
                            f"Sarvam Realtime connection failed after {consecutive_errors} attempts: {exc}",
                            permanent=False,
                        ) from exc

                    if not session_ended_cleanly and not buffer.is_at_clean_eof():
                        await aggregator.clear_partial(booth_id)

                    delay = min(BACKOFF_BASE_SECONDS * (2 ** (consecutive_errors - 1)), BACKOFF_MAX_SECONDS)
                    try:
                        await asyncio.wait_for(buffer.stream_ended_event.wait(), timeout=delay)
                        if buffer.error is not None:
                            raise buffer.error
                        return
                    except TimeoutError:
                        pass
        except _StdoutReadError as exc:
            logger.error(f"[{booth_id}] FFmpeg stdout read failed; stopping Sarvam stream: {exc.original}")
            raise exc.original from exc
        finally:
            await _cancel_and_await(stdout_reader_task)
