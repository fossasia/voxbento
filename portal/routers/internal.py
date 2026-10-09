"""Internal service-to-service endpoints. Never exposed through the public proxy."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from portal.config import settings
from portal.program_ingest.publish_auth import MediaMTXAuthRequest, authorize_publish
from portal.program_ingest.supervisor import supervisor

router = APIRouter(include_in_schema=False)

_UNAUTHORIZED = {"detail": "Unauthorized"}


def hook_caller_allowed(key: str) -> bool:
    """Only MediaMTX, which holds ``MEDIAMTX_AUTH_HOOK_SECRET``, may call the hook.

    Startup refuses to run without the secret outside debug mode
    (``Settings.validate_production_secrets``); debug mode accepts any caller.
    """
    expected = settings.mediamtx_auth_hook_secret
    if expected:
        return hmac.compare_digest(expected.encode("utf-8"), key.encode("utf-8"))
    return settings.debug


@router.post("/internal/mediamtx/auth")
async def mediamtx_auth_hook(body: MediaMTXAuthRequest, key: str = Query("")) -> JSONResponse:
    """MediaMTX ``authMethod: http`` callback.

    Any 2xx allows the action. Every denial is the same bare 401 so callers
    cannot tell a missing room from a wrong secret.
    """
    if not hook_caller_allowed(key):
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    decision = await authorize_publish(body, can_accept_new_feed=supervisor.can_accept_new_feed)
    if not decision.allowed:
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    return JSONResponse({"ok": True})
