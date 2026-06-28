from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

import websockets

from portal.tts.constants import get_deepgram_voice_for_language
from portal.tts.providers.base import AudioCallback, TTSProvider

logger = logging.getLogger(__name__)

# Sentence boundary regex — split LLM output into sentences for the Speak API.
SENTENCE_BOUNDARY = re.compile(r"([^.?!]+[.?!]+)")


class DeepgramTTSProvider(TTSProvider):
    """Deepgram Aura WebSocket TTS provider.

    Preserves the original streaming behaviour: a single Deepgram WebSocket is
    opened per language, translated sentences are streamed to the Speak API as
    soon as they are complete, and PCM audio frames are broadcast as they arrive.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def synthesize_stream(
        self,
        *,
        text_chunks: AsyncIterator[str],
        language_code: str,
        voice: str,
        on_audio: AudioCallback,
    ) -> None:
        # The room-level voice field is shared across providers; a value that is
        # not a Deepgram Aura model (e.g. a Supertonic preset like "M1") must be
        # ignored so Deepgram falls back to a valid language-mapped voice.
        dg_voice = voice if voice.startswith("aura") else get_deepgram_voice_for_language(language_code)
        # Using linear16 at 24000Hz. Can be adjusted based on requirements.
        dg_url = f"wss://api.deepgram.com/v1/speak?model={dg_voice}&encoding=linear16&sample_rate=24000&container=none"

        try:
            # We connect to Deepgram WS first
            async with websockets.connect(
                dg_url, additional_headers={"Authorization": f"Token {self.api_key}"}
            ) as dg_socket:
                # Start a background task to receive audio chunks and broadcast them
                async def receive_audio():
                    try:
                        async for message in dg_socket:
                            if isinstance(message, bytes):
                                await on_audio(message)
                            elif isinstance(message, str):
                                try:
                                    msg_json = json.loads(message)
                                    if msg_json.get("type") == "Error":
                                        logger.error(f"[TTS] Deepgram Error: {msg_json}")
                                except json.JSONDecodeError:
                                    pass
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as e:
                        logger.error(f"[TTS] Receive error: {e}")

                receive_task = asyncio.create_task(receive_audio())

                # Now stream from LLM and send Speak requests
                buffer = ""
                async for chunk in text_chunks:
                    buffer += chunk

                    # Send complete sentences to Deepgram
                    while True:
                        match = SENTENCE_BOUNDARY.search(buffer)
                        if not match:
                            break

                        sentence = match.group(1).strip()
                        split_idx = match.end()

                        if sentence:
                            await dg_socket.send(json.dumps({"type": "Speak", "text": sentence}))

                        buffer = buffer[split_idx:].lstrip()

                # Send any remaining text
                remaining = buffer.strip()
                if remaining:
                    await dg_socket.send(json.dumps({"type": "Speak", "text": remaining}))

                # Flush to tell Deepgram we are done generating for this segment
                await dg_socket.send(json.dumps({"type": "Flush"}))

                # Wait a short moment for remaining audio to arrive, then close.
                # Deepgram sends a 'Flushed' metadata message when done; we await
                # the receive task or time out after a few seconds to be safe.
                try:
                    await asyncio.wait_for(receive_task, timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        except Exception as e:
            logger.error(f"[TTS] Error in Deepgram TTS stream for lang {language_code}: {e}")
