"""Sarvam AI realtime speech-to-text for Indian languages (saaras:v3-realtime, beta)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import urlencode

import websockets

from portal.outage_log import OutageLog
from portal.transcription.constants import sarvam_language_code
from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)

SARVAM_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
FRAME_BYTES = 3200  # 100 ms of 16 kHz mono 16-bit PCM, the framing Sarvam's guide uses.
PING_AFTER_SECS = 5.0  # Audio stalled this long: ping, or Sarvam closes the idle session (1008).
FINAL_WAIT_SECS = 5.0  # After "end", how long to wait for the last transcript.
STABLE_AFTER_SECS = 10.0  # A session this old was not rejected, so the outage is over.
BACKOFF_BASE_SECS = 1.0
BACKOFF_MAX_SECS = 30.0


class _SarvamRejected(Exception):
    """Sarvam reported a fatal error. Holds only its code and status, never its message."""


def _failure_reason(exc: Exception) -> str:
    """Name a failure without echoing third-party text, so a secret can't reach the logs."""
    if isinstance(exc, _SarvamRejected):
        return str(exc)
    if isinstance(exc, websockets.ConnectionClosed):
        return f"connection closed code={exc.rcvd.code if exc.rcvd else None}"
    if isinstance(exc, websockets.InvalidStatus):
        return f"handshake rejected status={exc.response.status_code}"
    return type(exc).__name__


class _LinkHealth:
    """Backoff and once-per-outage logging across one booth's reconnects."""

    def __init__(self, booth_id: str) -> None:
        self.booth_id = booth_id
        self.attempts = 0
        self.outage = OutageLog(logger, f"sarvam_stream_failed booth_id={booth_id}")

    def stable(self) -> None:
        self.attempts = 0
        self.outage.recovered()

    def failed(self, reason: str) -> float:
        """Record a lost session and return how long to wait before reconnecting."""
        if not self.outage.failures:
            logger.warning("[%s] Sarvam session lost: %s", self.booth_id, reason)
        self.outage.failed()
        delay = min(BACKOFF_BASE_SECS * 2**self.attempts, BACKOFF_MAX_SECS)
        if delay < BACKOFF_MAX_SECS:
            self.attempts += 1
        return delay


class _CaptionFeed:
    """Feeds one WebSocket session's transcripts to the aggregator.

    Partials repeat the utterance so far, while the aggregator force-finalizes a long one
    (15 s or 50 words). Remembering what it already published keeps the rest of that
    utterance from being captioned, translated and spoken twice.
    """

    def __init__(self, aggregator, booth_id: str) -> None:
        self.aggregator = aggregator
        self.booth_id = booth_id
        self.committed = ""

    def _unpublished(self, text: str) -> str:
        """Drop the part of this utterance that forced finalization already published."""
        if self.committed and text.startswith(self.committed):
            return text[len(self.committed) :].strip()
        return text

    async def _partial(self, text: str) -> None:
        """Show an interim caption and note what the aggregator force-finalized."""
        unpublished = self._unpublished(text)
        if not unpublished:
            return
        await self.aggregator.handle_partial(self.booth_id, unpublished)
        if not self.aggregator.states[self.booth_id].current_utterance:
            self.committed = text

    async def _final(self, text: str) -> None:
        """Publish the end of an utterance and start tracking the next one."""
        unpublished = self._unpublished(text)
        self.committed = ""
        if unpublished:
            await self.aggregator.handle_final(self.booth_id, unpublished)
        else:
            await self.aggregator.handle_clear(self.booth_id)

    async def handle(self, message: dict) -> None:
        """Publish a transcript or act on a Sarvam error; every other event is ignored."""
        event = message.get("event")
        text = message.get("text")
        if event == "transcript.partial" and isinstance(text, str):
            await self._partial(text.strip())
        elif event == "transcript.final" and isinstance(text, str):
            await self._final(text.strip())
        elif event == "error":
            detail = f"error code={message.get('code')} status={message.get('status_code')}"
            if message.get("is_fatal"):
                raise _SarvamRejected(detail)
            logger.warning("[%s] Sarvam %s", self.booth_id, detail)


async def _send_audio(ws, process: asyncio.subprocess.Process) -> None:
    """Stream ffmpeg's PCM until it ends, then ask Sarvam to finish the pending utterance."""

    async def send_frame(frame: bytes) -> None:
        await ws.send(json.dumps({"event": "audio_input", "audio": base64.b64encode(frame).decode("ascii")}))

    while True:
        try:
            frame = await asyncio.wait_for(process.stdout.readexactly(FRAME_BYTES), timeout=PING_AFTER_SECS)
        except TimeoutError:
            await ws.send(json.dumps({"event": "ping"}))
            continue
        except asyncio.IncompleteReadError as eof:
            if eof.partial:
                await send_frame(eof.partial)
            await ws.send(json.dumps({"event": "end"}))
            return
        await send_frame(frame)


async def _receive(ws, feed: _CaptionFeed) -> None:
    """Hand each Sarvam message to the feed, skipping any that isn't a JSON object."""
    async for raw in ws:
        try:
            message = json.loads(raw)
        except ValueError:
            message = None
        if isinstance(message, dict):
            await feed.handle(message)
        else:
            logger.warning("[%s] Ignored a malformed Sarvam message", feed.booth_id)


async def _discard_audio(process: asyncio.subprocess.Process, delay: float) -> bool:
    """Wait out a backoff, dropping audio that would only be stale by the time we reconnect.

    Draining also stops ffmpeg blocking on a full pipe. Returns False if the audio ended.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while (remaining := deadline - loop.time()) > 0:
        try:
            if not await asyncio.wait_for(process.stdout.read(65536), timeout=remaining):
                return False
        except TimeoutError:
            break
    return True


class SarvamProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        return ""  # Streaming only: Sarvam's REST limit (60 requests/minute) can't carry live captions.

    @staticmethod
    async def _run_session(url, headers, process, feed: _CaptionFeed, link: _LinkHealth) -> bool:
        """Run one WebSocket session. True means the audio ended; False or an exception, reconnect."""
        async with websockets.connect(url, additional_headers=headers) as ws:
            sender = asyncio.create_task(_send_audio(ws, process))
            receiver = asyncio.create_task(_receive(ws, feed))
            tasks = {sender, receiver}
            try:
                done, _ = await asyncio.wait(tasks, timeout=STABLE_AFTER_SECS, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    link.stable()
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    sender.result()
                    done, _ = await asyncio.wait({receiver}, timeout=FINAL_WAIT_SECS)
                    if receiver in done:
                        receiver.result()
                    return True
                receiver.result()
                return False
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

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
        """Stream ffmpeg's audio to Sarvam until it ends, reconnecting with backoff when the link drops."""
        from portal.transcription.aggregator import CaptionAggregator

        api_key = config.get_key()
        if not api_key:
            logger.error("[%s] Sarvam API key missing", booth_id)
            return
        sarvam_language = sarvam_language_code(language_code)
        if sarvam_language is None:
            logger.error("[%s] Sarvam does not support language %s", booth_id, language_code)
            return

        # TODO(#1164): send the event glossary's source terms as the `prompt` query parameter.
        query = urlencode(
            {"model": model_variant, "language_code": sarvam_language, "encoding": "linear16", "sample_rate": 16000}
        )
        url = f"{SARVAM_WS_URL}?{query}"
        headers = {"API-Subscription-Key": api_key}

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        link = _LinkHealth(booth_id)
        while process.returncode is None:
            ended = False
            reason = "server ended the session"
            try:
                ended = await self._run_session(url, headers, process, _CaptionFeed(aggregator, booth_id), link)
            except asyncio.CancelledError:
                # The worker is stopping: keep the words already spoken in the transcript.
                await aggregator.handle_clear(booth_id)
                raise
            except Exception as exc:
                reason = _failure_reason(exc)
            # Publish a caption the lost session left unfinished; the next one starts a new utterance.
            await aggregator.handle_clear(booth_id)
            if ended or not await _discard_audio(process, link.failed(reason)):
                return
