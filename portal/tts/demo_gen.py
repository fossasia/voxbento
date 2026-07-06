from __future__ import annotations

import asyncio
import json
import logging
import struct
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where pre-baked demo audio lives. Served by FastAPI StaticFiles.
DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "audio" / "demo"
MANIFEST_PATH = DEMO_DIR / "manifest.json"

# Generation state, shared with portal.routers.demo and fastapi_app's lifespan.
_generating = False
_generation_error: str | None = None
_generation_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def track_task(task: asyncio.Task) -> None:
    """Keep a strong reference to a background task so it isn't garbage
    collected mid-run, and log any exception it raises once it completes."""
    _tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("[Demo] Background task failed", exc_info=t.exception())

    task.add_done_callback(_on_done)

# ---------------------------------------------------------------------------
# Demo content — scripts pre-written in each language so no translation API
# is needed; Supertonic generates the audio locally at first startup.
# ---------------------------------------------------------------------------

DEMO_VIDEO_URL = (
    "https://videos.pexels.com/video-files/3253441/3253441-sd_640_360_25fps.mp4"
)

# Each language has its own pre-written script so the translated audio sounds
# natural without a real-time LLM call.
DEMO_LANGUAGES: list[dict[str, Any]] = [
    {
        "code": "en",
        "name": "English",
        "segments": [
            {"text": "Welcome to the Global Innovation Summit.", "start_ms": 0, "end_ms": 3200},
            {"text": "Today we are proud to announce a breakthrough in renewable energy technology that will reshape how we power our cities.", "start_ms": 3200, "end_ms": 10000},
            {"text": "Over the next decade, this technology promises to reduce carbon emissions by fifty percent while creating millions of new jobs worldwide.", "start_ms": 10000, "end_ms": 18500},
            {"text": "Join us as we take the first steps toward a cleaner, more connected future.", "start_ms": 18500, "end_ms": 24000},
        ],
    },
    {
        "code": "fr",
        "name": "French",
        "segments": [
            {"text": "Bienvenue au Sommet Mondial de l'Innovation.", "start_ms": 0, "end_ms": 3200},
            {"text": "Aujourd'hui, nous sommes fiers d'annoncer une avancée majeure dans les technologies d'énergie renouvelable qui va transformer notre façon d'alimenter nos villes.", "start_ms": 3200, "end_ms": 10000},
            {"text": "Au cours de la prochaine décennie, cette technologie promet de réduire les émissions de carbone de cinquante pour cent tout en créant des millions de nouveaux emplois dans le monde.", "start_ms": 10000, "end_ms": 18500},
            {"text": "Rejoignez-nous pour franchir les premières étapes vers un avenir plus propre et plus connecté.", "start_ms": 18500, "end_ms": 24000},
        ],
    },
    {
        "code": "es",
        "name": "Spanish",
        "segments": [
            {"text": "Bienvenidos a la Cumbre Mundial de Innovación.", "start_ms": 0, "end_ms": 3200},
            {"text": "Hoy nos enorgullece anunciar un avance en tecnología de energía renovable que transformará la forma en que alimentamos nuestras ciudades.", "start_ms": 3200, "end_ms": 10000},
            {"text": "En la próxima década, esta tecnología promete reducir las emisiones de carbono en un cincuenta por ciento mientras crea millones de nuevos empleos en todo el mundo.", "start_ms": 10000, "end_ms": 18500},
            {"text": "Únase a nosotros mientras damos los primeros pasos hacia un futuro más limpio y más conectado.", "start_ms": 18500, "end_ms": 24000},
        ],
    },
    {
        "code": "de",
        "name": "German",
        "segments": [
            {"text": "Willkommen beim Globalen Innovationsgipfel.", "start_ms": 0, "end_ms": 3200},
            {"text": "Heute sind wir stolz, einen Durchbruch in der Technologie erneuerbarer Energien anzukündigen, der die Art und Weise, wie wir unsere Städte mit Energie versorgen, grundlegend verändern wird.", "start_ms": 3200, "end_ms": 10000},
            {"text": "Im nächsten Jahrzehnt verspricht diese Technologie, die Kohlenstoffemissionen um fünfzig Prozent zu reduzieren und gleichzeitig Millionen neuer Arbeitsplätze weltweit zu schaffen.", "start_ms": 10000, "end_ms": 18500},
            {"text": "Begleiten Sie uns auf den ersten Schritten in eine sauberere und stärker vernetzte Zukunft.", "start_ms": 18500, "end_ms": 24000},
        ],
    },
]


def _pcm_to_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1, bit_depth: int = 16) -> bytes:
    """Wrap raw PCM bytes in a RIFF WAV header."""
    byte_rate = sample_rate * channels * bit_depth // 8
    block_align = channels * bit_depth // 8
    data_len = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bit_depth,
        b"data",
        data_len,
    )
    return header + pcm


async def generate_demo_assets() -> dict[str, Any]:
    """Generate pre-baked WAV audio for every language in DEMO_LANGUAGES.

    Uses the Supertonic provider (no external API key required). Writes WAV
    files to ``static/audio/demo/<lang>.wav`` and updates the manifest.
    Returns the manifest dict.
    """
    from portal.tts.providers.supertonic import SupertonicTTSProvider

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    provider = SupertonicTTSProvider()

    generated: list[dict[str, Any]] = []

    for lang in DEMO_LANGUAGES:
        code: str = lang["code"]
        name: str = lang["name"]
        segments: list[dict] = lang["segments"]
        full_text = " ".join(seg["text"] for seg in segments)

        chunks: list[bytes] = []

        async def collect(audio: bytes) -> None:
            chunks.append(audio)

        async def text_iter(t: str = full_text):
            yield t

        try:
            logger.info("[Demo] Generating %s audio...", name)
            await provider.synthesize_stream(
                text_chunks=text_iter(),
                language_code=code,
                voice="",
                on_audio=collect,
            )
        except Exception:
            logger.exception("[Demo] TTS generation failed for %s", name)
            continue

        pcm = b"".join(chunks)
        if not pcm:
            logger.warning("[Demo] No audio produced for %s", name)
            continue

        wav = _pcm_to_wav(pcm)
        out_path = DEMO_DIR / f"{code}.wav"
        out_path.write_bytes(wav)
        logger.info("[Demo] %s audio written: %d bytes", name, len(wav))

        generated.append({
            "code": code,
            "name": name,
            "audio_url": f"/static/audio/demo/{code}.wav",
            "segments": segments,
        })

    manifest: dict[str, Any] = {
        "video_url": DEMO_VIDEO_URL,
        "languages": generated,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    logger.info("[Demo] Manifest written with %d languages.", len(generated))
    return manifest


async def ensure_demo_generated() -> None:
    """Generate demo assets if they don't already exist. Safe to call at startup."""
    global _generation_error
    if MANIFEST_PATH.exists():
        return
    logger.info("[Demo] No manifest found — generating demo audio (first run)...")
    try:
        await generate_demo_assets()
        _generation_error = None
    except Exception as exc:
        logger.exception("[Demo] Background demo generation failed")
        _generation_error = str(exc)


def load_manifest() -> dict[str, Any] | None:
    """Return the manifest dict, or None if not yet generated."""
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except Exception:
        logger.exception("Failed to load demo manifest")
        return None
