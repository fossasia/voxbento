from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from starlette import status
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from portal.auth import get_admin_flags, get_current_user
from portal.utils import safe_redirect

WORKSPACE_PREFIX = "/workspace"
ADMIN_PREFIX = "/admin"
_EVENT_PATH_RE = re.compile(r"^/(?:api/)?admin(?:/api)?/events/(?P<event_id>\d+)(?:/|$)")
_WORKSPACE_ROUTE_PATTERNS = (
    re.compile(r"/workspace/?"),
    re.compile(r"/workspace/events/?"),
    re.compile(r"/workspace/setup/?"),
    re.compile(r"/workspace/events/\d+/?"),
    re.compile(r"/workspace/events/\d+/(?:setup/(?:rooms|booths|invite)|regenerate_join_code|api-settings|delete)/?"),
    re.compile(r"/workspace/events/\d+/(?:rooms|members)/?"),
    re.compile(r"/workspace/events/\d+/members/\d+/(?:invite|delete)/?"),
    re.compile(r"/workspace/events/\d+/rooms/\d+(?:/(?:transcripts|edit|delete))?/?"),
    re.compile(r"/workspace/events/\d+/rooms/\d+/members(?:/\d+/(?:invite|delete))?/?"),
    re.compile(
        r"/workspace/events/\d+/rooms/\d+/program-ingest/(?:mode|credential|credential/revoke|sync-offset|status)/?"
    ),
    re.compile(r"/workspace/events/\d+/rooms/\d+/booths/?"),
    re.compile(
        r"/workspace/events/\d+/rooms/\d+/booths/\d+"
        r"(?:/(?:edit|delete|translation-settings|transcription-settings))?/?"
    ),
    re.compile(
        r"/workspace/events/\d+/rooms/\d+/booths/\d+/members"
        r"(?:/\d+/(?:invite|delete))?/?"
    ),
    re.compile(r"/workspace/events/\d+/rooms/\d+/booths/\d+/tokens(?:/[0-9a-f]{64}/revoke)?/?"),
    re.compile(r"/workspace/api/events/\d+/api-keys(?:/\d+)?/?"),
    re.compile(r"/workspace/models/(?:trigger_download|download_progress)"),
    re.compile(r"/workspace/models/supertonic/(?:trigger_download|download_progress)"),
    re.compile(r"/api/workspace/events/\d+/rooms/\d+/transcripts/[^/]+"),
    re.compile(r"/api/workspace/providers/translation/models"),
)


def workspace_to_admin_path(path: str) -> str | None:
    """Map an allowlisted organizer workspace path to its shared handler path."""
    if not any(pattern.fullmatch(path) for pattern in _WORKSPACE_ROUTE_PATTERNS):
        return None
    if path.startswith(f"{WORKSPACE_PREFIX}/") or path == WORKSPACE_PREFIX:
        return f"{ADMIN_PREFIX}{path[len(WORKSPACE_PREFIX) :]}"
    if path.startswith("/api/workspace/"):
        return f"/api/admin{path[len('/api/workspace') :]}"
    return None


def legacy_admin_to_workspace_path(path: str) -> str | None:
    """Return the organizer equivalent of a legacy management path."""
    if path == ADMIN_PREFIX or path.startswith(f"{ADMIN_PREFIX}/"):
        workspace_path = f"{WORKSPACE_PREFIX}{path[len(ADMIN_PREFIX) :]}"
    elif path.startswith("/api/admin/"):
        workspace_path = f"/api/workspace{path[len('/api/admin') :]}"
    else:
        return None
    return workspace_path if workspace_to_admin_path(workspace_path) == path else None


async def should_redirect_organizer(request: Request) -> bool:
    """Return whether an organizer should leave the reserved admin namespace."""
    user = await get_current_user(request)
    if user is None or not user.get("sub"):
        return False

    event_match = _EVENT_PATH_RE.match(request.url.path)
    event_id = int(event_match.group("event_id")) if event_match is not None else None
    flags = await get_admin_flags(request, event_id=event_id)
    if flags["is_super_admin"]:
        return False
    return flags["is_event_owner"]


def management_template_context(request: Request) -> dict[str, str | bool]:
    """Expose the externally visible management namespace to Jinja templates."""
    is_workspace = getattr(request.state, "is_workspace", False)
    return {
        "management_prefix": WORKSPACE_PREFIX if is_workspace else ADMIN_PREFIX,
        "management_api_prefix": "/api/workspace" if is_workspace else "/api/admin",
        "is_workspace": is_workspace,
    }


def management_url(request: Request, path: str) -> str:
    """Build a management URL in the namespace used by the current request."""
    prefix = WORKSPACE_PREFIX if getattr(request.state, "is_workspace", False) else ADMIN_PREFIX
    return f"{prefix}/{path.lstrip('/')}"


def workspace_location(location: str) -> str:
    """Keep framework-generated redirects in the public workspace namespace."""
    parsed = urlsplit(location)
    mapped_path = legacy_admin_to_workspace_path(parsed.path)
    if mapped_path is None:
        return location
    return urlunsplit((parsed.scheme, parsed.netloc, mapped_path, parsed.query, parsed.fragment))


class WorkspaceRoutingMiddleware:
    """Route workspace URLs through the existing management handlers."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        original_path = scope["path"]
        legacy_workspace_path = legacy_admin_to_workspace_path(original_path)
        if legacy_workspace_path is not None:
            request = Request(scope)
            if await should_redirect_organizer(request):
                query = request.url.query
                target = f"{legacy_workspace_path}?{query}" if query else legacy_workspace_path
                response = safe_redirect(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
                await response(scope, receive, send)
                return

        mapped_path = workspace_to_admin_path(original_path)
        if mapped_path is None:
            await self.app(scope, receive, send)
            return

        workspace_scope = dict(scope)
        workspace_scope["state"] = dict(scope.get("state", {}))
        workspace_scope["state"]["is_workspace"] = True
        workspace_scope["path"] = mapped_path
        workspace_scope["raw_path"] = mapped_path.encode("utf-8")

        async def send_in_workspace(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                location = headers.get("location")
                if location:
                    headers["location"] = workspace_location(location)
            await send(message)

        await self.app(workspace_scope, receive, send_in_workspace)
