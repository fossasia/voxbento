from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)

SARVAM_REALTIME_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
SARVAM_DEFAULT_MODEL = "saaras:v3-realtime"
SARVAM_ALLOWED_MODELS = frozenset({SARVAM_DEFAULT_MODEL})
SARVAM_SAMPLE_RATE = 16000
AUDIO_CHUNK_BYTES = 4096
PING_INTERVAL_SECONDS = 15.0
STABLE_SESSION_SECONDS = 10.0
EOF_DRAIN_TIMEOUT_SECONDS = 5.0
MAX_RECONNECT_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 16.0

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


def validate_sarvam_model(model_variant: str | None) -> str:
    """Validate and normalize the Sarvam realtime model identifier."""
    model = (model_variant or "").strip() or SARVAM_DEFAULT_MODEL
    if model not in SARVAM_ALLOWED_MODELS:
        raise ValueError(
            f"Unsupported Sarvam model '{model}'. Allowed models: {sorted(SARVAM_ALLOWED_MODELS)}"
        )
    return model


def map_sarvam_language_code(language_code: str | None) -> str:
    """Map a VoxBento language code to a Sarvam realtime BCP-47 code or 'auto'."""
    if not language_code:
        return "auto"
    lower = language_code.strip().replace("_", "-").lower()
    if not lower or lower in ("floor", "auto"):
        return "auto"
    if lower in _SARVAM_BCP47_MAP:
        return _SARVAM_BCP47_MAP[lower]
    if lower in _SARVAM_ISO_MAP:
        return _SARVAM_ISO_MAP[lower]
    return "auto"


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
        return ""

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

        from portal.transcription.aggregator import CaptionAggregator

        api_key = (config.get_key() or "").strip() if config else ""
        if not api_key:
            logger.error(f"[{booth_id}] Sarvam API key missing")
            return

        url = build_sarvam_ws_url(language_code, model_variant)
        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        headers = {"Api-Subscription-Key": api_key}

        consecutive_errors = 0
        while process.returncode is None:
            session_started = False
            transcript_received = False
            connected_at = time.monotonic()

            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    send_lock = asyncio.Lock()

                    async def send_json(payload: dict) -> None:
                        async with send_lock:
                            await ws.send(json.dumps(payload))

                    async def sender() -> str:
                        while True:
                            chunk = await process.stdout.read(AUDIO_CHUNK_BYTES)
                            if not chunk:
                                return "EOF"
                            await send_json(
                                {
                                    "event": "audio_input",
                                    "audio": base64.b64encode(chunk).decode("utf-8"),
                                }
                            )

                    async def pinger() -> str:
                        while True:
                            await asyncio.sleep(PING_INTERVAL_SECONDS)
                            await send_json({"event": "ping"})

                    async def receiver() -> str:
                        nonlocal consecutive_errors, session_started, transcript_received
                        async for raw_msg in ws:
                            data = json.loads(raw_msg)
                            event = data.get("event")

                            if event == "session.begin":
                                session_started = True
                                logger.info(
                                    f"[{booth_id}] Sarvam session started (request_id={data.get('request_id')})"
                                )
                            elif event == "transcript.partial":
                                if session_started:
                                    transcript_received = True
                                    consecutive_errors = 0
                                text = (data.get("text") or "").strip()
                                if text:
                                    await aggregator.handle_partial(booth_id, text)
                            elif event == "transcript.final":
                                if session_started:
                                    transcript_received = True
                                    consecutive_errors = 0
                                text = (data.get("text") or "").strip()
                                if text:
                                    await aggregator.handle_final(booth_id, text)
                                else:
                                    await aggregator.handle_clear(booth_id)
                            elif event == "session.end":
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
                                message = data.get("message", "")
                                logger.error(
                                    f"[{booth_id}] Sarvam WS error "
                                    f"(code={code}, status_code={status_code}, is_fatal={is_fatal}): {message}"
                                )
                                if is_fatal:
                                    if _is_fatal_error_event(data):
                                        raise _SarvamFatalError(
                                            f"Sarvam fatal error (code={code}, status={status_code}): {message}"
                                        )
                                    raise _SarvamTransientError(
                                        f"Sarvam stream error (code={code}, status={status_code}): {message}"
                                    )
                            elif event in ("pong", "vad.speech_start", "vad.speech_end", "config.updated"):
                                logger.debug(f"[{booth_id}] Sarvam event {event}: {data}")
                        return "CLOSED"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())
                    ping_task = asyncio.create_task(pinger())
                    tasks = (sender_task, receiver_task, ping_task)

                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                        sender_eof = (
                            sender_task in done
                            and not sender_task.cancelled()
                            and sender_task.exception() is None
                            and sender_task.result() == "EOF"
                        )
                        if sender_eof:
                            await _cancel_and_await(ping_task)

                            if receiver_task.done():
                                recv_exc = None if receiver_task.cancelled() else receiver_task.exception()
                                if isinstance(recv_exc, _SarvamFatalError):
                                    raise recv_exc
                                if recv_exc is not None and _is_ws_usable(ws, recv_exc):
                                    logger.warning(
                                        f"[{booth_id}] Receiver failed at EOF while WS usable; sending end: {recv_exc}"
                                    )
                                    try:
                                        await send_json({"event": "end"})
                                    except Exception as exc:
                                        logger.debug(f"[{booth_id}] Sarvam end signal failed during EOF: {exc}")
                                else:
                                    logger.debug(
                                        f"[{booth_id}] Stream ended at EOF with receiver already finished: "
                                        f"{recv_exc or receiver_task.result()}"
                                    )
                                return

                            if _is_ws_usable(ws, None):
                                try:
                                    await send_json({"event": "end"})
                                except Exception as exc:
                                    logger.debug(f"[{booth_id}] Sarvam end signal failed during EOF: {exc}")

                            if not receiver_task.done():
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(receiver_task),
                                        timeout=EOF_DRAIN_TIMEOUT_SECONDS,
                                    )
                                except TimeoutError:
                                    logger.warning(f"[{booth_id}] Timed out waiting for Sarvam session.end after EOF")
                                except _SarvamFatalError:
                                    raise
                                except Exception as exc:
                                    logger.debug(f"[{booth_id}] Receiver closed during EOF drain: {exc}")

                            if receiver_task.done() and not receiver_task.cancelled():
                                recv_exc = receiver_task.exception()
                                if isinstance(recv_exc, _SarvamFatalError):
                                    raise recv_exc
                            return

                        for task in done:
                            if not task.cancelled():
                                exc = task.exception()
                                if exc is not None:
                                    raise exc

                        if process.returncode is not None:
                            return

                        raise _SarvamTransientError("Sarvam WebSocket closed unexpectedly before stream EOF")
                    finally:
                        await _cancel_and_await(*tasks)

            except asyncio.CancelledError:
                raise
            except _SarvamFatalError as exc:
                logger.error(f"[{booth_id}] Sarvam fatal configuration/authentication error; not reconnecting: {exc}")
                return
            except Exception as exc:
                if _is_fatal_ws_exception(exc):
                    logger.error(
                        f"[{booth_id}] Sarvam fatal rejection "
                        f"(http_status={_http_status_from_exc(exc)}, "
                        f"close_code={_ws_close_code_from_exc(exc)}); not reconnecting: {exc}"
                    )
                    return

                if session_started and (
                    transcript_received or (time.monotonic() - connected_at) >= STABLE_SESSION_SECONDS
                ):
                    consecutive_errors = 0

                consecutive_errors += 1
                logger.error(
                    f"[{booth_id}] Sarvam WS connection error ({consecutive_errors}/{MAX_RECONNECT_ATTEMPTS}): {exc}"
                )

                if consecutive_errors >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(f"[{booth_id}] Sarvam Realtime connection repeatedly failed. Giving up.")
                    break

                delay = min(BACKOFF_BASE_SECONDS * (2 ** (consecutive_errors - 1)), BACKOFF_MAX_SECONDS)
                await asyncio.sleep(delay)
