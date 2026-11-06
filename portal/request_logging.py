from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable

from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from portal.logging_config import new_request_id, reset_request_id, set_request_id

logger = logging.getLogger("portal.request")

REQUEST_ID_HEADER = "x-request-id"

# Stands in for the route label when no route matched; the concrete path can
# carry a token, so it is never used as a fallback.
UNMATCHED_ROUTE = "<unmatched>"

# The load balancer polls these every few seconds; logging each success buries
# everything else, the same reason uvicorn's access log drops them. A *failing*
# probe is still logged — that is the one worth seeing.
_QUIET_WHEN_HEALTHY = frozenset({"/healthz"})

# An upstream proxy may supply the id; keep only characters that cannot break a log line.
_SAFE_REQUEST_ID = re.compile(r"[^A-Za-z0-9._-]")
_MAX_INBOUND_ID_LEN = 64


def _inbound_request_id(scope: Scope) -> str:
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name == REQUEST_ID_HEADER.encode():
            candidate = _SAFE_REQUEST_ID.sub("", raw_value.decode("latin-1"))
            return candidate[:_MAX_INBOUND_ID_LEN]
    return ""


def _route_template(scope: Scope) -> str:
    """Path template rather than the concrete path, so ids stay out of the log.

    A request that matched no route (a 404, or one rewritten before routing)
    has no template. The concrete path is not a safe substitute — an invite or
    revocation URL carries its token in the path — so it is withheld entirely.
    """
    route = scope.get("route")
    for attribute in ("path_format", "path"):
        value = getattr(route, attribute, None)
        if isinstance(value, str) and value:
            return value
    return UNMATCHED_ROUTE


class RequestLoggingMiddleware:
    """Give every request a correlation id and log its outcome once.

    The id is bound to a context variable, so logs emitted deeper in the stack
    carry it without being passed it explicitly, and is echoed in the response
    header so a report can be tied back to a single trail.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # The id is always minted here. An inbound X-Request-ID is recorded
        # alongside it rather than adopted: this endpoint is public, so trusting
        # the header would let a caller merge its activity into another trace or
        # choose an id that reaches the log unredacted.
        request_id = new_request_id()
        upstream_id = _inbound_request_id(scope)
        token = set_request_id(request_id)
        started = time.perf_counter()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # "headers" is optional in the ASGI spec, and MutableHeaders
                # indexes it directly, so an app that omits it would KeyError.
                message.setdefault("headers", [])
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            elif message["type"] == "websocket.accept":
                # The handshake response carries headers too, so a socket client
                # can read the same correlation id an HTTP caller gets.
                message.setdefault("headers", [])
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        def details(**fields: object) -> dict[str, object]:
            if upstream_id:
                fields["upstream_request_id"] = upstream_id
            return fields

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Log the failure against this id, then let the app's handlers run.
            # Starlette's error middleware sits outside this one and will turn
            # this into the 500 the caller sees, so the status is recorded here.
            logger.exception(
                "request failed",
                extra=details(
                    event="request.failed",
                    method=scope.get("method", scope["type"]),
                    route=_route_template(scope),
                    status=500,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                ),
            )
            raise
        else:
            if scope.get("path") in _QUIET_WHEN_HEALTHY and status_code < 400:
                return
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if scope["type"] == "websocket":
                logger.info(
                    "socket closed",
                    extra=details(
                        event="socket.closed",
                        route=_route_template(scope),
                        duration_ms=duration_ms,
                    ),
                )
            else:
                logger.log(
                    logging.WARNING if status_code >= 500 else logging.INFO,
                    "request completed",
                    extra=details(
                        event="request.completed",
                        method=scope.get("method", ""),
                        route=_route_template(scope),
                        status=status_code,
                        duration_ms=duration_ms,
                    ),
                )
        finally:
            reset_request_id(token)
