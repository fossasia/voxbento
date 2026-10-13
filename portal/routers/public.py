from __future__ import annotations

import logging

# We assume templates is initialized in fastapi_app and imported here, or
# we initialize it here. Usually, Jinja2Templates is shared or re-initialized.
# To avoid circular imports, let's re-initialize it.
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from portal.auth import get_admin_flags, get_current_user
from portal.database import (
    get_session,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
)
from portal.email_sender import send_demo_request_email
from portal.globals import _JS_CACHE_BUST
from portal.utils import _check_mediamtx

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()


@router.get("/")
async def home(request: Request):
    current_user = await get_current_user(request)
    admin_flags = await get_admin_flags(request)
    if not current_user and admin_flags.get("is_super_admin"):
        current_user = {"email": "admin", "display_name": "Admin", "is_admin": True}

    management_home = "/admin/" if current_user and current_user.get("is_admin") else "/account"

    if current_user and current_user.get("sub") and not current_user.get("is_admin"):
        try:
            async with get_session() as session:
                uid = int(current_user["sub"])
                ems = await list_memberships_for_user(session, uid)
                room_memberships = await list_room_memberships_for_user(session, uid)
                booth_memberships = await list_booth_memberships_for_user(session, uid)
            if any(em.role == "event_owner" for em in ems):
                management_home = "/workspace/"
            elif any(rm.role == "room_coordinator" for rm in room_memberships):
                management_home = "/mission-control/"
            elif booth_memberships:
                management_home = "/interpreter"
        except Exception as _exc:
            logging.getLogger(__name__).warning("home() DB error: %s", _exc, exc_info=True)

    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "current_user": current_user,
            "management_home": management_home,
            "js_version": _JS_CACHE_BUST,
        },
    )


@router.get("/local")
@router.get("/local/")
async def voxbento_local(request: Request):
    current_user = await get_current_user(request)
    management_home = "/admin/" if current_user and current_user.get("is_admin") else "/account"
    return templates.TemplateResponse(
        request=request,
        name="local.html",
        context={
            "current_user": current_user,
            "management_home": management_home,
            "js_version": _JS_CACHE_BUST,
            "github_repo_url": "https://github.com/ArnavBallinCode/voxa",
            "github_releases_url": "https://github.com/ArnavBallinCode/voxa/releases",
            "github_latest_release_url": "https://github.com/ArnavBallinCode/voxa/releases/latest",
        },
    )


@router.get("/research")
@router.get("/research/")
async def research_index(request: Request):
    return templates.TemplateResponse(request=request, name="research/index.html", context={})


@router.get("/research/speech-ai-statistics")
async def research_speech_ai_statistics(request: Request):
    return templates.TemplateResponse(request=request, name="research/speech_ai_statistics.html", context={})


@router.get("/research/whisper-benchmark")
async def research_whisper_benchmark(request: Request):
    return templates.TemplateResponse(request=request, name="research/whisper_benchmark.html", context={})


@router.get("/research/stt-comparison")
async def research_stt_comparison(request: Request):
    return templates.TemplateResponse(request=request, name="research/stt_comparison.html", context={})


@router.get("/research/real-time-audio-latency")
async def research_real_time_audio_latency(request: Request):
    return templates.TemplateResponse(request=request, name="research/real_time_audio_latency.html", context={})


@router.get("/research/webrtc-vs-rtmp-vs-hls")
async def research_webrtc_vs_rtmp_vs_hls(request: Request):
    return templates.TemplateResponse(request=request, name="research/webrtc_vs_rtmp_vs_hls.html", context={})


@router.get("/research/speech-ai-glossary")
async def research_speech_ai_glossary(request: Request):
    return templates.TemplateResponse(request=request, name="research/speech_ai_glossary.html", context={})


@router.get("/research/streaming-stt-architecture")
async def research_streaming_stt_architecture(request: Request):
    return templates.TemplateResponse(request=request, name="research/streaming_stt_architecture.html", context={})


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


class DemoRequestModel(BaseModel):
    email: str
    firstName: str | None = None
    lastName: str | None = None
    phoneNumber: str | None = None
    companyName: str | None = None
    country: str | None = None
    industry: str | None = None
    companySize: str | None = None
    languageSupport: str | None = None


@router.post("/api/request-demo")
async def request_demo(data: DemoRequestModel):
    try:
        await send_demo_request_email(data.model_dump())
        return {"ok": True}
    except Exception as e:
        logging.getLogger(__name__).error(f"Error processing demo request: {e}", exc_info=True)
        return {"ok": False, "detail": "An internal error occurred while processing the request."}
