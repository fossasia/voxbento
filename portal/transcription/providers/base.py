import asyncio
import io
import logging
import time
import wave
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterator, Awaitable, Callable

from portal.models import Event
from portal.transcription.constants import ProviderEnum

logger = logging.getLogger(__name__)


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def get_api_key(event: Event, provider: ProviderEnum) -> str | None:
    from portal.crypto import decrypt_val

    key_map = {
        ProviderEnum.OPENAI: event.encrypted_openai_api_key,
        ProviderEnum.DEEPGRAM: event.encrypted_deepgram_api_key,
        ProviderEnum.NVIDIA: event.encrypted_nvidia_api_key,
        ProviderEnum.ELEVENLABS: event.encrypted_elevenlabs_api_key,
    }
    encrypted = key_map.get(provider)
    return decrypt_val(encrypted) if encrypted else None


@dataclass
class ProviderConfig:
    api_key: str | None

    def get_key(self) -> str | None:
        return self.api_key


@dataclass
class BoothTranscriptionState:
    booth_id: str
    overlap_buffer: bytes = b""
    chunks_dropped_total: int = 0
    consecutive_drops: int = 0
    inference_latency_avg_ms: float = 0.0


class TranscriptionProvider:
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        raise NotImplementedError

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
        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        chunk_size_bytes = 16000 * 2 * 3  # 3 seconds
        queue = asyncio.Queue(maxsize=2)
        booth_state = BoothTranscriptionState(booth_id=booth_id)

        async def audio_reader_task():
            while process.returncode is None:
                try:
                    chunk = await process.stdout.readexactly(chunk_size_bytes)
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial
                    if chunk:
                        try:
                            queue.put_nowait(chunk)
                        except asyncio.QueueFull:
                            pass
                    break
                except Exception as e:
                    logger.error(f"[{booth_id}] Reader error: {e}")
                    break

                if not chunk:
                    break

                if queue.full():
                    try:
                        queue.get_nowait()
                        booth_state.chunks_dropped_total += 1
                        booth_state.consecutive_drops += 1
                        logger.warning(
                            f"[{booth_id}] Inference lagging: dropped oldest audio chunk. Total dropped: {booth_state.chunks_dropped_total}"
                        )
                    except asyncio.QueueEmpty:
                        pass

                try:
                    queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

            await queue.put(None)

        async def inference_task():
            consecutive_errors = 0
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break

                if booth_state.consecutive_drops > 3:
                    logger.error(f"[{booth_id}] Overload Protection triggered. Pausing inference for 10s.")
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await broadcast_callback(booth_id, "[Server overloaded - transcription temporarily paused]")
                    await asyncio.sleep(10)
                    booth_state.consecutive_drops = 0
                    consecutive_errors = 0
                    continue

                t0 = time.time()
                try:
                    text = await self.process_chunk(
                        chunk, language_code, model_variant, config, booth_state=booth_state
                    )
                    consecutive_errors = 0
                    booth_state.consecutive_drops = 0

                    if text:
                        logger.debug(f"[{booth_id}] Transcribed: {text}")
                        await aggregator.handle_chunk(booth_id, text)
                    else:
                        await aggregator.handle_clear(booth_id)
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"[{booth_id}] Provider error ({consecutive_errors}/3): {e}")
                    if consecutive_errors >= 3:
                        await broadcast_callback(booth_id, "[Transcription provider failed. Check logs.]")
                        break
                finally:
                    latency = (time.time() - t0) * 1000
                    if booth_state.inference_latency_avg_ms == 0:
                        booth_state.inference_latency_avg_ms = latency
                    else:
                        booth_state.inference_latency_avg_ms = (
                            0.8 * booth_state.inference_latency_avg_ms + 0.2 * latency
                        )

                    if latency > 3000:
                        logger.warning(f"[{booth_id}] Inference slow: {latency:.0f}ms")

        reader = asyncio.create_task(audio_reader_task())
        inference = asyncio.create_task(inference_task())

        await asyncio.wait([reader, inference], return_when=asyncio.FIRST_COMPLETED)

        reader.cancel()
        inference.cancel()


@dataclass(frozen=True)
class AudioFrame:
    """
    Represents a discrete chunk of audio data.
    `start_timestamp` is audio-relative (derived strictly from bytes read / sample rate),
    NOT monotonic wall-clock, ensuring it strictly aligns with the audio stream's true duration.
    """
    data: bytes
    start_timestamp: float
    duration: float
    seq: int

class AudioIngester:
    def __init__(self, process: asyncio.subprocess.Process, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2):
        self.process = process
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.bytes_per_second = sample_rate * channels * sample_width

    async def stream(self, chunk_size: int = 4096) -> AsyncGenerator[AudioFrame, None]:
        seq = 0
        total_bytes = 0
        while self.process.returncode is None:
            try:
                chunk = await self.process.stdout.read(chunk_size)
                if not chunk:
                    break
                duration = len(chunk) / self.bytes_per_second
                start_timestamp = total_bytes / self.bytes_per_second
                yield AudioFrame(
                    data=chunk,
                    start_timestamp=start_timestamp,
                    duration=duration,
                    seq=seq
                )
                seq += 1
                total_bytes += len(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"AudioIngester error: {e}")
                break

class StreamingProvider:
    async def process_stream(
        self,
        audio_generator: AsyncIterator[AudioFrame],
        aggregator,
        notify_gap: Callable[[float, float], Awaitable[None]],
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_id: str,
    ) -> None:
        raise NotImplementedError

class ContinuousProvider(StreamingProvider):
    async def process_stream(
        self,
        audio_generator: AsyncIterator[AudioFrame],
        aggregator,
        notify_gap: Callable[[float, float], Awaitable[None]],
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_id: str,
    ) -> None:
        self.aggregator = aggregator
        self.notify_gap = notify_gap
        self.language_code = language_code
        self.model_variant = model_variant
        self.config = config
        self.booth_id = booth_id

        self.queue = asyncio.Queue()
        self.queue_duration = 0.0
        self.MAX_QUEUE_DURATION = 10.0
        self.eof_event = asyncio.Event()
        self._dropped_start = None
        self._dropped_end = None

        self.consumer_task = asyncio.create_task(self._consume_generator(audio_generator))

        try:
            await self._run_reconnect_loop()
        finally:
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except asyncio.CancelledError:
                pass

    async def _consume_generator(self, audio_generator: AsyncIterator[AudioFrame]):
        try:
            async for frame in audio_generator:
                self.queue.put_nowait(frame)
                self.queue_duration += frame.duration

                while self.queue_duration > self.MAX_QUEUE_DURATION:
                    try:
                        dropped = self.queue.get_nowait()
                        self.queue_duration -= dropped.duration
                        if self._dropped_start is None:
                            self._dropped_start = dropped.start_timestamp
                        self._dropped_end = dropped.start_timestamp + dropped.duration
                    except asyncio.QueueEmpty:
                        break

            await self.queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.booth_id}] Error in audio consumer: {e}")
            await self.queue.put(None)
        finally:
            self.eof_event.set()

    async def _run_reconnect_loop(self):
        retries = 0
        while retries <= 5:
            try:
                if self._dropped_start is not None:
                    await self.notify_gap(self._dropped_start, self._dropped_end)
                    self._dropped_start = None
                    self._dropped_end = None

                await self.connect_and_stream()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                retries += 1
                if retries > 5:
                    logger.error(f"[{self.booth_id}] Terminal error: {e}")
                    if hasattr(self.aggregator, "broadcast_callback"):
                        await self.aggregator.broadcast_callback(self.booth_id, "[Server disconnected: transcription failed. Please refresh.]")
                    return

                backoff = min(10.0, 2 ** retries)
                logger.warning(f"[{self.booth_id}] Connection failed: {e}. Reconnecting in {backoff}s...")

                try:
                    await asyncio.wait_for(self.eof_event.wait(), timeout=backoff)
                    logger.info(f"[{self.booth_id}] EOF reached during reconnect backoff. Terminating cleanly.")
                    return
                except asyncio.TimeoutError:
                    pass

    async def connect_and_stream(self):
        raise NotImplementedError

class ChunkedProvider(StreamingProvider):
    async def process_block(self, audio_bytes: bytes, start_timestamp: float, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        raise NotImplementedError

    async def process_stream(
        self,
        audio_generator: AsyncIterator[AudioFrame],
        aggregator,
        notify_gap: Callable[[float, float], Awaitable[None]],
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_id: str,
    ) -> None:
        self.aggregator = aggregator
        self.notify_gap = notify_gap
        self.language_code = language_code
        self.model_variant = model_variant
        self.config = config
        self.booth_id = booth_id

        self.MAX_PENDING_BLOCKS = 3
        self.BLOCK_DURATION = 3.0

        self.block_queue = asyncio.Queue()
        self.consumer_task = asyncio.create_task(self._assemble_blocks(audio_generator))

        consecutive_errors = 0
        try:
            while True:
                block_data = await self.block_queue.get()
                if block_data is None:
                    break

                audio_bytes, start_ts, duration, is_partial = block_data

                try:
                    text = await self.process_block(audio_bytes, start_ts, language_code, model_variant, config)
                    consecutive_errors = 0
                    if text:
                        await self.aggregator.handle_chunk(self.booth_id, text)
                    else:
                        await self.aggregator.handle_clear(self.booth_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"[{self.booth_id}] Provider error ({consecutive_errors}/3): {e}")
                    if consecutive_errors >= 3:
                        if hasattr(self.aggregator, "broadcast_callback"):
                            await self.aggregator.broadcast_callback(self.booth_id, "[Transcription provider failed. Check logs.]")
                        break
        except asyncio.CancelledError:
            raise
        finally:
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except asyncio.CancelledError:
                pass

    async def _assemble_blocks(self, audio_generator: AsyncIterator[AudioFrame]):
        current_block = bytearray()
        current_duration = 0.0
        start_ts = None

        try:
            async for frame in audio_generator:
                if start_ts is None:
                    start_ts = frame.start_timestamp

                current_block.extend(frame.data)
                current_duration += frame.duration

                if current_duration >= self.BLOCK_DURATION:
                    await self._enqueue_block(bytes(current_block), start_ts, current_duration, False)
                    current_block = bytearray()
                    current_duration = 0.0
                    start_ts = None

            if current_block:
                await self._enqueue_block(bytes(current_block), start_ts, current_duration, True)
            await self.block_queue.put(None)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.booth_id}] Error assembling blocks: {e}")
            await self.block_queue.put(None)

    async def _enqueue_block(self, audio_bytes: bytes, start_ts: float, duration: float, is_partial: bool):
        while self.block_queue.qsize() >= self.MAX_PENDING_BLOCKS:
            try:
                dropped = self.block_queue.get_nowait()
                if dropped is not None:
                    _, drop_start, drop_duration, _ = dropped
                    await self.notify_gap(drop_start, drop_start + drop_duration)
            except asyncio.QueueEmpty:
                break

        self.block_queue.put_nowait((audio_bytes, start_ts, duration, is_partial))
