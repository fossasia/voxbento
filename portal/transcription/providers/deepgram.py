from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlencode

import websockets
from sqlalchemy.exc import SQLAlchemyError
from websockets.exceptions import InvalidStatus

from portal.database import get_session
from portal.transcription.aggregator import CaptionAggregator
from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider
from portal.translations.vocabulary import speech_keyterms

logger = logging.getLogger(__name__)

LISTEN_URL = "wss://api.deepgram.com/v1/listen"
# Silence that ends a phrase. Short enough that captions settle quickly, long enough that a
# breath mid-sentence does not cut it in two before translation.
ENDPOINTING_MS = 800
# Closes a phrase when background noise keeps endpointing from ever firing (Deepgram's minimum is 1000).
UTTERANCE_END_MS = 1500
# Keyterm prompting exists on Nova-3 only; Nova-2 rejects it and takes boosted keywords instead.
KEYTERM_MODEL_PREFIXES = ("nova-3",)
# Deepgram's keyword boost is exponential and large values make the model hear the term in noise.
# 2 is a modest nudge: names it already half-hears get spelled right, without false insertions.
KEYWORD_BOOST = 2
# Terms are optional: a slow glossary lookup must not hold up captions.
GLOSSARY_LOOKUP_TIMEOUT = 2.0
RECONNECT_DELAY_SECONDS = 2.0
MAX_CONSECUTIVE_ERRORS = 5


def supports_keyterms(model: str) -> bool:
    return model.startswith(KEYTERM_MODEL_PREFIXES)


def build_listen_url(model: str, language_code: str, keyterms: list[str] | tuple[str, ...] = ()) -> str:
    """The streaming URL for one booth. No language lets Deepgram use its English default."""
    params = [
        ("model", model),
        ("encoding", "linear16"),
        ("sample_rate", "16000"),
        ("channels", "1"),
        ("interim_results", "true"),
        ("endpointing", str(ENDPOINTING_MS)),
        ("utterance_end_ms", str(UTTERANCE_END_MS)),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("numerals", "true"),
    ]
    if language_code and language_code != "floor":
        params.append(("language", language_code))
    if supports_keyterms(model):
        params.extend(("keyterm", term) for term in keyterms)
    else:
        params.extend(("keywords", f"{term}:{KEYWORD_BOOST}") for term in keyterms)
    return f"{LISTEN_URL}?{urlencode(params)}"


async def _read_keyterms(room_id: int) -> list[str]:
    async with get_session() as session:
        return await speech_keyterms(session, room_id)


async def load_keyterms(room_id: int | None) -> list[str]:
    """The room's glossary terms, or none when they cannot be read in time: captions without them beat none."""
    if room_id is None:
        return []
    try:
        return await asyncio.wait_for(_read_keyterms(room_id), timeout=GLOSSARY_LOOKUP_TIMEOUT)
    except SQLAlchemyError:
        logger.exception("Could not load glossary terms for room %s", room_id)
    except TimeoutError:
        logger.warning(
            "Glossary lookup for room %s took over %ss; captions start without it", room_id, GLOSSARY_LOOKUP_TIMEOUT
        )
    return []


async def handle_message(aggregator: CaptionAggregator, booth_id: str, data: dict) -> None:
    """Turn one Deepgram message into caption updates.

    Deepgram marks a stretch of audio final every few seconds, often mid-sentence; the phrase
    only ends at ``speech_final`` or ``UtteranceEnd``. Final stretches are collected by the
    aggregator, which publishes whole sentences, so translation never gets half a sentence.
    """
    if data.get("type") == "UtteranceEnd":
        # -1 means the phrase was already finalized; Deepgram says to ignore the stale notice, which
        # would otherwise clear the preview of the next phrase.
        if data.get("last_word_end") != -1:
            await aggregator.handle_clear(booth_id)
        return
    try:
        transcript = data["channel"]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return
    if data.get("is_final"):
        if transcript:
            await aggregator.handle_chunk(booth_id, transcript)
        if data.get("speech_final"):
            await aggregator.handle_clear(booth_id)
    elif transcript:
        await aggregator.handle_interim(booth_id, transcript)


class DeepgramProvider(TranscriptionProvider):
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
        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)

        api_key = config.get_key()
        if not api_key:
            logger.error("[%s] Deepgram API key missing", booth_id)
            return

        keyterms = await load_keyterms(room_id)
        if keyterms:
            logger.info("[%s] Deepgram %s listening for %d glossary terms", booth_id, model_variant, len(keyterms))
        url = build_listen_url(model_variant, language_code, keyterms)
        headers = {"Authorization": f"Token {api_key}"}

        consecutive_errors = 0
        while process.returncode is None:
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    consecutive_errors = 0

                    async def sender():
                        try:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=5.0)
                                    if not chunk:
                                        return "EOF"
                                    await ws.send(chunk)
                                except asyncio.TimeoutError:
                                    await ws.send(json.dumps({"type": "KeepAlive"}))
                        except (OSError, websockets.exceptions.WebSocketException) as e:
                            logger.error("[%s] Deepgram WS sender error: %s", booth_id, e)
                            return "ERROR"

                    async def receiver():
                        try:
                            async for msg in ws:
                                try:
                                    data = json.loads(msg)
                                except json.JSONDecodeError:
                                    continue
                                if isinstance(data, dict):
                                    await handle_message(aggregator, booth_id, data)
                        except websockets.exceptions.WebSocketException as e:
                            logger.error("[%s] Deepgram WS receiver error: %s", booth_id, e)
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in done:
                        # A caption or database failure must not vanish with its task; reconnect after it.
                        if task.exception() is not None:
                            logger.error("[%s] Deepgram stream task failed", booth_id, exc_info=task.exception())

                    if sender_task in done and sender_task.exception() is None and sender_task.result() == "EOF":
                        try:
                            await ws.send(json.dumps({"type": "CloseStream"}))
                            await receiver_task
                        except asyncio.CancelledError:
                            raise
                        except websockets.exceptions.WebSocketException as e:
                            logger.warning("[%s] Deepgram receiver error at end of stream: %s", booth_id, e)
                        # Whatever was still being said when the audio ended.
                        await aggregator.handle_clear(booth_id)
                        for task in pending:
                            task.cancel()
                        return  # Clean exit

                    for task in pending:
                        task.cancel()

            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
                if keyterms and isinstance(e, InvalidStatus) and e.response.status_code == 400:
                    # Deepgram counts keyterm tokens with its own tokenizer, so a glossary inside the
                    # estimated budget can still be refused. Captions without the glossary beat none.
                    logger.warning(
                        "[%s] Deepgram refused the glossary terms (HTTP 400); continuing without them", booth_id
                    )
                    keyterms = []
                    url = build_listen_url(model_variant, language_code)
                    continue
                consecutive_errors += 1
                logger.error("[%s] Deepgram connection failed (%d): %s", booth_id, consecutive_errors, e)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error("[%s] Deepgram Realtime connection repeatedly failed. Giving up.", booth_id)
                    break
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

        # The stream ended without a clean end of audio (connection given up, ffmpeg gone): publish
        # the settled words still waiting for the end of their sentence, so they are not lost.
        await aggregator.handle_clear(booth_id)
