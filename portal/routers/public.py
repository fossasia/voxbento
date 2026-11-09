from __future__ import annotations

import logging
import re
from html import escape
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from portal.auth import get_access_flags, get_current_user, resolve_request_principal
from portal.config import settings
from portal.database import (
    get_session,
    get_user_by_id,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
)
from portal.globals import _JS_CACHE_BUST, asset_version_context
from portal.notifications import queue_notification
from portal.rate_limit import check_rate_limit
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.utils import _check_mediamtx, get_client_ip

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"), context_processors=[asset_version_context])

router = APIRouter()


@router.get("/")
async def home(request: Request):
    current_user = await get_current_user(request)
    admin_flags = await get_access_flags(request)
    if not current_user and admin_flags.get("is_admin"):
        current_user = {"email": "admin", "display_name": "Admin", "is_admin": True}

    management_home = "/admin/" if current_user and current_user.get("is_admin") else "/account"

    if current_user and current_user.get("sub") and not current_user.get("is_admin"):
        try:
            async with get_session() as session:
                uid = int(current_user["sub"])
                db_user = await get_user_by_id(session, uid)
                ems = await list_memberships_for_user(session, uid)
                room_memberships = await list_room_memberships_for_user(session, uid)
                booth_memberships = await list_booth_memberships_for_user(session, uid)
            if (db_user and db_user.can_host) or any(em.role == EVENT_OWNER for em in ems):
                management_home = "/workspace/"
            elif any(rm.role == ROOM_COORDINATOR for rm in room_memberships):
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
async def sitemap(request: Request) -> Response:
    """Return the public sitemap using the configured canonical base URL."""
    base_url = escape(settings.public_base_url.rstrip("/"), quote=False)
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}/research</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>{base_url}/research/speech-ai-statistics</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/whisper-benchmark</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/stt-comparison</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/real-time-audio-latency</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/webrtc-vs-rtmp-vs-hls</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/speech-ai-glossary</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base_url}/research/streaming-stt-architecture</loc>
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
    notes: str | None = None


@router.post("/api/request-demo")
async def request_demo(request: Request, data: DemoRequestModel):
    client_ip = get_client_ip(request)

    # 1. Strip potential header injection characters (\r, \n)
    def clean(s: str | None) -> str:
        if not s:
            return ""
        return re.sub(r"[\r\n]+", " ", s).strip()

    email = clean(data.email).lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "detail": "Invalid email address."}, status_code=status.HTTP_400_BAD_REQUEST)

    # 2. Rate limits: 20/hour per IP, 3/hour per email
    if not check_rate_limit("demo_request_ip", client_ip, max_requests=20, window_seconds=3600):
        return JSONResponse({"ok": False, "detail": "Too many requests. Please try again later."}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    if not check_rate_limit("demo_request_email", email, max_requests=3, window_seconds=3600):
        return JSONResponse({"ok": False, "detail": "Too many requests for this email. Please try again later."}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    # 3. Server-side user identification (ignore client-provided user_id/source)
    user_id = None
    is_authenticated = False
    async with get_session() as session:
        principal = await resolve_request_principal(request, session)
        user = principal["user"]
        # Hosting access is requested from the Account page (#768); a demo
        # request only links the email to the signed-in account.
        if user is not None:
            user_id = user.id
            is_authenticated = True

    cleaned_data = {
        "email": email,
        "firstName": clean(data.firstName),
        "lastName": clean(data.lastName),
        "phoneNumber": clean(data.phoneNumber),
        "companyName": clean(data.companyName),
        "country": clean(data.country),
        "industry": clean(data.industry),
        "companySize": clean(data.companySize),
        "languageSupport": clean(data.languageSupport),
        "notes": clean(data.notes),
        "user_id": user_id,
        "source": "account" if is_authenticated else "home",
    }

    async with get_session() as session:
        await queue_notification(
            session,
            "demo_request",
            settings.hosting_requests_email,
            {
                "firstName": cleaned_data["firstName"],
                "lastName": cleaned_data["lastName"],
                "email": cleaned_data["email"],
                "companyName": cleaned_data["companyName"],
                "phoneNumber": cleaned_data["phoneNumber"],
                "country": cleaned_data["country"],
                "industry": cleaned_data["industry"],
                "companySize": cleaned_data["companySize"],
                "languageSupport": cleaned_data["languageSupport"],
                "notes": cleaned_data["notes"],
                "admin_url": f"{settings.public_base_url.rstrip('/')}/admin/users/{user_id}/" if user_id else f"{settings.public_base_url.rstrip('/')}/admin/users/",
            },
        )
    return {"ok": True}
