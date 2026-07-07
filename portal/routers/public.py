from __future__ import annotations

import logging

# We assume templates is initialized in fastapi_app and imported here, or
# we initialize it here. Usually, Jinja2Templates is shared or re-initialized.
# To avoid circular imports, let's re-initialize it.
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from portal.auth import get_current_user
from portal.booth_identity import make_booth_id
from portal.database import (
    get_session,
    list_all_booths_for_events,
    list_booth_memberships_for_user,
    list_events,
    list_memberships_for_user,
)
from portal.globals import booths
from portal.utils import _check_mediamtx

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()


@router.get("/")
async def home(request: Request):
    current_user = await get_current_user(request)
    my_booths = []

    try:
        async with get_session() as session:
            events = await list_events(session)

            user_event_roles = {}
            user_booth_roles = {}
            if current_user:
                uid = int(current_user["sub"])
                ems = await list_memberships_for_user(session, uid)
                user_event_roles = {em.event_id: em.role for em in ems}

                bms = await list_booth_memberships_for_user(session, uid)
                user_booth_roles = {bm.booth_id: bm.role for bm in bms}
                for bm in bms:
                    bid = make_booth_id(bm.booth.event.slug, bm.booth.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == "connected"
                    my_booths.append(
                        {
                            "membership": bm,
                            "booth_id": bid,
                            "is_live": is_live,
                            "event_name": bm.booth.event.display_name,
                            "language_name": bm.booth.language_name,
                            "event_slug": bm.booth.event.slug,
                            "language_code": bm.booth.language_code,
                        }
                    )

            event_ids = [ev.id for ev in events]
            booths_by_event = await list_all_booths_for_events(session, event_ids)

            event_data = []
            for ev in events:
                db_booths = booths_by_event.get(ev.id, [])
                booth_statuses = []
                for b in db_booths:
                    bid = make_booth_id(ev.slug, b.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == "connected"

                    can_interpret = False
                    if current_user:
                        is_admin = current_user.get("is_admin", False)
                        ev_role = user_event_roles.get(ev.id)
                        booth_role = user_booth_roles.get(b.id)
                        if is_admin or ev_role == "event_owner" or booth_role == "interpreter":
                            can_interpret = True

                    booth_statuses.append(
                        {"db": b, "booth_id": bid, "is_live": is_live, "can_interpret": can_interpret}
                    )
                event_data.append(
                    {
                        "event": ev,
                        "booths": booth_statuses,
                        "live_count": sum(1 for bs in booth_statuses if bs["is_live"]),
                    }
                )
    except Exception as _exc:
        logging.getLogger(__name__).warning("home() DB error: %s", _exc, exc_info=True)
        event_data = []

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "events": event_data,
            "current_user": current_user,
            "my_booths": my_booths,
        },
    )


@router.get("/research")
@router.get("/research/")
async def research_index(request: Request):
    return templates.TemplateResponse(request, "research/index.html", {})


@router.get("/research/speech-ai-statistics")
async def research_speech_ai_statistics(request: Request):
    return templates.TemplateResponse(request, "research/speech_ai_statistics.html", {})


@router.get("/research/whisper-benchmark")
async def research_whisper_benchmark(request: Request):
    return templates.TemplateResponse(request, "research/whisper_benchmark.html", {})


@router.get("/research/stt-comparison")
async def research_stt_comparison(request: Request):
    return templates.TemplateResponse(request, "research/stt_comparison.html", {})


@router.get("/research/real-time-audio-latency")
async def research_real_time_audio_latency(request: Request):
    return templates.TemplateResponse(request, "research/real_time_audio_latency.html", {})


@router.get("/research/webrtc-vs-rtmp-vs-hls")
async def research_webrtc_vs_rtmp_vs_hls(request: Request):
    return templates.TemplateResponse(request, "research/webrtc_vs_rtmp_vs_hls.html", {})


@router.get("/research/speech-ai-glossary")
async def research_speech_ai_glossary(request: Request):
    return templates.TemplateResponse(request, "research/speech_ai_glossary.html", {})


@router.get("/research/streaming-stt-architecture")
async def research_streaming_stt_architecture(request: Request):
    return templates.TemplateResponse(request, "research/streaming_stt_architecture.html", {})


@router.get("/llms.txt")
async def llms_txt(request: Request):
    content = """# VoxBento LLM Discoverability
Welcome to VoxBento's LLM directory.
We provide real-time browser-based interpretation with Speech AI integration.

## Documentation
- Architecture: /research/streaming-stt-architecture
- Benchmarks: /research/whisper-benchmark
- Statistics: /research/speech-ai-statistics

For full context, see /llms-full.txt
"""
    return PlainTextResponse(content)


@router.get("/llms-full.txt")
async def llms_full_txt(request: Request):
    content = """# VoxBento Comprehensive Context
VoxBento is a production-grade browser-first interpretation booth console for live events.

# Routes
- /research/speech-ai-statistics
- /research/whisper-benchmark
- /research/stt-comparison
- /research/real-time-audio-latency
- /research/webrtc-vs-rtmp-vs-hls
- /research/speech-ai-glossary
- /research/streaming-stt-architecture

See those pages for specific benchmarking data and methodologies.
"""
    return PlainTextResponse(content)


@router.get("/sitemap.xml")
async def sitemap(request: Request):
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://voxbento.org/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://voxbento.org/research</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://voxbento.org/research/speech-ai-statistics</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/whisper-benchmark</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/stt-comparison</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/real-time-audio-latency</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/webrtc-vs-rtmp-vs-hls</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/speech-ai-glossary</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/streaming-stt-architecture</loc>
    <changefreq>weekly</changefreq>
  </url>
</urlset>"""
    return Response(content=xml_content, media_type="application/xml")


@router.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "server": "fastapi",
        "mediamtx_ok": await _check_mediamtx(),
    }
