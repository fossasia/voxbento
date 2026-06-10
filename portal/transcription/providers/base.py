import asyncio
import io
import logging
import wave
import time
from dataclasses import dataclass

from portal.models import Event
from portal.transcription.constants import ProviderEnum

logger = logging.getLogger(__name__)

def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
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
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig, booth_state: BoothTranscriptionState | None = None) -> str:
        raise NotImplementedError

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        from portal.transcription.aggregator import CaptionAggregator
        aggregator = CaptionAggregator(broadcast_callback)
        chunk_size_bytes = 16000 * 2 * 3 # 3 seconds
        queue = asyncio.Queue(maxsize=2)
        booth_state = BoothTranscriptionState(booth_id=booth_id)
        
        async def audio_reader_task():
            while process.returncode is None:
                try:
                    chunk = await process.stdout.readexactly(chunk_size_bytes)
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial
                    if chunk:
                        try: queue.put_nowait(chunk)
                        except asyncio.QueueFull: pass
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
                        logger.warning(f"[{booth_id}] Inference lagging: dropped oldest audio chunk. Total dropped: {booth_state.chunks_dropped_total}")
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
                        try: queue.get_nowait()
                        except asyncio.QueueEmpty: break
                    await broadcast_callback(booth_id, "[Server overloaded - transcription temporarily paused]")
                    await asyncio.sleep(10)
                    booth_state.consecutive_drops = 0
                    consecutive_errors = 0
                    continue

                t0 = time.time()
                try:
                    text = await self.process_chunk(chunk, language_code, model_variant, config, booth_state=booth_state)
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
                        booth_state.inference_latency_avg_ms = 0.8 * booth_state.inference_latency_avg_ms + 0.2 * latency
                        
                    if latency > 3000:
                        logger.warning(f"[{booth_id}] Inference slow: {latency:.0f}ms")

        reader = asyncio.create_task(audio_reader_task())
        inference = asyncio.create_task(inference_task())
        
        await asyncio.wait([reader, inference], return_when=asyncio.FIRST_COMPLETED)
        
        reader.cancel()
        inference.cancel()
