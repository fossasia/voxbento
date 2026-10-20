from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from portal import meeting_source, program_ingest
from portal.auth import require_admin
from portal.config import settings
from portal.exceptions import RedirectRequired
from portal.globals import asset_version_context
from portal.routers.admin import router as admin_router
from portal.routers.api import router as api_router
from portal.routers.api_v1 import router as api_v1_router
from portal.routers.auth import router as auth_router
from portal.routers.demo import router as demo_router
from portal.routers.developer import router as developer_router
from portal.routers.interpreter import router as interpreter_router
from portal.routers.listener import router as listener_router
from portal.routers.meeting_source import router as meeting_source_router
from portal.routers.oauth import router as oauth_router
from portal.routers.program_ingest import router as program_ingest_router
from portal.routers.public import router as public_router
from portal.routers.webhooks import router as webhooks_router
from portal.websockets.handlers import router as ws_router
from portal.workspace_routing import WorkspaceRoutingMiddleware

"FastAPI entry point — sole backend for the Voxbento.\n\nStart with:\n    uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --reload\n"

_BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "portal" / "templates"), context_processors=[asset_version_context])


@asynccontextmanager
async def lifespan(app: FastAPI):
    import httpx

    import portal.globals as pg
    from portal.tts import demo_gen as dg
    from portal.tts.demo_gen import ensure_demo_generated

    settings.validate_production_secrets()

    pg.shared_http_client = httpx.AsyncClient(timeout=10.0)

    # Generate landing page demo audio in the background on first startup.
    # Uses local Supertonic — no external API key needed.
    async with dg._generation_lock:
        dg._generating = True

    async def _gen():
        try:
            await ensure_demo_generated()
        finally:
            dg._generating = False

    import asyncio

    import portal.webhooks.worker as _webhook_worker_mod

    # Reference via module attribute so test-time monkey-patching of
    # portal.webhooks.worker.webhook_worker_loop is respected.
    webhook_task = asyncio.create_task(_webhook_worker_mod.webhook_worker_loop())
    ingest_task = asyncio.create_task(program_ingest.reconcile_loop()) if settings.program_ingest_enabled else None
    meeting_task = asyncio.create_task(meeting_source.reconcile_loop())

    dg.track_task(asyncio.create_task(_gen()))

    logging.getLogger("uvicorn.access").addFilter(_UvicornTokenRedactor())
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())
    yield
    import contextlib

    webhook_task.cancel()
    meeting_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await meeting_task
    if ingest_task:
        ingest_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest_task
    with contextlib.suppress(asyncio.CancelledError):
        await webhook_task

    if pg.shared_http_client:
        await pg.shared_http_client.aclose()


class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        try:
            return record.getMessage().find("GET /healthz") == -1
        except Exception:
            return True


class _UvicornTokenRedactor(logging.Filter):
    import re as _re

    _TOKEN_RE = _re.compile(r"(?i)((?:^|&|\?)(?:token|client_secret|code|access_token|refresh_token|key)=)[^&\s]*")

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True

        lowered = message.casefold()
        if self._TOKEN_RE.search(message) and any(
            x in lowered for x in ["/api/", "/embed/", "/ws/", "/oauth/", "/internal/"]
        ):
            if isinstance(record.msg, str):
                record.msg = self._TOKEN_RE.sub(r"\1[REDACTED]", record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._TOKEN_RE.sub(r"\1[REDACTED]", value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


app = FastAPI(title="Voxbento", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(WorkspaceRoutingMiddleware)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request, _=Depends(require_admin)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Voxbento API Docs")


@app.get("/openapi.json", include_in_schema=False)
async def get_open_api_endpoint(request: Request, _=Depends(require_admin)):
    return JSONResponse(get_openapi(title="Voxbento API", version="1.0.0", routes=app.routes))


@app.exception_handler(RedirectRequired)
async def redirect_required_handler(request: Request, exc: RedirectRequired):
    return RedirectResponse(exc.url, status_code=exc.status_code)


# Write-only secrets (e.g. the YouTube relay key) must never be echoed back by a 422.
_REDACTED_INPUT_FIELDS = frozenset({"stream_key"})


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        return any(key in _REDACTED_INPUT_FIELDS or _contains_secret_field(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    """FastAPI's default 422, minus the rejected input wherever it is or contains a secret field.

    A ``missing`` error reports the whole body as its input, so the check covers the value
    as well as the error location.
    """
    errors = []
    for error in exc.errors():
        if any(part in _REDACTED_INPUT_FIELDS for part in error.get("loc", ())) or _contains_secret_field(
            error.get("input")
        ):
            error = {key: value for key, value in error.items() if key != "input"}
        errors.append(error)
    return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    headers = dict(exc.headers) if exc.headers else None
    location = exc.headers.get("Location") if exc.headers else None

    if location and exc.status_code in (301, 302, 303, 307, 308):
        from portal.utils import safe_redirect

        return safe_redirect(url=location, status_code=exc.status_code)

    is_api = "/api/" in request.url.path or request.url.path.startswith("/api")
    if "text/html" in request.headers.get("accept", "") and not is_api:
        detail_msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 400:
            return templates.TemplateResponse(
                request, "400.html", {"request": request, "detail": detail_msg, "status_code": 400}, status_code=400
            )
        if exc.status_code in (401, 403):
            import urllib.parse

            from fastapi import status

            from portal.auth import get_current_user
            from portal.utils import safe_redirect

            if not request.url.path.startswith("/embed/"):
                user = await get_current_user(request)
                if not user:
                    next_url = request.url.path
                    if request.url.query:
                        next_url += f"?{request.url.query}"
                    return safe_redirect(
                        f"/login?next={urllib.parse.quote(next_url)}", status_code=status.HTTP_303_SEE_OTHER
                    )
            return templates.TemplateResponse(
                request, "403.html", {"request": request, "detail": detail_msg}, status_code=403
            )
        if exc.status_code == 404:
            return templates.TemplateResponse(
                request, "404.html", {"request": request, "detail": detail_msg}, status_code=404
            )
        if exc.status_code == 429:
            return templates.TemplateResponse(
                request, "429.html", {"request": request, "detail": detail_msg}, status_code=429
            )
        if exc.status_code >= 500:
            return templates.TemplateResponse(
                request, "500.html", {"request": request, "detail": detail_msg}, status_code=exc.status_code
            )
        return templates.TemplateResponse(
            request, "400.html", {"request": request, "detail": detail_msg, "status_code": exc.status_code}, status_code=exc.status_code
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    import logging
    import traceback

    traceback.print_exc()

    logging.exception("Unhandled Server Error:")
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request, "500.html", {"request": request, "detail": "Internal Server Error"}, status_code=500
        )
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


app.mount("/static", StaticFiles(directory=_BASE_DIR / "portal" / "static"), name="static")

app.include_router(public_router)

app.include_router(auth_router)
app.include_router(developer_router)
app.include_router(oauth_router)
app.include_router(webhooks_router)

app.include_router(interpreter_router)

app.include_router(listener_router)

app.include_router(api_router)
app.include_router(api_v1_router)

app.include_router(admin_router)
app.include_router(program_ingest_router)
app.include_router(meeting_source_router)

app.include_router(demo_router)

app.include_router(ws_router)


def main() -> None:
    import uvicorn

    uvicorn.run("fastapi_app:app", host=settings.host, port=settings.port, reload=settings.debug)


if __name__ == "__main__":
    main()
