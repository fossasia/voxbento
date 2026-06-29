from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from portal.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/demo/manifest")
async def demo_manifest() -> JSONResponse:
    """Return the pre-baked demo manifest.

    ``status`` is ``"ready"`` when audio files exist, ``"generating"`` when the
    background task is still running, or ``"pending"`` before first generation.
    The frontend polls until status xis ``"ready"``.
    """
    from portal.tts.demo_gen import DEMO_VIDEO_URL, load_manifest

    manifest = load_manifest()
    if manifest is not None:
        return JSONResponse({**manifest, "status": "ready"})

    # Not generated yet — check if a generation task is running.
    from portal.tts import demo_gen as dg

    generating = getattr(dg, "_generating", False)
    return JSONResponse({
        "status": "generating" if generating else "pending",
        "video_url": DEMO_VIDEO_URL,
        "languages": [],
    })


@router.post("/admin/demo/regenerate", dependencies=[Depends(require_admin)])
async def regenerate_demo() -> JSONResponse:
    """Re-generate the demo audio assets (admin only). Runs in the background."""
    import asyncio

    from portal.tts import demo_gen as dg
    from portal.tts.demo_gen import MANIFEST_PATH, generate_demo_assets

    if getattr(dg, "_generating", False):
        return JSONResponse({"ok": False, "detail": "Already generating"})

    MANIFEST_PATH.unlink(missing_ok=True)

    async def _run() -> None:
        dg._generating = True
        try:
            await generate_demo_assets()
        finally:
            dg._generating = False

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "detail": "Generation started"})
