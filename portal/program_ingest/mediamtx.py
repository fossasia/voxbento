"""MediaMTX Control API adapter for program ingest status."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from portal.config import settings
from portal.globals import get_http_client
from portal.utils import _created_paths

logger = logging.getLogger(__name__)

# Codecs ffmpeg can decode to PCM for STT (MediaMTX PathTrackCodec names).
DECODABLE_AUDIO_CODECS = frozenset(
    {
        "Opus",
        "MPEG-4 Audio",
        "MPEG-4 Audio LATM",
        "MPEG-1/2 Audio",
        "AC3",
        "FLAC",
        "Vorbis",
        "Speex",
        "G711",
        "G722",
        "G726",
        "LPCM",
    }
)
VIDEO_CODECS = frozenset({"AV1", "VP9", "VP8", "H265", "H264", "MPEG-4 Video", "MPEG-1/2 Video", "M-JPEG"})


@dataclass(frozen=True)
class PathSnapshot:
    online: bool
    source_type: str | None = None
    source_id: str | None = None
    audio_codecs: tuple[str, ...] = ()
    video_codecs: tuple[str, ...] = ()
    unsupported_codecs: tuple[str, ...] = ()
    inbound_bytes: int = 0

    @property
    def has_decodable_audio(self) -> bool:
        return bool(self.audio_codecs)


OFFLINE = PathSnapshot(online=False)


def track_codecs(data: dict) -> list[str]:
    tracks2 = data.get("tracks2")
    if isinstance(tracks2, list):
        return [t["codec"] for t in tracks2 if isinstance(t, dict) and isinstance(t.get("codec"), str)]
    tracks = data.get("tracks")
    if isinstance(tracks, list):
        return [t for t in tracks if isinstance(t, str)]
    return []


def parse_path_snapshot(data: object) -> PathSnapshot:
    """Parse a ``GET /v3/paths/get/{name}`` body; tolerant of old and new MediaMTX field names."""
    if not isinstance(data, dict):
        return OFFLINE
    source = data.get("source") if isinstance(data.get("source"), dict) else None
    if "online" in data:
        online = data.get("online") is True
    else:
        # MediaMTX < 1.15: "ready" without alwaysAvailable means a publisher is present.
        online = data.get("ready") is True and source is not None
    if not online:
        return OFFLINE

    audio: list[str] = []
    video: list[str] = []
    unsupported: list[str] = []
    for codec in track_codecs(data):
        if codec in DECODABLE_AUDIO_CODECS:
            audio.append(codec)
        elif codec in VIDEO_CODECS:
            video.append(codec)
        else:
            unsupported.append(codec)

    inbound = data.get("inboundBytes", data.get("bytesReceived", 0))
    source_type = source.get("type") if source else None
    source_id = source.get("id") if source else None
    return PathSnapshot(
        online=True,
        source_type=source_type if isinstance(source_type, str) else None,
        source_id=source_id if isinstance(source_id, str) else None,
        audio_codecs=tuple(audio),
        video_codecs=tuple(video),
        unsupported_codecs=tuple(unsupported),
        inbound_bytes=inbound if isinstance(inbound, int) else 0,
    )


async def fetch_path_snapshot(path: str) -> PathSnapshot | None:
    """Return the live state of ``path``; ``None`` when the Control API is unreachable."""
    url = f"{settings.mediamtx_api_base}/v3/paths/get/{quote(path, safe='/')}"
    try:
        response = await get_http_client().get(url, timeout=3.0)
    except httpx.HTTPError as exc:
        logger.warning("MediaMTX status unavailable for program ingest path=%s: %s", path, exc)
        return None
    if response.status_code == 404:
        return OFFLINE
    if response.status_code != 200:
        logger.warning("MediaMTX status error for program ingest path=%s status=%s", path, response.status_code)
        return None
    try:
        return parse_path_snapshot(response.json())
    except ValueError:
        logger.warning("MediaMTX returned invalid JSON for program ingest path=%s", path)
        return None


LIST_PAGE_SIZE = 500


async def fetch_path_snapshots() -> dict[str, PathSnapshot] | None:
    """Return live snapshots of every active MediaMTX path, keyed by path name.

    One paginated list call per poll keeps load flat as rooms grow and avoids a
    404 (logged as an error by MediaMTX) for every offline room. ``None`` means
    the Control API is unreachable or returned something unusable.
    """
    snapshots: dict[str, PathSnapshot] = {}
    page = 0
    while True:
        url = f"{settings.mediamtx_api_base}/v3/paths/list?itemsPerPage={LIST_PAGE_SIZE}&page={page}"
        try:
            response = await get_http_client().get(url, timeout=3.0)
        except httpx.HTTPError as exc:
            logger.warning("MediaMTX path list unavailable for program ingest: %s", exc)
            return None
        if response.status_code != 200:
            logger.warning("MediaMTX path list error for program ingest status=%s", response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("MediaMTX path list returned invalid JSON")
            return None
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return None
        for item in body["items"]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                snapshots[item["name"]] = parse_path_snapshot(item)
        page_count = body.get("pageCount")
        page += 1
        if not isinstance(page_count, int) or page >= page_count:
            return snapshots


async def remove_path_config(path: str) -> bool:
    """Drop any runtime path config (e.g. alwaysAvailable Opus) so the encoder's own tracks are used.

    :returns: whether the path is now free of a runtime config (removed or never present).
    """
    _created_paths.discard(path)
    url = f"{settings.mediamtx_api_base}/v3/config/paths/delete/{quote(path, safe='/')}"
    try:
        response = await get_http_client().delete(url, timeout=3.0)
    except httpx.HTTPError as exc:
        logger.warning("Could not remove MediaMTX path config path=%s: %s", path, exc)
        return False
    if response.status_code not in (200, 404):
        logger.warning("MediaMTX path config removal failed path=%s status=%s", path, response.status_code)
        return False
    return True


# MediaMTX source type → Control API kick collection.
KICK_ENDPOINTS = {
    "webRTCSession": "webrtc/sessions",
    "rtmpConn": "rtmp/conns",
    "rtmpsConn": "rtmps/conns",
    "srtConn": "srt/conns",
    "rtspSession": "rtsp/sessions",
    "rtspsSession": "rtsps/sessions",
}


class PublisherKickError(Exception):
    """MediaMTX could not confirm that the current publisher was disconnected."""


async def kick_publisher(path: str) -> bool:
    """Disconnect whoever is publishing ``path``.

    MediaMTX only authenticates at connect time, so revocation, rotation and
    source-mode switches must also end the established session.

    :returns: ``True`` if a session was kicked, ``False`` if nobody was publishing.
    :raises PublisherKickError: when MediaMTX is unreachable or the kick fails,
        i.e. the old publisher may still be connected.
    """
    snapshot = await fetch_path_snapshot(path)
    if snapshot is None:
        raise PublisherKickError(f"MediaMTX status unavailable for {path}")
    if not snapshot.online:
        return False
    collection = KICK_ENDPOINTS.get(snapshot.source_type or "")
    if collection is None or not snapshot.source_id:
        raise PublisherKickError(f"Cannot disconnect source type {snapshot.source_type!r} on {path}")
    url = f"{settings.mediamtx_api_base}/v3/{collection}/kick/{quote(snapshot.source_id, safe='')}"
    try:
        response = await get_http_client().post(url, timeout=3.0)
    except httpx.HTTPError as exc:
        raise PublisherKickError(f"MediaMTX kick request failed for {path}") from exc
    if response.status_code == 404:
        # The session ended between the status read and the kick.
        return False
    if response.status_code != 200:
        raise PublisherKickError(f"MediaMTX kick for {path} returned HTTP {response.status_code}")
    logger.info("program ingest publisher disconnected path=%s source_type=%s", path, snapshot.source_type)
    return True
