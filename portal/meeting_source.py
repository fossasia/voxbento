"""Managed Jitsi meeting floor agent: desired state, lifecycle and reconciliation.

The floor-bot service owns the browser, PulseAudio and FFmpeg processes; this module
only coordinates them. ``Room.meeting_agent_enabled`` is the durable desired state,
the bot's /status is the observed agent state, and MediaMTX reports whether audio
reached the canonical floor path. Meeting URLs can embed meeting credentials, so
they are never logged or returned in status responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import urllib.parse
from dataclasses import dataclass
from time import monotonic

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import get_session
from portal.globals import get_http_client
from portal.models import Event, Room
from portal.program_ingest import MediaPath, configure_source, media_paths, pipeline_problem, room_lock
from portal.transcription.process import audio_progress
from portal.transcription.providers.base import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.websockets.manager import broadcast_transcription

logger = logging.getLogger(__name__)
AGENT_CONTROL_TIMEOUT = 10.0
RECONCILE_INTERVAL_SECS = 2.0
# With no room wanting an agent, still look for orphans (e.g. a takeover whose /stop failed).
IDLE_SWEEP_SECS = 30.0
RESTART_BACKOFF_SECS = (2.0, 5.0, 10.0, 20.0, 30.0)
# Processing this long without a fault ends the incident and refills the restart budget.
STABLE_RESET_SECS = 60.0
MEETING_PATH = re.compile(r"[A-Za-z0-9_.~%-]{1,200}(?:/[A-Za-z0-9_.~%-]{1,200})?")
FAILURES = {
    "missing": "The meeting agent is not running.",
    "exited": "The meeting agent process exited unexpectedly.",
    "join_failed": "The agent could not join the meeting. Check the room URL and that the meeting has no lobby or password.",
    "not_jitsi": "The room URL did not open a Jitsi meeting. Use a meeting URL on the self-hosted Jitsi server.",
    "redirected": "The meeting URL redirects to a different room. Use the URL the meeting finally opens at.",
    "left_meeting": "The agent was removed from the meeting or the meeting ended.",
    "audio_failed": "The agent joined but could not send meeting audio to the media server.",
    "no_audio": "The agent joined but no meeting audio reached the media server.",
}
UNREACHABLE = "The meeting agent service is unreachable. Ask the server operator to check the floor-bot service."
SOURCE_CONFLICT = (
    "Program ingest (an encoder or the browser sender) owns this room's floor. Confirm the source "
    "change to disconnect it and connect the meeting."
)


@dataclass
class AgentHealth:
    state: str = "starting"
    reason: str = "Launching the meeting agent."
    restarts: int = 0
    failure: str = "exited"
    # URL the running agent joined; None after a portal restart until the agent is adopted.
    agent_url: str | None = None
    next_restart_at: float | None = None
    joined_at: float | None = None
    last_bytes: int = -1
    last_progress: float | None = None
    healthy_since: float | None = None
    worker_config: tuple | None = None
    worker_started_at: float | None = None


health: dict[int, AgentHealth] = {}
_last_sweep = 0.0  # monotonic time of the last stale-agent sweep; 0 forces one
_unreachable = False


def process_key(event_slug: str, room_id: int) -> str:
    return f"{event_slug}-{room_id}"


def _routes_to_internal_jitsi(parsed: urllib.parse.SplitResult) -> bool:
    """The bundled Jitsi is reached over the private network, whatever scheme was typed."""
    public = urllib.parse.urlsplit(settings.effective_jitsi_base_url)
    return parsed.netloc in ("jitsi.voxbento.com", public.netloc) or parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def validate_meeting_url(url: str) -> str:
    """Return a normalized meeting URL or raise ValueError with an actionable message."""
    url = url.strip()
    if not url:
        raise ValueError("Enter the Jitsi meeting URL.")
    if len(url) > 500:
        raise ValueError("The meeting URL is too long.")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Enter a full meeting URL that starts with https://.")
    if parsed.scheme == "http" and not _routes_to_internal_jitsi(parsed):
        # The agent would send the room URL, including any Jitsi jwt, in clear text.
        raise ValueError("Use an https:// meeting URL; plain http would expose meeting credentials.")
    if parsed.username or parsed.password:
        raise ValueError("Remove the username and password from the meeting URL.")
    if parsed.fragment:
        raise ValueError("Remove the '#' part from the meeting URL.")
    if not MEETING_PATH.fullmatch(parsed.path.strip("/")):
        raise ValueError(
            "The meeting URL must end with the Jitsi room name, for example https://meet.example.org/TrackA."
        )
    return url


def url_fingerprint(url: str) -> str:
    """Identify a meeting URL without revealing it; the floor bot reports the same value."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def joined_other_url(agent: dict | None, known_url: str | None, target_url: str) -> bool:
    """Whether the agent joined a meeting other than target_url.

    The bot's fingerprint of the URL it actually joined is authoritative; images
    that predate it fall back to the URL this portal last started the agent with.
    """
    fingerprint = agent.get("url_fingerprint") if agent else None
    if fingerprint:
        return fingerprint != url_fingerprint(target_url)
    return known_url is not None and known_url != target_url


def agent_url(room: Room, event: Event) -> str:
    """Resolve the URL the agent joins, routing the bundled Jitsi through the private network."""
    if not room.jitsi_url:
        clean_name = re.sub("[^a-zA-Z0-9]+", "", room.display_name)
        return f"{settings.effective_jitsi_internal_base}/Voxbento-{event.slug}-{clean_name}"
    parsed = urllib.parse.urlsplit(room.jitsi_url)
    if _routes_to_internal_jitsi(parsed):
        internal = urllib.parse.urlsplit(settings.effective_jitsi_internal_base)
        return urllib.parse.urlunsplit(parsed._replace(scheme=internal.scheme, netloc=internal.netloc))
    return room.jitsi_url


async def agent_snapshot() -> dict[str, dict]:
    response = await get_http_client().get(f"{settings.floor_bot_base}/status", timeout=5.0)
    response.raise_for_status()
    body = response.json()
    agents = body.get("active_rooms") if isinstance(body, dict) else None
    # Every caller reads entries with .get(); reject other shapes as ValueError, which
    # callers treat as an unavailable service, instead of crashing the reconcile task.
    if not isinstance(agents, dict) or not all(
        isinstance(key, str) and isinstance(info, dict) for key, info in agents.items()
    ):
        raise ValueError("Invalid floor-bot status")
    return agents


async def bot_stage(event_slug: str, room_id: int) -> tuple[bool, str, bool]:
    """Raw floor-bot view for the legacy API: (service reachable, bot stage, process running)."""
    try:
        info = (await agent_snapshot()).get(process_key(event_slug, room_id))
    except (httpx.HTTPError, ValueError):
        return False, "unknown", False
    if not info:
        return True, "stopped", False
    return True, str(info.get("stage", info.get("state", "unknown"))), info.get("state") == "healthy"


async def start_agent(room: Room, event: Event) -> None:
    # The bot replaces any process it holds for this room, so a start never adds a second participant.
    response = await get_http_client().post(
        f"{settings.floor_bot_base}/start",
        json={
            "event_slug": event.slug,
            "room_id": room.id,
            "jitsi_url": agent_url(room, event),
            "mediamtx_rtsp_base": settings.mediamtx_rtsp_base,
        },
        timeout=AGENT_CONTROL_TIMEOUT,
    )
    response.raise_for_status()


async def stop_agent(event_slug: str, room_id: int) -> None:
    response = await get_http_client().post(
        f"{settings.floor_bot_base}/stop",
        json={"event_slug": event_slug, "room_id": room_id},
        timeout=AGENT_CONTROL_TIMEOUT,
    )
    response.raise_for_status()


async def connect(room: Room, event: Event, meeting_url: str | None, confirm_source_change: bool) -> None:
    """Make the meeting agent the floor source. Caller holds the room lock and DB transaction."""
    current = health.get(room.id)
    known_url = current.agent_url if current and current.agent_url else agent_url(room, event)
    try:
        if meeting_url is not None:
            room.jitsi_url = validate_meeting_url(meeting_url)
        elif room.jitsi_url:
            validate_meeting_url(room.jitsi_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if room.floor_source != "jitsi_bot" and not confirm_source_change:
        raise HTTPException(409, {"code": "source_conflict", "message": SOURCE_CONFLICT})
    try:
        agent = (await agent_snapshot()).get(process_key(event.slug, room.id))
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, UNREACHABLE) from None
    # Start the agent before releasing a program source: a failed start must leave the
    # encoder or browser sender connected. Until floor_source changes, the publish hook
    # rejects the agent's RTSP stream, so the two sources never publish at once.
    started = False
    # The bot reads its URL only at launch, so a changed URL needs a fresh agent.
    target_url = agent_url(room, event)
    if joined_other_url(agent, known_url, target_url) or not agent or agent.get("state") != "healthy":
        try:
            await start_agent(room, event)
        except httpx.HTTPError:
            raise HTTPException(502, UNREACHABLE) from None
        started = True
    if room.floor_source != "jitsi_bot":
        try:
            await configure_source(room, event, "disable", room.program_sync_offset_ms)
        except httpx.HTTPError:
            if started:
                await _abandon_agent(event.slug, room.id)
            raise HTTPException(
                502, "Could not switch the floor source; the meeting was not connected. Retry."
            ) from None
    room.meeting_agent_enabled = True
    health[room.id] = AgentHealth(agent_url=target_url)
    logger.info("meeting_agent action=connect room_id=%s", room.id)


async def _abandon_agent(event_slug: str, room_id: int) -> None:
    """Stop an agent whose connect failed; the reconcile sweep retries if this stop fails."""
    global _last_sweep
    try:
        await stop_agent(event_slug, room_id)
    except httpx.HTTPError:
        _last_sweep = 0.0
        logger.warning("meeting_agent stop_pending room_id=%s", room_id)


async def disconnect(room: Room, event: Event) -> bool:
    """Stop the meeting agent. Caller holds the room lock and DB transaction.

    Returns False when the agent could not be confirmed stopped; the reconcile loop
    retries that cleanup because the desired state is already cleared.
    """
    global _last_sweep
    room.meeting_agent_enabled = False
    health.pop(room.id, None)
    # A program encoder may own the floor worker; only stop the worker the agent fed.
    if room.floor_source == "jitsi_bot":
        await stop_transcription_worker(make_booth_id(event.slug, room.id, "floor"))
    try:
        await stop_agent(event.slug, room.id)
    except httpx.HTTPError:
        _last_sweep = 0.0
        logger.warning("meeting_agent stop_pending room_id=%s", room.id)
        return False
    logger.info("meeting_agent action=disconnect room_id=%s", room.id)
    return True


def status(room: Room) -> dict:
    """Public status. Never include the meeting URL or media addresses."""
    enabled = room.meeting_agent_enabled and room.floor_source == "jitsi_bot"
    current = health.get(room.id) if enabled else None
    if enabled:
        state = current.state if current else "starting"
        reason = current.reason if current else "Launching the meeting agent."
    else:
        state = "stopped"
        reason = (
            "Program ingest (an encoder or the browser sender) owns the floor."
            if room.floor_source != "jitsi_bot"
            else ""
        )
    return {
        "enabled": enabled,
        "state": state,
        "reason": reason,
        "floor_source": room.floor_source,
        "restarts": current.restarts if current else 0,
        "max_restarts": settings.meeting_agent_max_restarts,
    }


async def _stop_worker(current: AgentHealth, booth_id: str) -> None:
    await stop_transcription_worker(booth_id)
    current.worker_config = None
    current.worker_started_at = None
    current.healthy_since = None


async def _recover(room: Room, event: Event, current: AgentHealth, now: float, failure: str) -> None:
    """Restart a missing or exited agent within the bounded policy."""
    current.joined_at = None
    current.last_progress = None
    current.last_bytes = -1
    current.failure = failure
    message = FAILURES.get(failure, FAILURES["exited"])
    limit = settings.meeting_agent_max_restarts
    if current.restarts >= limit:
        current.state = "degraded"
        current.reason = (
            f"{message} Automatic reconnects stopped after {limit} attempts; fix the cause and connect again."
        )
        return
    if current.next_restart_at is None:
        current.next_restart_at = now + RESTART_BACKOFF_SECS[min(current.restarts, len(RESTART_BACKOFF_SECS) - 1)]
    current.state = "reconnecting"
    if now < current.next_restart_at:
        current.reason = f"{message} Reconnecting shortly (attempt {current.restarts + 1} of {limit})."
        return
    current.restarts += 1
    current.next_restart_at = None
    await start_agent(room, event)
    current.agent_url = agent_url(room, event)
    current.reason = f"{message} Restarting the meeting agent (attempt {current.restarts} of {limit})."
    logger.info("meeting_agent restart room_id=%s attempt=%s failure=%s", room.id, current.restarts, failure)


async def reconcile_room(
    room: Room, event: Event, agent: dict | None, path: MediaPath | None, media_known: bool = True
) -> None:
    current = health.setdefault(room.id, AgentHealth())
    booth_id = make_booth_id(event.slug, room.id, "floor")
    now = monotonic()
    target_url = agent_url(room, event)
    if joined_other_url(agent, current.agent_url, target_url):
        # The owner changed the meeting URL. Rejoin with a fresh budget even if the old
        # agent failed: retries spent on the previous URL say nothing about the new one.
        await _stop_worker(current, booth_id)
        await start_agent(room, event)
        current.agent_url = target_url
        current.restarts = 0
        current.next_restart_at = None
        current.joined_at = None
        current.last_progress = None
        current.last_bytes = -1
        current.state, current.reason = "starting", "The meeting URL changed; joining the new meeting."
        logger.info("meeting_agent url_changed room_id=%s", room.id)
        return
    alive = bool(agent and agent.get("state") == "healthy")
    # A scheduled restart proceeds even if the old agent survived a failed stop:
    # start_agent replaces the room's process, so recovery stays bounded.
    if not alive or current.next_restart_at is not None:
        await _stop_worker(current, booth_id)
        if alive:
            failure = current.failure
        else:
            failure = "missing" if not agent else str(agent.get("failure") or "exited")
        await _recover(room, event, current, now, failure)
        return
    # Adopted after a portal restart: the fingerprint check above confirmed the URL when available.
    current.agent_url = target_url
    stage = agent.get("stage")
    if stage in {"launching", "joining"}:
        current.state = "starting"
        current.reason = "Launching the meeting agent." if stage == "launching" else "Joining the meeting."
        return
    if stage != "in_meeting":
        await _stop_worker(current, booth_id)
        current.state, current.reason = "reconnecting", "The meeting agent is leaving the meeting."
        return
    if current.joined_at is None:
        current.joined_at = now
    if not media_known:
        current.state, current.reason = "degraded", "Media control unavailable; retrying automatically."
        return
    if path and path.ready and path.source and path.bytesReceived != current.last_bytes:
        current.last_bytes = path.bytesReceived
        current.last_progress = now
    if now - (current.last_progress or current.joined_at) >= settings.meeting_agent_no_audio_secs:
        # Joined but the FFmpeg leg is not delivering media: restart the whole agent.
        await _stop_worker(current, booth_id)
        try:
            await stop_agent(event.slug, room.id)
        except httpx.HTTPError:
            logger.warning("meeting_agent stop_failed room_id=%s", room.id)
        await _recover(room, event, current, now, "no_audio")
        return
    if current.last_progress is None:
        current.state, current.reason = "joined", "In the meeting; waiting for audio to reach VoxBento."
        return
    problem = pipeline_problem(room, event)
    if problem:
        await _stop_worker(current, booth_id)
        current.state, current.reason = "degraded", problem
        return
    current.state, current.reason = "receiving", "Meeting audio received; starting transcription."
    config = (room.floor_transcription_provider, room.floor_transcription_model, room.floor_language_code)
    if current.worker_config and current.worker_config != config:
        await _stop_worker(current, booth_id)
    await start_transcription_worker(
        event_slug=event.slug,
        language_code="floor",
        booth_id=booth_id,
        broadcast_callback=broadcast_transcription,
        provider=room.floor_transcription_provider,
        model_size=room.floor_transcription_model,
        config=ProviderConfig(api_key=get_api_key(event, ProviderEnum(room.floor_transcription_provider))),
        transcription_language=room.floor_language_code,
        room_id=room.id,
    )
    current.worker_config = config
    if current.worker_started_at is None:
        current.worker_started_at = now
    last_audio = audio_progress.get(booth_id)
    if now - (last_audio or current.worker_started_at) > settings.program_ingest_stall_secs:
        await _stop_worker(current, booth_id)
        current.state, current.reason = "degraded", "Audio decoder stalled or failed; restarting the processing worker."
    elif last_audio:
        current.state, current.reason = "processing", "Meeting audio is being transcribed."
        if current.healthy_since is None:
            current.healthy_since = now
        elif now - current.healthy_since >= STABLE_RESET_SECS:
            current.restarts = 0


async def _stop_stale_agents(agents: dict[str, dict]) -> None:
    """Stop agents the database no longer wants, e.g. after a portal restart or failed disconnect."""
    for key, info in agents.items():
        event_slug, room_id = info.get("event_slug"), info.get("room_id")
        if not event_slug or room_id is None:
            event_slug, _, raw_room_id = key.rpartition("-")
            if not event_slug or not raw_room_id.isdigit():
                continue
            room_id = raw_room_id
        room_id = int(room_id)
        # Re-check under the room lock: a connect may be committing right now.
        async with room_lock(room_id), get_session() as session:
            row = (await session.execute(select(Room, Event).join(Event).where(Room.id == room_id))).first()
            if (
                row
                and row[1].slug == event_slug
                and row[0].meeting_agent_enabled
                and row[0].floor_source == "jitsi_bot"
            ):
                continue
            await stop_agent(event_slug, room_id)
            logger.info("meeting_agent stale_agent_stopped room_id=%s", room_id)


async def reconcile_once() -> None:
    global _last_sweep, _unreachable
    async with get_session() as session:
        rows = (await session.execute(select(Room.id, Event.slug).join(Event).where(Room.meeting_agent_enabled))).all()
    ids = [room_id for room_id, _ in rows]
    for room_id in set(health) - set(ids):
        health.pop(room_id, None)
    if not rows and monotonic() - _last_sweep < IDLE_SWEEP_SECS:
        return
    try:
        agents = await agent_snapshot()
    except (httpx.HTTPError, ValueError):
        for room_id in ids:
            current = health.setdefault(room_id, AgentHealth())
            current.state, current.reason = "degraded", f"{UNREACHABLE} Retrying automatically."
        if not _unreachable:
            logger.warning("meeting_agent service_unreachable")
        _unreachable = True
        return
    _unreachable = False
    wanted = {process_key(slug, room_id) for room_id, slug in rows}
    try:
        await _stop_stale_agents({key: info for key, info in agents.items() if key not in wanted})
        _last_sweep = monotonic()
    except (httpx.HTTPError, ValueError):
        logger.warning("meeting_agent stale_cleanup_pending")
    paths: dict[str, MediaPath] = {}
    media_known = True
    if rows:
        try:
            paths = await media_paths()
        except (httpx.HTTPError, ValueError):
            media_known = False
    for room_id in ids:
        try:
            async with room_lock(room_id), get_session() as session:
                row = (await session.execute(select(Room, Event).join(Event).where(Room.id == room_id))).first()
                if not row or not row[0].meeting_agent_enabled:
                    continue
                room, event = row
                if room.floor_source != "jitsi_bot":
                    # Another source took the floor; the agent no longer has a claim to it.
                    room.meeting_agent_enabled = False
                    health.pop(room_id, None)
                    continue
                await reconcile_room(
                    room,
                    event,
                    agents.get(process_key(event.slug, room.id)),
                    paths.get(make_mediamtx_path(event.slug, room.id, "floor")),
                    media_known,
                )
        except (httpx.HTTPError, ValueError, SQLAlchemyError):
            current = health.setdefault(room_id, AgentHealth())
            current.state, current.reason = (
                "degraded",
                "Agent control or processing unavailable; retrying automatically.",
            )
            # Do not log exception bodies: upstream errors may echo the meeting URL.
            logger.warning("meeting_agent reconcile_failed room_id=%s", room_id)


async def reconcile_loop() -> None:
    while True:
        try:
            await reconcile_once()
        except SQLAlchemyError:
            logger.warning("meeting_agent database_unavailable")
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any single bad tick
            # Log the type only: exception text from upstream calls may echo the meeting URL.
            logger.error("meeting_agent reconcile_error type=%s", type(exc).__name__)
        await asyncio.sleep(RECONCILE_INTERVAL_SECS)
