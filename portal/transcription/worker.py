import asyncio
import logging
import uuid
from enum import Enum
from typing import Dict

import portal.webhooks.worker as _wh_worker
from portal.config import settings
from portal.transcription.process import FfmpegProcess
from portal.transcription.providers.base import ProviderConfig
from portal.transcription.providers.deepgram import DeepgramProvider
from portal.transcription.providers.elevenlabs import ElevenLabsProvider
from portal.transcription.providers.local import LocalProvider
from portal.transcription.providers.nvidia import NVIDIAProvider
from portal.transcription.providers.openai import OpenAIProvider

logger = logging.getLogger(__name__)

PROVIDERS = {
    "local": LocalProvider(),
    "openai": OpenAIProvider(),
    "deepgram": DeepgramProvider(),
    "nvidia": NVIDIAProvider(),
    "elevenlabs": ElevenLabsProvider(),
}

active_workers_lock = asyncio.Lock()
MAX_TOTAL_WORKERS = 10

class State(Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"

class TranscriptionWorkerSession:
    def __init__(
        self,
        event_slug: str,
        language_code: str,
        booth_id: str,
        broadcast_callback,
        provider_name: str,
        model_size: str,
        config: ProviderConfig,
        transcription_language: str | None = None,
        room_id: int | None = None,
    ):
        self.session_id = str(uuid.uuid4())
        self.event_slug = event_slug
        self.language_code = language_code
        self.booth_id = booth_id
        self.broadcast_callback = broadcast_callback
        self.provider_name = provider_name
        self.model_size = model_size
        self.config = config
        self.transcription_language = transcription_language
        self.room_id = room_id

        self.state = State.STARTING
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

        from portal.booth_identity import make_mediamtx_path
        channel_path = make_mediamtx_path(self.event_slug, self.room_id, self.language_code)
        self.rtsp_url = f"{settings.mediamtx_rtsp_base}/{channel_path}"
        self.provider = PROVIDERS.get(self.provider_name, PROVIDERS["local"])
        self.sample_rate = "24000" if self.provider_name == "openai" else "16000"

    def start(self):
        if self.task:
            return
        logger.info(f"[{self.booth_id}][{self.session_id}] Session started in state STARTING")
        self.task = asyncio.create_task(self._run_loop())

    def stop(self):
        if self.state in (State.STOPPING, State.STOPPED):
            return
        logger.info(f"[{self.booth_id}][{self.session_id}] Session stopping")
        self.state = State.STOPPING
        self.stop_event.set()
        if self.task:
            self.task.cancel()

    async def wait_until_stopped(self):
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[{self.booth_id}][{self.session_id}] Task ended with exception: {e}")

    async def _run_loop(self):
        retry_count = 0
        try:
            while self.state != State.STOPPING:
                # Narrow the race window: check STOPPING immediately before spawning ffmpeg
                if self.state == State.STOPPING:
                    break

                self.state = State.RUNNING
                await _wh_worker.enqueue_webhook("booth.transcription.started", {"booth_id": self.booth_id, "session_id": self.session_id})

                # The context manager entirely encapsulates ffmpeg process lifecycle and cleanup.
                async with FfmpegProcess(self.rtsp_url, self.sample_rate, self.booth_id) as process:
                    try:
                        actual_language = self.transcription_language or self.language_code
                        from portal.transcription.providers.base import AudioIngester, StreamingProvider
                        if isinstance(self.provider, StreamingProvider):
                            ingester = AudioIngester(process, sample_rate=self.sample_rate)
                            from portal.transcription.aggregator import CaptionAggregator
                            aggregator = CaptionAggregator(self.broadcast_callback, room_id=self.room_id)
                            async def notify_gap(start: float, end: float):
                                logger.warning(f"[{self.booth_id}] Audio gap: {start:.1f}s - {end:.1f}s")
                                await self.broadcast_callback(self.booth_id, f"[Audio gap: {end-start:.1f}s skipped to catch up]")
                            await self.provider.process_stream(
                                ingester.stream(),
                                aggregator,
                                notify_gap,
                                language_code=actual_language,
                                model_variant=self.model_size,
                                config=self.config,
                                booth_id=self.booth_id
                            )
                        else:
                            await self.provider.run_stream(
                                process, actual_language, self.model_size, self.config, self.broadcast_callback, self.booth_id, self.room_id
                            )
                    except asyncio.IncompleteReadError:
                        logger.error(f"[{self.booth_id}][{self.session_id}] ffmpeg stream ended abruptly. Retrying...")
                    except asyncio.CancelledError:
                        logger.info(f"[{self.booth_id}][{self.session_id}] Transcription worker cancelled.")
                        raise
                    except Exception as e:
                        logger.error(f"[{self.booth_id}][{self.session_id}] Transcription error: {e}. Retrying...")

                if self.state == State.STOPPING:
                    break

                retry_count += 1
                logger.info(f"[{self.booth_id}][{self.session_id}] Retry scheduled (count={retry_count})")

                # Interruptible retry wait
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        finally:
            self.state = State.STOPPED
            await _wh_worker.enqueue_webhook("booth.transcription.stopped", {"booth_id": self.booth_id, "session_id": self.session_id})
            if self.provider_name == "local":
                from portal.transcription.providers.local import decrement_model_ref
                decrement_model_ref(self.model_size)
            logger.info(f"[{self.booth_id}][{self.session_id}] Transcription worker exited and cleaned up cleanly.")

active_workers: Dict[str, TranscriptionWorkerSession] = {}


async def start_transcription_worker(
    event_slug: str,
    language_code: str,
    booth_id: str,
    broadcast_callback,
    provider: str,
    model_size: str,
    config: ProviderConfig,
    transcription_language: str | None = None,
    room_id: int | None = None,
):
    while True:
        async with active_workers_lock:
            existing_session = active_workers.get(booth_id)
            if not existing_session:
                if len(active_workers) >= MAX_TOTAL_WORKERS:
                    raise ValueError(f"System at maximum capacity ({MAX_TOTAL_WORKERS} concurrent transcription booths).")

                if provider == "local":
                    from portal.transcription.providers.local import increment_model_ref, start_eviction_loop
                    increment_model_ref(model_size)
                    start_eviction_loop()

                new_session = TranscriptionWorkerSession(
                    event_slug, language_code, booth_id, broadcast_callback, provider, model_size, config, transcription_language, room_id
                )
                active_workers[booth_id] = new_session
                new_session.start()
                return

            if existing_session.state in (State.STOPPING, State.STOPPED):
                if existing_session.state == State.STOPPED:
                    # It's fully dead. Pop it and let the loop recreate it immediately.
                    active_workers.pop(booth_id)
                    continue
                # Session is tearing down in the background. We must strictly serialize.
                # Drop lock, actively await the old session's death, then loop around and re-check registry.
                pass
            else:
                logger.info(f"Transcription worker for {booth_id} is already running.")
                return

        # We dropped the lock and the session is STOPPING. Wait for it to die completely.
        logger.info(f"[{booth_id}] start_transcription_worker actively waiting for old session [{existing_session.session_id}] to STOP.")
        await existing_session.wait_until_stopped()


async def stop_transcription_worker(booth_id: str):
    session = None
    async with active_workers_lock:
        session = active_workers.get(booth_id)
        if session:
            session.stop()

    # We dropped the lock so we don't hold it across an await.
    if session:
        await session.wait_until_stopped()
        # Clean up only if it hasn't been replaced
        async with active_workers_lock:
            if active_workers.get(booth_id) is session:
                active_workers.pop(booth_id)
