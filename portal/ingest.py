from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, Thread
from typing import Any

from .config import Settings

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRecorder

    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency path
    RTCPeerConnection = None
    RTCSessionDescription = None
    MediaRecorder = None
    AIORTC_AVAILABLE = False


class IngestError(Exception):
    """Base error for ingest service operations."""


class IngestUnavailableError(IngestError):
    """Raised when aiortc is not installed or disabled."""


@dataclass
class IngestSession:
    channel_id: str
    booth_id: str
    participant_id: str
    peer_connection: Any
    recorder: Any
    connection_state: str = 'new'
    recorder_started: bool = False


class AsyncRuntime:
    """Dedicated runtime loop for long-lived aiortc sessions."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_forever, daemon=True)
        self.thread.start()

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coroutine: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class IngestService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtime = AsyncRuntime()
        self.sessions: dict[str, IngestSession] = {}
        self.lock = RLock()

    def connect(
        self,
        *,
        channel_id: str,
        booth_id: str,
        participant_id: str,
        offer_sdp: str,
        offer_type: str,
    ) -> dict[str, str]:
        if not AIORTC_AVAILABLE:
            raise IngestUnavailableError('aiortc is not installed. Install requirements to enable ingest.')
        return self.runtime.run(
            self._connect(
                channel_id=channel_id,
                booth_id=booth_id,
                participant_id=participant_id,
                offer_sdp=offer_sdp,
                offer_type=offer_type,
            )
        )

    def disconnect(self, channel_id: str) -> None:
        self.runtime.run(self._disconnect(channel_id))

    def status(self, channel_id: str) -> str:
        with self.lock:
            session = self.sessions.get(channel_id)
            if session is None:
                return 'disconnected'
            return session.connection_state

    async def _connect(
        self,
        *,
        channel_id: str,
        booth_id: str,
        participant_id: str,
        offer_sdp: str,
        offer_type: str,
    ) -> dict[str, str]:
        await self._disconnect(channel_id)
        peer_connection = RTCPeerConnection()
        recorder = self._build_recorder(channel_id)
        session = IngestSession(
            channel_id=channel_id,
            booth_id=booth_id,
            participant_id=participant_id,
            peer_connection=peer_connection,
            recorder=recorder,
        )

        @peer_connection.on('connectionstatechange')
        async def on_connectionstatechange() -> None:
            session.connection_state = peer_connection.connectionState
            if peer_connection.connectionState in {'failed', 'closed'}:
                await self._disconnect(channel_id)

        @peer_connection.on('track')
        async def on_track(track: Any) -> None:
            if track.kind != 'audio':
                return
            recorder.addTrack(track)
            if not session.recorder_started:
                await recorder.start()
                session.recorder_started = True

        await peer_connection.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)
        await self._wait_for_ice_completion(peer_connection)

        with self.lock:
            self.sessions[channel_id] = session

        local_description = peer_connection.localDescription
        return {
            'type': local_description.type,
            'sdp': local_description.sdp,
        }

    async def _disconnect(self, channel_id: str) -> None:
        with self.lock:
            session = self.sessions.pop(channel_id, None)
        if session is None:
            return
        try:
            if session.recorder_started:
                await session.recorder.stop()
        finally:
            await session.peer_connection.close()

    def _build_recorder(self, channel_id: str) -> Any:
        output_dir = Path(self.settings.ingest_hls_root) / channel_id
        output_dir.mkdir(parents=True, exist_ok=True)
        playlist = output_dir / 'playlist.m3u8'
        options = {
            'hls_time': str(self.settings.hls_segment_seconds),
            'hls_list_size': str(self.settings.hls_playlist_length),
            'hls_flags': 'delete_segments+append_list',
            'codec:a': 'aac',
            'b:a': '128k',
        }
        return MediaRecorder(
            str(playlist),
            format='hls',
            options=options,
        )

    @staticmethod
    async def _wait_for_ice_completion(peer_connection: Any) -> None:
        if peer_connection.iceGatheringState == 'complete':
            return
        done = asyncio.Event()

        @peer_connection.on('icegatheringstatechange')
        async def on_icegatheringstatechange() -> None:
            if peer_connection.iceGatheringState == 'complete':
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            return

    def shutdown(self) -> None:
        channel_ids = list(self.sessions.keys())
        for channel_id in channel_ids:
            self.disconnect(channel_id)
        self.runtime.shutdown()
