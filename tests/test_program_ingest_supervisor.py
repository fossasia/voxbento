from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST, Event, Room
from portal.program_ingest.mediamtx import OFFLINE, PathSnapshot, parse_path_snapshot
from portal.program_ingest.supervisor import (
    IngestState,
    ProgramIngestSupervisor,
    SupervisorConfig,
    WorkerOps,
)

AV = PathSnapshot(
    online=True,
    source_type="webRTCSession",
    source_id="sess-1",
    audio_codecs=("Opus",),
    video_codecs=("H264",),
    inbound_bytes=1000,
)


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@dataclass
class FakeWorkers:
    running: set[int] = field(default_factory=set)
    starts: int = 0
    stops: int = 0
    fail_with: str | None = None

    async def start(self, event: Event, room: Room) -> None:
        if self.fail_with:
            raise ValueError(self.fail_with)
        self.starts += 1
        self.running.add(room.id)

    async def stop(self, event_slug: str, room_id: int) -> None:
        self.stops += 1
        self.running.discard(room_id)

    def is_running(self, event_slug: str, room_id: int) -> bool:
        return room_id in self.running

    def ops(self) -> WorkerOps:
        return WorkerOps(start=self.start, stop=self.stop, is_running=self.is_running)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_supervisor(snapshots: dict[str, PathSnapshot | None], workers: FakeWorkers, clock: Clock, **cfg):
    config = SupervisorConfig(
        max_active=cfg.get("max_active", 4),
        disconnect_grace_seconds=cfg.get("grace", 15),
        stall_seconds=cfg.get("stall", 10),
        poll_seconds=1,
    )

    async def fetch() -> dict[str, PathSnapshot] | None:
        if any(v is None for v in snapshots.values()):
            return None
        return {path: snap for path, snap in snapshots.items() if snap is not None and snap.online}

    async def clear(path: str) -> bool:
        return True

    return ProgramIngestSupervisor(
        config=config, fetch_snapshots=fetch, clear_path_config=clear, worker_ops=workers.ops(), clock=clock
    )


async def make_room(slug: str, *, transcription: bool = True, mode: str = FLOOR_SOURCE_PROGRAM_INGEST) -> Room:
    from portal.database import create_event, create_room, get_session

    async with get_session() as s:
        ev = await create_event(s, slug=slug, display_name=slug)
        room = await create_room(s, event_id=ev.id, display_name="Main")
        room.floor_source_mode = mode
        room.floor_transcription_enabled = transcription
        room.floor_transcription_provider = "local"
        room.floor_transcription_model = "tiny"
        room.floor_language_code = "en"
        await s.flush()
        return room


def floor_path(slug: str, room: Room) -> str:
    return f"{slug}/{room.id}/floor"


async def reload(room_id: int) -> Room:
    from portal.database import get_room_by_id, get_session

    async with get_session() as s:
        return await get_room_by_id(s, room_id)


# ── MediaMTX snapshot parsing ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_parse_snapshot_current_api_shape():
    snap = parse_path_snapshot(
        {
            "online": True,
            "available": True,
            "source": {"type": "webRTCSession", "id": "abc"},
            "tracks2": [{"codec": "H264"}, {"codec": "Opus"}, {"codec": "KLV"}],
            "inboundBytes": 42,
        }
    )
    assert snap.online and snap.source_type == "webRTCSession" and snap.source_id == "abc"
    assert snap.audio_codecs == ("Opus",) and snap.video_codecs == ("H264",)
    assert snap.unsupported_codecs == ("KLV",) and snap.inbound_bytes == 42


@pytest.mark.anyio
async def test_parse_snapshot_legacy_api_shape():
    snap = parse_path_snapshot(
        {
            "ready": True,
            "source": {"type": "rtmpConn", "id": "x"},
            "tracks": ["H264", "MPEG-4 Audio"],
            "bytesReceived": 7,
        }
    )
    assert snap.online and snap.audio_codecs == ("MPEG-4 Audio",) and snap.inbound_bytes == 7


@pytest.mark.anyio
async def test_parse_snapshot_offline_and_garbage():
    assert not parse_path_snapshot({"online": False, "available": True, "source": None}).online
    assert not parse_path_snapshot({"ready": True, "source": None}).online
    assert not parse_path_snapshot(["not", "a", "dict"]).online


# ── State machine ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_waiting_until_encoder_connects_and_stale_timestamps_closed():
    from portal.database import get_session
    from portal.models import utc_now

    room = await make_room("ev-wait")
    async with get_session() as s:
        stored = await s.get(Room, room.id)
        stored.program_ingest_last_connected_at = utc_now()

    workers = FakeWorkers()
    sup = make_supervisor({}, workers, Clock())
    await sup.tick()

    status = sup.status_for(room.id)
    assert status.state == IngestState.WAITING
    assert workers.starts == 0
    refreshed = await reload(room.id)
    assert refreshed.program_ingest_last_disconnected_at is not None


@pytest.mark.anyio
async def test_connected_feed_starts_exactly_one_worker_without_floor_bot():
    room = await make_room("ev-av")
    snaps = {floor_path("ev-av", room): AV}
    workers = FakeWorkers()
    sup = make_supervisor(snaps, workers, Clock())

    for _ in range(3):
        await sup.tick()

    status = sup.status_for(room.id)
    assert status.state == IngestState.PROCESSING
    assert status.audio_codecs == ("Opus",) and status.video_codecs == ("H264",)
    assert workers.running == {room.id}
    assert (await reload(room.id)).program_ingest_last_connected_at is not None


@pytest.mark.anyio
async def test_video_only_feed_is_degraded_without_worker():
    room = await make_room("ev-vid")
    snaps = {floor_path("ev-vid", room): PathSnapshot(online=True, video_codecs=("H264",), inbound_bytes=5)}
    workers = FakeWorkers()
    sup = make_supervisor(snaps, workers, Clock())
    await sup.tick()

    status = sup.status_for(room.id)
    assert status.state == IngestState.DEGRADED
    assert "no audio" in status.detail
    assert workers.starts == 0


@pytest.mark.anyio
async def test_undecodable_audio_is_degraded_with_actionable_detail():
    room = await make_room("ev-codec")
    snaps = {floor_path("ev-codec", room): PathSnapshot(online=True, unsupported_codecs=("KLV",), inbound_bytes=5)}
    sup = make_supervisor(snaps, FakeWorkers(), Clock())
    await sup.tick()
    status = sup.status_for(room.id)
    assert status.state == IngestState.DEGRADED
    assert "KLV" in status.detail and "Opus or AAC" in status.detail


@pytest.mark.anyio
async def test_transcription_disabled_reports_receiving_with_warning():
    room = await make_room("ev-off", transcription=False)
    snaps = {floor_path("ev-off", room): AV}
    workers = FakeWorkers()
    sup = make_supervisor(snaps, workers, Clock())
    await sup.tick()
    status = sup.status_for(room.id)
    assert status.state == IngestState.RECEIVING
    assert "disabled" in status.detail
    assert workers.starts == 0


@pytest.mark.anyio
async def test_disconnect_keeps_worker_for_grace_then_stops_it():
    room = await make_room("ev-dc")
    path = floor_path("ev-dc", room)
    snaps: dict[str, PathSnapshot | None] = {path: AV}
    workers = FakeWorkers()
    clock = Clock()
    sup = make_supervisor(snaps, workers, clock, grace=15)
    await sup.tick()

    snaps[path] = OFFLINE
    clock.now += 2
    await sup.tick()
    status = sup.status_for(room.id)
    assert status.state == IngestState.DISCONNECTED
    assert workers.running == {room.id}
    assert (await reload(room.id)).program_ingest_last_disconnected_at is not None

    clock.now += 20
    await sup.tick()
    assert sup.status_for(room.id).state == IngestState.DISCONNECTED
    assert workers.running == set()
    assert not sup.status_for(room.id).worker_running


@pytest.mark.anyio
async def test_reconnect_within_grace_resumes_without_duplicate_worker():
    room = await make_room("ev-rc")
    path = floor_path("ev-rc", room)
    snaps: dict[str, PathSnapshot | None] = {path: AV}
    workers = FakeWorkers()
    clock = Clock()
    sup = make_supervisor(snaps, workers, clock)
    await sup.tick()
    snaps[path] = OFFLINE
    clock.now += 2
    await sup.tick()
    snaps[path] = PathSnapshot(**{**AV.__dict__, "source_id": "sess-2", "inbound_bytes": 2000})
    clock.now += 2
    await sup.tick()

    status = sup.status_for(room.id)
    assert status.state == IngestState.PROCESSING
    assert status.reconnects == 1
    assert workers.running == {room.id}
    assert workers.stops == 0


@pytest.mark.anyio
async def test_reconnect_after_grace_restarts_worker_once():
    room = await make_room("ev-rc2")
    path = floor_path("ev-rc2", room)
    snaps: dict[str, PathSnapshot | None] = {path: AV}
    workers = FakeWorkers()
    clock = Clock()
    sup = make_supervisor(snaps, workers, clock, grace=5)
    await sup.tick()
    snaps[path] = OFFLINE
    clock.now += 1
    await sup.tick()
    clock.now += 10
    await sup.tick()
    assert workers.running == set()

    snaps[path] = AV
    clock.now += 1
    await sup.tick()
    await sup.tick()
    assert workers.running == {room.id}
    assert sup.status_for(room.id).state == IngestState.PROCESSING


@pytest.mark.anyio
async def test_stalled_feed_is_degraded():
    room = await make_room("ev-stall")
    path = floor_path("ev-stall", room)
    clock = Clock()
    sup = make_supervisor({path: AV}, FakeWorkers(), clock, stall=10)
    await sup.tick()
    clock.now += 11
    await sup.tick()
    status = sup.status_for(room.id)
    assert status.state == IngestState.DEGRADED
    assert "no media" in status.detail


@pytest.mark.anyio
async def test_publisher_replacement_is_counted():
    room = await make_room("ev-rep")
    path = floor_path("ev-rep", room)
    snaps: dict[str, PathSnapshot | None] = {path: AV}
    sup = make_supervisor(snaps, FakeWorkers(), Clock())
    await sup.tick()
    snaps[path] = PathSnapshot(**{**AV.__dict__, "source_id": "sess-other"})
    await sup.tick()
    assert sup.status_for(room.id).replacements == 1


@pytest.mark.anyio
async def test_two_rooms_are_isolated():
    room_a = await make_room("ev-iso-a")
    room_b = await make_room("ev-iso-b")
    workers = FakeWorkers()
    sup = make_supervisor({floor_path("ev-iso-a", room_a): AV}, workers, Clock())
    await sup.tick()
    assert sup.status_for(room_a.id).state == IngestState.PROCESSING
    assert sup.status_for(room_b.id).state == IngestState.WAITING
    assert workers.running == {room_a.id}


@pytest.mark.anyio
async def test_bot_mode_rooms_are_not_supervised():
    room = await make_room("ev-botmode", mode=FLOOR_SOURCE_JITSI_BOT)
    workers = FakeWorkers()
    sup = make_supervisor({floor_path("ev-botmode", room): AV}, workers, Clock())
    await sup.tick()
    assert sup.status_for(room.id) is None
    assert workers.starts == 0


@pytest.mark.anyio
async def test_leaving_program_mode_stops_owned_worker():
    from portal.database import get_session

    room = await make_room("ev-leave")
    workers = FakeWorkers()
    sup = make_supervisor({floor_path("ev-leave", room): AV}, workers, Clock())
    await sup.tick()
    assert workers.running == {room.id}

    async with get_session() as s:
        (await s.get(Room, room.id)).floor_source_mode = FLOOR_SOURCE_JITSI_BOT
    await sup.tick()
    assert workers.running == set()
    assert sup.status_for(room.id) is None


@pytest.mark.anyio
async def test_capacity_limits_new_feeds_but_not_connected_rooms():
    room_a = await make_room("ev-cap-a")
    room_b = await make_room("ev-cap-b")
    sup = make_supervisor({floor_path("ev-cap-a", room_a): AV}, FakeWorkers(), Clock(), max_active=1)
    await sup.tick()
    assert sup.can_accept_new_feed(room_a.id)
    assert not sup.can_accept_new_feed(room_b.id)


@pytest.mark.anyio
async def test_mediamtx_unreachable_is_degraded():
    room = await make_room("ev-down")
    sup = make_supervisor({floor_path("ev-down", room): None}, FakeWorkers(), Clock())
    await sup.tick()
    assert sup.status_for(room.id).state == IngestState.DEGRADED


@pytest.mark.anyio
async def test_worker_start_failure_is_degraded():
    room = await make_room("ev-full")
    workers = FakeWorkers(fail_with="System at maximum capacity")
    sup = make_supervisor({floor_path("ev-full", room): AV}, workers, Clock())
    await sup.tick()
    status = sup.status_for(room.id)
    assert status.state == IngestState.DEGRADED
    assert "maximum capacity" in status.detail


@pytest.mark.anyio
async def test_status_payload_has_no_credential_material():
    from portal.database import get_session
    from portal.program_ingest.credentials import apply_issued_secret, issue_ingest_secret

    room = await make_room("ev-pub")
    issued = issue_ingest_secret()
    async with get_session() as s:
        apply_issued_secret(await s.get(Room, room.id), issued)
    sup = make_supervisor({floor_path("ev-pub", room): AV}, FakeWorkers(), Clock())
    await sup.tick()
    payload = repr(sup.status_for(room.id).as_public_dict())
    assert issued.secret not in payload and issued.digest not in payload


@pytest.mark.anyio
async def test_real_worker_registry_never_duplicates_floor_worker():
    """Drive the real floor worker helpers; the session's media loop is stubbed out."""
    from portal.config import settings
    from portal.transcription import worker as worker_mod
    from portal.websockets.manager import broadcast_transcription

    room = await make_room("ev-real")
    path = floor_path("ev-real", room)
    snaps: dict[str, PathSnapshot | None] = {path: AV}
    clock = Clock()
    config = SupervisorConfig(max_active=4, disconnect_grace_seconds=5, stall_seconds=60, poll_seconds=1)

    async def fetch() -> dict[str, PathSnapshot] | None:
        return {p: snap for p, snap in snaps.items() if snap is not None and snap.online}

    def fake_start(self):
        self.state = worker_mod.State.RUNNING

    async def clear(p: str) -> bool:
        return True

    sup = ProgramIngestSupervisor(config=config, fetch_snapshots=fetch, clear_path_config=clear, clock=clock)
    booth_id = f"ev-real-{room.id}-floor"
    with (
        patch.object(worker_mod.TranscriptionWorkerSession, "start", fake_start),
        patch("portal.transcription.providers.local.start_eviction_loop"),
    ):
        try:
            await sup.tick()
            first = worker_mod.active_workers[booth_id]
            # Same reader and pipeline entry point the floor bot path uses.
            assert first.rtsp_url == f"{settings.mediamtx_rtsp_base}/{path}"
            assert first.room_id == room.id
            assert first.broadcast_callback is broadcast_transcription
            await sup.tick()
            assert worker_mod.active_workers[booth_id] is first
            assert sum(1 for k in worker_mod.active_workers if k == booth_id) == 1

            snaps[path] = OFFLINE
            clock.now += 1
            await sup.tick()
            clock.now += 10
            await sup.tick()
            assert booth_id not in worker_mod.active_workers
        finally:
            worker_mod.active_workers.pop(booth_id, None)


@pytest.mark.anyio
async def test_fetch_snapshot_handles_404_errors_and_json():
    import httpx

    from portal.program_ingest import mediamtx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing/1/floor"):
            return httpx.Response(404)
        if request.url.path.endswith("/broken/1/floor"):
            return httpx.Response(500)
        return httpx.Response(
            200, json={"online": True, "source": {"type": "webRTCSession", "id": "a"}, "tracks2": [{"codec": "Opus"}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        assert (await mediamtx.fetch_path_snapshot("missing/1/floor")) == OFFLINE
        assert (await mediamtx.fetch_path_snapshot("broken/1/floor")) is None
        assert (await mediamtx.fetch_path_snapshot("ok/1/floor")).audio_codecs == ("Opus",)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    down = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with patch.object(mediamtx, "get_http_client", return_value=down):
        assert (await mediamtx.fetch_path_snapshot("ok/1/floor")) is None


@pytest.mark.anyio
async def test_remove_path_config_clears_alwaysavailable_cache():
    import httpx

    from portal.program_ingest import mediamtx
    from portal.utils import _created_paths

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200)

    _created_paths.add("ev/1/floor")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        await mediamtx.remove_path_config("ev/1/floor")
    assert "ev/1/floor" not in _created_paths
    assert seen == [("DELETE", "/v3/config/paths/delete/ev/1/floor")]


@pytest.mark.anyio
async def test_kick_publisher_targets_the_live_session():
    import httpx

    from portal.program_ingest import mediamtx

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"online": True, "source": {"type": "webRTCSession", "id": "sess-9"}})
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        assert await mediamtx.kick_publisher("ev/1/floor")
    assert calls[-1] == ("POST", "/v3/webrtc/sessions/kick/sess-9")


@pytest.mark.anyio
async def test_kick_publisher_noop_when_offline():
    import httpx

    from portal.program_ingest import mediamtx

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        assert not await mediamtx.kick_publisher("ev/1/floor")


@pytest.mark.anyio
async def test_fetch_path_snapshots_pages_through_list():
    import httpx

    from portal.program_ingest import mediamtx

    pages = {
        "0": {
            "pageCount": 2,
            "items": [
                {
                    "name": "ev/1/floor",
                    "online": True,
                    "source": {"type": "webRTCSession", "id": "a"},
                    "tracks2": [{"codec": "Opus"}],
                }
            ],
        },
        "1": {"pageCount": 2, "items": [{"name": "ev/2/floor", "online": False, "source": None}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/paths/list"
        return httpx.Response(200, json=pages[request.url.params["page"]])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        snaps = await mediamtx.fetch_path_snapshots()
    assert snaps["ev/1/floor"].online and snaps["ev/1/floor"].audio_codecs == ("Opus",)
    assert not snaps["ev/2/floor"].online

    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with patch.object(mediamtx, "get_http_client", return_value=down):
        assert await mediamtx.fetch_path_snapshots() is None


@pytest.mark.anyio
async def test_path_config_removal_retried_until_confirmed():
    room = await make_room("ev-cfg")
    attempts: list[str] = []
    results = iter([False, False, True])

    async def flaky_clear(path: str) -> bool:
        attempts.append(path)
        return next(results)

    sup = make_supervisor({}, FakeWorkers(), Clock())
    sup.clear_path_config = flaky_clear
    for _ in range(5):
        await sup.tick()
    assert attempts == [f"ev-cfg/{room.id}/floor"] * 3
    assert room.id in sup.path_config_cleared


@pytest.mark.anyio
async def test_kick_publisher_raises_when_mediamtx_fails():
    import httpx

    from portal.program_ingest import mediamtx

    def kick_fails(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"online": True, "source": {"type": "webRTCSession", "id": "s1"}})
        return httpx.Response(500)

    for transport in (
        httpx.MockTransport(kick_fails),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    ):
        client = httpx.AsyncClient(transport=transport)
        with patch.object(mediamtx, "get_http_client", return_value=client), pytest.raises(mediamtx.PublisherKickError):
            await mediamtx.kick_publisher("ev/1/floor")


@pytest.mark.anyio
async def test_kick_publisher_session_already_gone_is_success():
    import httpx

    from portal.program_ingest import mediamtx

    def gone(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"online": True, "source": {"type": "webRTCSession", "id": "s1"}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(gone))
    with patch.object(mediamtx, "get_http_client", return_value=client):
        assert await mediamtx.kick_publisher("ev/1/floor") is False


@pytest.mark.anyio
async def test_path_config_cleanup_runs_concurrently_across_rooms():
    import asyncio

    for i in range(5):
        await make_room(f"ev-par-{i}")
    in_flight = 0
    peak = 0

    async def slow_failing_clear(path: str) -> bool:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return False

    sup = make_supervisor({}, FakeWorkers(), Clock())
    sup.clear_path_config = slow_failing_clear
    await sup.tick()
    assert peak == 5
    assert sup.path_config_cleared == set()
    # Still retried on the next tick, and rooms are evaluated regardless.
    await sup.tick()
    assert all(sup.status_for(rid) is not None for rid in range(1, 6))
