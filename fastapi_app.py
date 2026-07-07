from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from portal.auth import require_admin
from portal.config import settings
from portal.routers.admin import router as admin_router
from portal.routers.api import router as api_router
from portal.routers.auth import router as auth_router
from portal.routers.demo import router as demo_router
from portal.routers.interpreter import router as interpreter_router
from portal.routers.listener import router as listener_router
from portal.routers.public import router as public_router
from portal.websockets.handlers import router as ws_router

"FastAPI entry point — sole backend for the Voxbento.\n\nStart with:\n    uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --reload\n"

_BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    import httpx

    import portal.transcription as ts
    from portal.tts import demo_gen as dg
    from portal.tts.demo_gen import ensure_demo_generated

    settings.validate_production_secrets()

    ts.shared_http_client = httpx.AsyncClient(timeout=10.0)

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
    dg.track_task(asyncio.create_task(_gen()))

    yield
    if ts.shared_http_client:
        await ts.shared_http_client.aclose()


app = FastAPI(title="Voxbento", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request, _=Depends(require_admin)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Voxbento API Docs")


@app.get("/openapi.json", include_in_schema=False)
async def get_open_api_endpoint(request: Request, _=Depends(require_admin)):
    return JSONResponse(get_openapi(title="Voxbento API", version="1.0.0", routes=app.routes))


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if "text/html" in request.headers.get("accept", ""):
        if exc.status_code == 403:
            return templates.TemplateResponse(
                request, "403.html", {"request": request, "detail": exc.detail}, status_code=403
            )
        if exc.status_code == 404:
            return templates.TemplateResponse(
                request, "404.html", {"request": request, "detail": exc.detail}, status_code=404
            )
        if exc.status_code == 429:
            return templates.TemplateResponse(
                request, "429.html", {"request": request, "detail": exc.detail}, status_code=429
            )
        if exc.status_code >= 500:
            return templates.TemplateResponse(
                request, "500.html", {"request": request, "detail": exc.detail}, status_code=exc.status_code
            )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    import logging

    logging.exception("Unhandled Server Error:")
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request, "500.html", {"request": request, "detail": "Internal Server Error"}, status_code=500
        )
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")

app.include_router(public_router)

app.include_router(auth_router)

app.include_router(interpreter_router)

app.include_router(listener_router)

app.include_router(api_router)

app.include_router(admin_router)

app.include_router(demo_router)

app.include_router(ws_router)


def main() -> None:
    import uvicorn

    uvicorn.run("fastapi_app:app", host=settings.host, port=settings.port, reload=settings.debug)


if __name__ == "__main__":
    main()
