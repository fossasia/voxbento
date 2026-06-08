import asyncio
import io
import logging
import wave
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

class TranscriptionProvider:
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        raise NotImplementedError

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        consecutive_errors = 0
        chunk_size_bytes = 16000 * 2 * 3 # 3 seconds
        
        while True:
            try:
                chunk = await process.stdout.readexactly(chunk_size_bytes)
            except asyncio.IncompleteReadError as e:
                chunk = e.partial
                if not chunk:
                    break
                    
            if not chunk:
                break
                
            try:
                text = await self.process_chunk(chunk, language_code, model_variant, config)
                consecutive_errors = 0
                
                if text:
                    logger.debug(f"[{booth_id}] Transcribed: {text}")
                    await broadcast_callback(booth_id, text)
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] Provider error ({consecutive_errors}/3): {e}")
                if consecutive_errors >= 3:
                    await broadcast_callback(booth_id, "[Transcription provider failed. Check logs.]")
                    break
