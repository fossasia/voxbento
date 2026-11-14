import asyncio
import hmac
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


class TranscriptionStartResult:
    def __init__(
        self,
        status: str,
        provider: str,
        model: str,
        error: str | None = None,
    ):
        self.status = status  # "started", "already_running", "failed"
        self.provider = provider
        self.model = model
        self.error = error

    def __bool__(self) -> bool:
        return self.status in ("started", "already_running")

    def __repr__(self) -> str:
        return f"<TranscriptionStartResult status={self.status!r} provider={self.provider!r} model={self.model!r} error={self.error!r}>"


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
        self.ready_event = asyncio.Event()
        self.start_error: str | None = None
        self.started_webhook_emitted: bool = False
        self.task: asyncio.Task | None = None
        self.loop_entered = False

        from portal.booth_identity import make_mediamtx_path

        channel_path = make_mediamtx_path(self.event_slug, self.room_id, self.language_code)
        self.rtsp_url = f"{settings.mediamtx_rtsp_base}/{channel_path}"
        self.provider = PROVIDERS.get(self.provider_name) or PROVIDERS.get("local")
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
        if self.task and self.loop_entered:
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
        self.loop_entered = True
        retry_count = 0
        try:
            while self.state != State.STOPPING:
                # Narrow the race window: check STOPPING immediately before spawning ffmpeg
                if self.state == State.STOPPING:
                    break

                try:
                    # The context manager entirely encapsulates ffmpeg process lifecycle and cleanup.
                    async with FfmpegProcess(self.rtsp_url, self.sample_rate, self.booth_id) as process:
                        self.state = State.RUNNING
                        if not self.started_webhook_emitted:
                            await _wh_worker.enqueue_webhook(
                                "booth.transcription.started",
                                {
                                    "booth_id": self.booth_id,
                                    "session_id": self.session_id,
                                    "event_slug": self.event_slug,
                                },
                            )
                            self.started_webhook_emitted = True
                        if not self.ready_event.is_set():
                            self.ready_event.set()

                        actual_language = self.transcription_language or self.language_code
                        if actual_language == "floor":
                            actual_language = ""

                        from portal.transcription.providers.base import AudioIngester, StreamingProvider

                        if isinstance(self.provider, StreamingProvider):
                            ingester = AudioIngester(process, sample_rate=self.sample_rate)
                            from portal.transcription.aggregator import CaptionAggregator

                            aggregator = CaptionAggregator(self.broadcast_callback, room_id=self.room_id)

                            async def notify_gap(start: float, end: float):
                                logger.warning(f"[{self.booth_id}] Audio gap: {start:.1f}s - {end:.1f}s")
                                await self.broadcast_callback(
                                    self.booth_id, f"[Audio gap: {end - start:.1f}s skipped to catch up]"
                                )

                            await self.provider.process_stream(
                                ingester.stream(),
                                aggregator,
                                notify_gap,
                                language_code=actual_language,
                                model_variant=self.model_size,
                                config=self.config,
                                booth_id=self.booth_id,
                            )
                        else:
                            await self.provider.run_stream(
                                process,
                                actual_language,
                                self.model_size,
                                self.config,
                                self.broadcast_callback,
                                self.booth_id,
                                self.room_id,
                            )
                except asyncio.IncompleteReadError:
                    logger.error(f"[{self.booth_id}][{self.session_id}] ffmpeg stream ended abruptly. Retrying...")
                except asyncio.CancelledError:
                    logger.info(f"[{self.booth_id}][{self.session_id}] Transcription worker cancelled.")
                    raise
                except Exception as e:
                    if not self.ready_event.is_set():
                        self.start_error = str(e) or e.__class__.__name__
                        self.ready_event.set()
                        logger.error(f"[{self.booth_id}][{self.session_id}] Worker startup failed: {e}")
                        break
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
            if not self.ready_event.is_set():
                if not self.start_error:
                    self.start_error = "Worker stopped before becoming ready"
                self.ready_event.set()
            async with active_workers_lock:
                if active_workers.get(self.booth_id) is self:
                    active_workers.pop(self.booth_id, None)
            if self.started_webhook_emitted:
                await _wh_worker.enqueue_webhook(
                    "booth.transcription.stopped",
                    {"booth_id": self.booth_id, "session_id": self.session_id, "event_slug": self.event_slug},
                )
            if self.provider_name == "local":
                from portal.transcription.providers.local import decrement_model_ref

                decrement_model_ref(self.model_size)
            logger.info(f"[{self.booth_id}][{self.session_id}] Transcription worker exited and cleaned up cleanly.")


active_workers: Dict[str, TranscriptionWorkerSession] = {}


def _worker_config_matches(
    session: TranscriptionWorkerSession,
    provider: str,
    model_size: str,
    config: ProviderConfig | None,
    transcription_language: str | None = None,
    language_code: str | None = None,
) -> bool:
    if session.provider_name != provider:
        return False
    if session.model_size != model_size:
        return False

    session_lang = (session.transcription_language or session.language_code or "").strip().lower()
    fallback_lang = (session.language_code or "").strip().lower()
    incoming_lang = (transcription_language or language_code or fallback_lang or "").strip().lower()
    if session_lang != incoming_lang:
        return False

    existing_key = (getattr(session.config, "api_key", None) or "") if session.config else ""
    new_key = (getattr(config, "api_key", None) or "") if config else ""
    if not existing_key and not new_key:
        return True
    return hmac.compare_digest(str(existing_key).encode("utf-8"), str(new_key).encode("utf-8"))


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
    wait_for_ready: bool = False,
) -> TranscriptionStartResult:
    session_to_wait: TranscriptionWorkerSession | None = None
    is_new_session = False

    while True:
        async with active_workers_lock:
            existing_session = active_workers.get(booth_id)
            if not existing_session:
                if len(active_workers) >= MAX_TOTAL_WORKERS:
                    raise ValueError(
                        f"System at maximum capacity ({MAX_TOTAL_WORKERS} concurrent transcription booths)."
                    )

                if provider == "local":
                    from portal.transcription.providers.local import increment_model_ref, start_eviction_loop

                    increment_model_ref(model_size)
                    start_eviction_loop()

                new_session = TranscriptionWorkerSession(
                    event_slug,
                    language_code,
                    booth_id,
                    broadcast_callback,
                    provider,
                    model_size,
                    config,
                    transcription_language,
                    room_id,
                )
                active_workers[booth_id] = new_session
                new_session.start()
                session_to_wait = new_session
                is_new_session = True
                break

            if existing_session.state in (State.STOPPING, State.STOPPED):
                if existing_session.state == State.STOPPED:
                    # It's fully dead. Pop it and let the loop recreate it immediately.
                    if active_workers.get(booth_id) is existing_session:
                        active_workers.pop(booth_id, None)
                    continue
                # Session is tearing down in the background. We must strictly serialize.
                # Drop lock, actively await the old session's death, then loop around and re-check registry.
                pass
            else:
                if _worker_config_matches(
                    existing_session,
                    provider,
                    model_size,
                    config,
                    transcription_language=transcription_language,
                    language_code=language_code,
                ):
                    if existing_session.state == State.RUNNING:
                        logger.info(f"Transcription worker for {booth_id} is already running with matching config.")
                        return TranscriptionStartResult(
                            status="already_running",
                            provider=existing_session.provider_name,
                            model=existing_session.model_size,
                        )

                    # Existing session is in State.STARTING!
                    # Wait for its readiness rather than assuming success.
                    session_to_wait = existing_session
                    is_new_session = False
                    break

                logger.info(f"[{booth_id}] Configuration changed for running worker; restarting with new settings.")
                existing_session.stop()
                pass

        # We dropped the lock and the session is STOPPING. Wait for it to die completely.
        logger.info(
            f"[{booth_id}] start_transcription_worker actively waiting for old session [{existing_session.session_id}] to STOP."
        )
        startup_timeout = getattr(settings, "transcription_startup_timeout_secs", 5.0)
        try:
            await asyncio.wait_for(existing_session.wait_until_stopped(), timeout=startup_timeout)
        except TimeoutError:
            logger.error(
                f"[{booth_id}][{existing_session.session_id}] Timed out waiting for stopping session to terminate. Retaining session in registry."
            )
            return TranscriptionStartResult(
                status="failed",
                provider=provider,
                model=model_size,
                error="Timed out waiting for existing worker to stop",
            )

    if wait_for_ready and session_to_wait is not None:
        startup_timeout = getattr(settings, "transcription_startup_timeout_secs", 5.0)
        try:
            await asyncio.wait_for(session_to_wait.ready_event.wait(), timeout=startup_timeout)
        except TimeoutError:
            logger.warning(f"[{booth_id}][{session_to_wait.session_id}] Startup timed out waiting for audio process.")
            session_to_wait.start_error = "Startup timed out waiting for audio process"
            session_to_wait.stop()
            try:
                await asyncio.wait_for(session_to_wait.wait_until_stopped(), timeout=startup_timeout)
                async with active_workers_lock:
                    if active_workers.get(booth_id) is session_to_wait:
                        active_workers.pop(booth_id, None)
            except TimeoutError:
                logger.error(
                    f"[{booth_id}][{session_to_wait.session_id}] Teardown timed out after startup failure. Retaining session in registry to prevent duplicate workers."
                )
            return TranscriptionStartResult(
                status="failed",
                provider=provider,
                model=model_size,
                error=session_to_wait.start_error,
            )

        if session_to_wait.start_error:
            return TranscriptionStartResult(
                status="failed",
                provider=provider,
                model=model_size,
                error=session_to_wait.start_error,
            )

    return TranscriptionStartResult(
        status="started" if is_new_session else "already_running",
        provider=provider if is_new_session else session_to_wait.provider_name,
        model=model_size if is_new_session else session_to_wait.model_size,
    )


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
                active_workers.pop(booth_id, None)
