"""Listener channel switching in real Chromium over real HTTP and WebSockets.

Run: uv run --with playwright python -m playwright install chromium
     PYTHONPATH=. uv run --with playwright python tests/listener_channel_switch_browser.py
BROWSER_TEST_RUNS=10 repeats the whole rehearsal. Optional LISTENER_ARTIFACT_DIR saves
screenshots and a browser recording.

A listener moves between language channels (English and Spanish booths, floor audio)
and between translation languages. After every switch the test checks, in the browser
and on the server, that only the chosen channel's WHEP peer, caption socket and TTS
socket are left, that the audio element plays the new peer's track, and that only the
new channel's captions and translations reach the page. Rapid switches must end on the
last choice, and a rotated join code or a token for another event must still be refused.

MediaMTX is replaced by ``FakeMediaServer``: each WHEP offer is answered by a second
peer connection inside the page that sends a tone, so the listener's peer really
connects and really closes. Nothing goes to a media or AI service.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from browser_harness import CHROMIUM_ARGS, live_portal, offline_cdn, runs_requested, until
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route, async_playwright, expect

from portal.auth import create_listener_token
from portal.database import create_booth, create_event, create_room, get_session
from portal.globals import booths
from portal.join_code_limit import _join_code_attempts
from portal.models import BoothTranslationLanguage, Event, RoomTranslationLanguage
from portal.transcription.timing import SegmentTiming, segment_timing
from portal.websockets.manager import listener_manager, tts_manager

JOIN_CODE = "SWITCH1"
TRANSLATIONS = [("fr", "French"), ("de", "German")]
SOURCES = {"en": "English", "es": "Spanish", "floor": "Floor Audio (Original)"}

# Records every peer connection, WebSocket and scheduled TTS buffer the page creates,
# and lets the fake media server answer WHEP offers with a loopback peer that sends a tone.
PROBE_SCRIPT = r"""
(() => {
  const NativePeer = window.RTCPeerConnection;
  const NativeSocket = window.WebSocket;
  const probe = { peers: [], sockets: [], speech: [], answerers: new Map() };
  window.__probe = probe;
  window.__NativeWebSocket = NativeSocket;

  // TTS speech is played through AudioBufferSourceNodes; the buffer length identifies the segment.
  const nativeStart = AudioBufferSourceNode.prototype.start;
  const nativeStop = AudioBufferSourceNode.prototype.stop;
  AudioBufferSourceNode.prototype.start = function (...args) {
    this.__speech = { frames: this.buffer ? this.buffer.length : 0, stopped: false, ended: false };
    this.addEventListener('ended', () => { this.__speech.ended = true; });
    probe.speech.push(this.__speech);
    return nativeStart.apply(this, args);
  };
  AudioBufferSourceNode.prototype.stop = function (...args) {
    if (this.__speech) this.__speech.stopped = true;
    return nativeStop.apply(this, args);
  };

  window.RTCPeerConnection = class extends NativePeer {
    constructor(...args) {
      super(...args);
      this.__channel = null;
      probe.peers.push(this);
    }
  };

  window.WebSocket = class extends NativeSocket {
    constructor(...args) {
      super(...args);
      this.__closeCode = null;
      probe.sockets.push(this);
      this.addEventListener('close', (event) => { this.__closeCode = event.code; });
    }
  };

  let media = null;
  window.__answerWhep = async ({ sdp, channel, session }) => {
    // Match on the ICE username fragment: the offer's candidates can grow after it was sent.
    const ufrag = (sdp.match(/a=ice-ufrag:(\S+)/) || [])[1];
    const listener = probe.peers.find(
      (peer) => peer.localDescription && peer.localDescription.sdp.includes('a=ice-ufrag:' + ufrag));
    if (listener) listener.__channel = channel;
    // The listener moved on while the offer was in flight: answer nothing.
    if (!listener || listener.connectionState === 'closed') return null;
    if (!media) media = new AudioContext();
    const tone = media.createOscillator();
    const output = media.createMediaStreamDestination();
    tone.connect(output);
    tone.start();
    const peer = new NativePeer({ iceServers: [] });
    await peer.setRemoteDescription({ type: 'offer', sdp });
    const [transceiver] = peer.getTransceivers();
    transceiver.direction = 'sendonly';
    await transceiver.sender.replaceTrack(output.stream.getAudioTracks()[0]);
    transceiver.sender.setStreams(output.stream);
    await peer.setLocalDescription(await peer.createAnswer());
    if (peer.iceGatheringState !== 'complete') {
      await new Promise((resolve) => {
        peer.addEventListener('icegatheringstatechange', () => {
          if (peer.iceGatheringState === 'complete') resolve();
        });
        setTimeout(resolve, 2000);
      });
    }
    probe.answerers.set(session, { peer, tone });
    return peer.localDescription.sdp;
  };
  window.__endWhep = (session) => {
    const answerer = probe.answerers.get(session);
    if (!answerer) return;
    answerer.tone.stop();
    answerer.peer.close();
    probe.answerers.delete(session);
  };
})();
"""

SNAPSHOT_SCRIPT = """
() => {
  const probe = window.__probe;
  const audio = document.getElementById('audio-player');
  const track = audio.srcObject ? audio.srcObject.getAudioTracks()[0] : null;
  return {
    peers: probe.peers.map((peer) => ({
      channel: peer.__channel,
      state: peer.connectionState,
      ice: peer.iceConnectionState,
      signaling: peer.signalingState,
      playing: Boolean(track) && peer.getReceivers().some((receiver) => receiver.track === track),
    })),
    sockets: probe.sockets.map((ws) => ({
      path: new URL(ws.url).pathname,
      readyState: ws.readyState,
      closeCode: ws.__closeCode,
    })),
    audioLive: Boolean(track) && track.readyState === 'live' && !audio.paused,
    status: document.getElementById('status').textContent.trim(),
    lines: Array.from(document.querySelectorAll('#caption-history .untrusted-text')).map((el) => el.textContent),
    speech: probe.speech.map((item) => ({ ...item })),
  };
}
"""

# Opens a socket outside the probe and reports its close code, "open" if the server
# accepted it and kept it open, or "timeout" if the handshake never finished. A refused
# socket is accepted and then closed at once, so "open" waits a moment after onopen.
SOCKET_VERDICT_SCRIPT = """
(path) => new Promise((resolve) => {
  const ws = new window.__NativeWebSocket('ws://' + location.host + path);
  let settled = false;
  const finish = (verdict) => {
    if (settled) return;
    settled = true;
    clearTimeout(deadline);
    if (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN) ws.close();
    resolve(verdict);
  };
  const deadline = setTimeout(() => finish('timeout'), 5000);
  ws.onopen = () => setTimeout(() => {
    if (ws.readyState === WebSocket.OPEN) finish('open');
  }, 750);
  ws.onclose = (event) => finish(event.code);
})
"""


class PeerState(TypedDict):
    channel: str | None
    state: str
    ice: str
    signaling: str
    playing: bool


class SocketState(TypedDict):
    path: str
    readyState: int
    closeCode: int | None


class SpeechState(TypedDict):
    frames: int
    stopped: bool
    ended: bool


class Snapshot(TypedDict):
    """What the probe reports about the page at one moment."""

    peers: list[PeerState]
    sockets: list[SocketState]
    audioLive: bool
    status: str
    lines: list[str]
    speech: list[SpeechState]


class FakeMediaServer:
    """Answers the page's WHEP requests the way MediaMTX would, and records them."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.posts: list[tuple[str, float]] = []
        self.sessions: dict[str, str] = {}
        self.deleted: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.arrived: dict[str, asyncio.Event] = {}
        # Channels answered with 404, as MediaMTX does while nobody publishes.
        self.offline: set[str] = set()
        self.errors: list[str] = []

    def hold(self, channel: str) -> tuple[asyncio.Event, asyncio.Event]:
        """Hold the next answers for ``channel`` until the returned gate is set."""
        self.gates[channel] = asyncio.Event()
        self.arrived[channel] = asyncio.Event()
        return self.arrived[channel], self.gates[channel]

    def posts_since(self, channel: str, since: float) -> int:
        return sum(1 for posted, at in self.posts if posted == channel and at >= since)

    async def handle(self, route: Route) -> None:
        try:
            await self._handle(route)
        except PlaywrightError as exc:
            # Playwright swallows handler errors and leaves the request hanging; make them visible.
            self.errors.append(f"{route.request.method} {route.request.url}: {exc!r}")
            raise

    async def _handle(self, route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path.removeprefix("/media/")
        if request.method == "POST" and path.endswith("/whep"):
            channel = path.removesuffix("/whep")
            self.posts.append((channel, time.monotonic()))
            if channel in self.offline:
                await route.fulfill(status=404, body="no stream is available on path")
                return
            if channel in self.gates:
                self.arrived[channel].set()
                await self.gates[channel].wait()
            session = uuid.uuid4().hex
            answer = await self.page.evaluate(
                "args => window.__answerWhep(args)",
                {"sdp": request.post_data, "channel": channel, "session": session},
            )
            try:
                if answer is None:
                    await route.abort()
                    return
                await route.fulfill(
                    status=201,
                    headers={"Content-Type": "application/sdp", "Location": f"/media/session/{session}"},
                    body=answer,
                )
                self.sessions[session] = channel
            except PlaywrightError:
                # The page aborted the request; nobody will ever DELETE this session.
                await self.page.evaluate("s => window.__endWhep(s)", session)
        elif request.method == "DELETE" and path.startswith("session/"):
            session = path.removeprefix("session/")
            self.deleted.append(session)
            await self.page.evaluate("s => window.__endWhep(s)", session)
            await route.fulfill(status=200)
        else:
            await route.fulfill(status=404)


class Channels:
    """Names for one rehearsal's event and room."""

    def __init__(self, slug: str, room_id: int) -> None:
        self.slug = slug
        self.room_id = room_id

    def booth(self, code: str) -> str:
        return f"{self.slug}-{self.room_id}-{code}"

    def whep(self, code: str) -> str:
        return f"{self.slug}/{self.room_id}/{code}"

    def captions_path(self, code: str) -> str:
        return f"/ws/captions/{self.booth(code)}"

    def tts_path(self, code: str, target: str) -> str:
        return f"/ws/tts/{self.room_id}/{target}/{self.booth(code)}"

    def tts_key(self, code: str, target: str) -> str:
        return f"{self.room_id}-{target}-{self.booth(code)}"

    def server_caption_sockets(self) -> dict[str, int]:
        prefix = f"{self.slug}-{self.room_id}-"
        return {booth: len(sockets) for booth, sockets in listener_manager._rooms.items() if booth.startswith(prefix)}

    def server_tts_sockets(self) -> dict[str, int]:
        suffix = {self.booth(code) for code in SOURCES}
        return {key: len(s) for key, s in tts_manager._rooms.items() if key.split("-", 2)[-1] in suffix}


async def choose(page: Page, label: str, option: str) -> None:
    """Pick from one of the listener page's custom dropdowns, the way a listener does."""
    await page.get_by_role("combobox", name=label).click()
    await page.get_by_role("listbox", name=label).get_by_role("option", name=option, exact=True).click()


async def snapshot(page: Page) -> Snapshot:
    return await page.evaluate(SNAPSHOT_SCRIPT)


async def speech_playing(page: Page, frames: int) -> bool:
    """A TTS buffer of this length is scheduled and has neither been stopped nor finished."""
    items = [item for item in (await snapshot(page))["speech"] if item["frames"] == frames]
    return bool(items) and not any(item["stopped"] or item["ended"] for item in items)


async def speech_stopped(page: Page, frames: int) -> bool:
    """Every TTS buffer of this length was stopped by the page rather than left to play out."""
    items = [item for item in (await snapshot(page))["speech"] if item["frames"] == frames]
    return bool(items) and all(item["stopped"] for item in items)


async def expect_only_channel(page: Page, media: FakeMediaServer, ch: Channels, code: str, target: str | None) -> None:
    """Exactly one connected peer, caption socket and TTS socket remain, all for ``code``."""
    try:
        await expect(page.locator("#status")).to_contain_text("Live", timeout=10000)
    except AssertionError as exc:
        snap = await snapshot(page)
        raise AssertionError(
            f"{code} never went live: status={snap['status']!r} peers={snap['peers']} "
            f"recent WHEP posts={media.posts[-5:]} media errors={media.errors}"
        ) from exc
    await until(
        lambda: page.evaluate("() => window.__probe.peers.filter(p => p.connectionState === 'connected').length === 1"),
        f"one connected WHEP peer for {code}",
    )
    snap = await snapshot(page)
    live_peers = [peer for peer in snap["peers"] if peer["state"] != "closed"]
    assert len(live_peers) == 1, f"leaked or duplicate peer connections: {snap['peers']}"
    assert live_peers[0]["channel"] == ch.whep(code), live_peers
    assert live_peers[0]["playing"] and snap["audioLive"], "audio element is not playing the current peer's track"

    expected_paths = {ch.captions_path(code)} | ({ch.tts_path(code, target)} if target else set())
    open_paths = sorted(ws["path"] for ws in snap["sockets"] if ws["readyState"] in (0, 1))
    assert open_paths == sorted(expected_paths), f"open sockets {open_paths}, expected {sorted(expected_paths)}"

    # The server agrees: only the chosen channel still has this listener subscribed.
    await until(lambda: ch.server_caption_sockets() == {ch.booth(code): 1}, f"caption sockets only on {code}")
    expected_tts = {ch.tts_key(code, target): 1} if target else {}
    await until(lambda: ch.server_tts_sockets() == expected_tts, f"TTS sockets only on {code}/{target}")

    # Every WHEP session the page abandoned was deleted; only the current one is left.
    await until(
        lambda: [channel for s, channel in media.sessions.items() if s not in media.deleted] == [ch.whep(code)],
        f"DELETE for every WHEP session except the current one on {code}",
    )


async def expect_no_retries(media: FakeMediaServer, ch: Channels, page: Page, old_codes: list[str]) -> None:
    """Once settled, nothing reconnects to channels the listener left (a back-off loop retries within 1.5 s)."""
    since = time.monotonic()
    await asyncio.sleep(1.5)
    for code in old_codes:
        assert media.posts_since(ch.whep(code), since) == 0, f"WHEP retried old channel {code}"
    snap = await snapshot(page)
    open_paths = {ws["path"] for ws in snap["sockets"] if ws["readyState"] in (0, 1)}
    for code in old_codes:
        assert not any(path.endswith("/" + ch.booth(code)) for path in open_paths), f"socket reopened for {code}"


class Feeds:
    """Sends captions and translations from the server, as transcription and TTS do.

    Like the real pipeline, each booth numbers its segments, a caption carries its segment's
    ``seq``, and the translation bundle for that segment reuses it.
    """

    def __init__(self, ch: Channels) -> None:
        self.ch = ch
        self.seqs: dict[str, int] = {}
        self.sent = 0

    def next_seq(self, code: str) -> int:
        self.seqs[code] = self.seqs.get(code, 0) + 1
        return self.seqs[code]

    async def caption(self, code: str, text: str, seq: int, delay_ms: int = 0) -> None:
        message = {"type": "caption", "status": "final", "segment_id": uuid.uuid4().hex, "seq": seq, "text": text}
        if delay_ms:
            # Program-stream timing: the page holds the caption back by sync_offset_ms.
            now_ms = int(time.time() * 1000)
            message |= {"source_received_at_ms": now_ms, "sync_offset_ms": delay_ms, "server_sent_at_ms": now_ms}
        await listener_manager.broadcast(self.ch.booth(code), message)

    async def translation(
        self, code: str, target: str, text: str, seq: int, *, delay_ms: int = 0, speech_frames: int = 0
    ) -> None:
        """Send a TTS bundle; ``speech_frames`` of silent 24 kHz PCM make it a spoken segment."""
        token = segment_timing.set(SegmentTiming(int(time.time() * 1000), delay_ms) if delay_ms else None)
        try:
            await tts_manager.broadcast_bundle(
                self.ch.room_id,
                target,
                self.ch.booth(code),
                bytes(2 * speech_frames),
                segment_id=uuid.uuid4().hex,
                seq=seq,
                translation=text,
                audio_pending=False,
            )
        finally:
            segment_timing.reset(token)

    async def translated_caption(self, code: str, target: str, text: str, seq: int) -> None:
        message = {
            "type": "translated_caption",
            "status": "final",
            "language_code": target,
            "segment_id": uuid.uuid4().hex,
            "seq": seq,
            "text": text,
        }
        await listener_manager.broadcast(self.ch.booth(code), message)

    async def silent_bundles(self, code: str, seq: int, skip: str | None = None) -> None:
        """TTS bundles with nothing to say, which the server sends for every segment and language."""
        for lang, _ in TRANSLATIONS:
            if lang != skip:
                await self.translation(code, lang, "", seq)

    async def segment(self, code: str, caption: str, translations: dict[str, str]) -> int:
        seq = self.next_seq(code)
        await self.caption(code, caption, seq)
        for target, text in translations.items():
            await self.translation(code, target, text, seq)
        return seq

    async def expect_routing(self, page: Page, code: str, target: str | None) -> None:
        """Feeds for every other channel stay off the page; the current channel's arrive exactly once."""
        self.sent += 1
        stale: list[str] = []
        fresh: list[str] = []
        for source in SOURCES:
            current = source == code
            caption = f"{'live' if current else 'stale'} caption from {source} #{self.sent}"
            (fresh if current else stale).append(caption)
            translations = {}
            for lang, _ in TRANSLATIONS:
                chosen = current and lang == target
                translations[lang] = f"{'live' if chosen else 'stale'} {lang} translation from {source} #{self.sent}"
                (fresh if chosen else stale).append(translations[lang])
            seq = await self.segment(source, caption, translations)
            # Translations pushed on the caption socket must match the chosen language too.
            for lang, _ in TRANSLATIONS:
                if not (current and lang == target):
                    text = f"stale {lang} translated_caption from {source} #{self.sent}"
                    stale.append(text)
                    await self.translated_caption(source, lang, text, seq)
        if target:
            # The chosen language's translated_caption arrives on its own, without a TTS bundle.
            text = f"live {target} translated_caption from {code} #{self.sent}"
            fresh.append(text)
            seq = self.next_seq(code)
            await self.translated_caption(code, target, text, seq)
            await self.silent_bundles(code, seq)
        for text in fresh:
            await expect(page.locator("#caption-history")).to_contain_text(text)
        await asyncio.sleep(0.3)
        lines = (await snapshot(page))["lines"]
        leaked = [text for text in stale if text in lines]
        assert not leaked, f"old channel feed reached the page: {leaked}"
        repeated = [text for text in fresh if lines.count(text) != 1]
        assert not repeated, f"live feed shown more than once: {repeated}"


async def seed(run: int) -> tuple[Channels, int]:
    """An event with English and Spanish booths and floor audio, each translated to French and German."""
    async with get_session() as s:
        event = await create_event(s, slug=f"switch-{run}", display_name="Channel switch rehearsal")
        event.listener_join_code = JOIN_CODE
        room = await create_room(s, event_id=event.id, display_name="Main hall")
        room.floor_transcription_enabled = True
        room.floor_transcription_provider = "local"
        room.floor_translation_enabled = True
        room.floor_tts_enabled = True
        for code, name in TRANSLATIONS:
            s.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True))
        for code in ("en", "es"):
            booth = await create_booth(
                s, event_id=event.id, room_id=room.id, language_code=code, language_name=SOURCES[code]
            )
            booth.translation_enabled = True
            for lang, name in TRANSLATIONS:
                s.add(BoothTranslationLanguage(booth_id=booth.id, language_code=lang, language_name=name, enabled=True))
        await create_event(s, slug=f"elsewhere-{run}", display_name="Another event")
        ch = Channels(event.slug, room.id)
        event_id = event.id
    # Interpreters are live in both booths, so the page starts WHEP as soon as it is told so.
    for code in ("en", "es"):
        await booths.create_booth(event_slug=ch.slug, language_code=code, language=SOURCES[code], room_id=ch.room_id)
        live = booths.get_booth_sync(ch.booth(code))
        live.broadcast_unlocked = True
        live.ingest_status = "connected"
    return ch, event_id


async def rehearse(browser, origin: str, run: int, artifacts: Path | None) -> None:
    _join_code_attempts.clear()
    ch, event_id = await seed(run)
    options = {"viewport": {"width": 1280, "height": 900}}
    if artifacts:
        options["record_video_dir"] = str(artifacts)
    context = await browser.new_context(**options)
    await context.add_init_script(PROBE_SCRIPT)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    media = FakeMediaServer(page)
    await page.route(f"{origin}/media/**", media.handle)
    await page.route("https://cdn.tailwindcss.com/**", offline_cdn)
    feeds = Feeds(ch)

    async def shot(name: str) -> None:
        if artifacts:
            await page.screenshot(path=str(artifacts / f"run{run}-{name}.png"), full_page=True)

    try:
        # The only room is preselected; the listener picks English and a French translation.
        await page.goto(f"{origin}/listener/{ch.slug}?code={JOIN_CODE}")
        await choose(page, "Source Audio", "English")
        await expect_only_channel(page, media, ch, "en", None)
        await feeds.expect_routing(page, "en", None)
        await choose(page, "Target Language", "French")
        await expect_only_channel(page, media, ch, "en", "fr")
        await feeds.expect_routing(page, "en", "fr")
        await shot("1-english")

        # Booth switch: WHEP, captions and TTS all move to Spanish; French is kept.
        await choose(page, "Source Audio", "Spanish")
        await expect_only_channel(page, media, ch, "es", "fr")
        await feeds.expect_routing(page, "es", "fr")
        await expect_no_retries(media, ch, page, ["en"])

        # Translation switch: only the TTS socket moves.
        await choose(page, "Target Language", "German")
        await expect_only_channel(page, media, ch, "es", "de")
        await feeds.expect_routing(page, "es", "de")

        # Captions and translations held back for the program delay arrive late on the current
        # channel, and are dropped if the listener switches before they are due.
        delay_ms = 1500
        due = f"delayed caption on es #{run}"
        due_seq = feeds.next_seq("es")
        await feeds.caption("es", due, due_seq, delay_ms)
        await feeds.silent_bundles("es", due_seq)
        await asyncio.sleep(0.5)
        assert due not in (await snapshot(page))["lines"], "delayed caption shown before it was due"
        await expect(page.locator("#caption-history")).to_contain_text(due, timeout=5000)
        held = [f"held caption on es #{run}", f"held de translation on es #{run}"]
        held_seq = feeds.next_seq("es")
        await feeds.caption("es", held[0], held_seq, delay_ms)
        await feeds.translation("es", "de", held[1], held_seq, delay_ms=delay_ms)
        await feeds.silent_bundles("es", held_seq, skip="de")
        held_due = time.monotonic() + delay_ms / 1000

        # Floor audio starts WHEP without waiting for a booth to go live.
        await choose(page, "Source Audio", SOURCES["floor"])
        await expect_only_channel(page, media, ch, "floor", "de")
        await feeds.expect_routing(page, "floor", "de")
        await expect_no_retries(media, ch, page, ["es"])
        await asyncio.sleep(max(0.0, held_due + 0.5 - time.monotonic()))
        leaked = [text for text in held if text in (await snapshot(page))["lines"]]
        assert not leaked, f"feed held for the old channel was shown after the switch: {leaked}"

        # Speech already scheduled for the old language stops when the listener switches.
        # The buffer length tells the segments apart: 4 s of German, 3 s of French at 24 kHz.
        german, french = 96000, 72000
        await page.get_by_text("Read translation aloud").click()
        await expect(page.locator("#status")).to_contain_text("German TTS")
        await until(lambda: ch.server_tts_sockets() == {ch.tts_key("floor", "de"): 1}, "TTS socket for read-aloud")
        await feeds.translation("floor", "de", f"spoken de #{run}", feeds.next_seq("floor"), speech_frames=german)
        await until(lambda: speech_playing(page, german), "German speech scheduled")
        await choose(page, "Target Language", "French")
        await until(lambda: speech_stopped(page, german), "German speech stopped after switching to French")
        await until(lambda: ch.server_tts_sockets() == {ch.tts_key("floor", "fr"): 1}, "TTS socket moved to French")
        await feeds.translation("floor", "fr", f"spoken fr #{run}", feeds.next_seq("floor"), speech_frames=french)
        await until(lambda: speech_playing(page, french), "French speech scheduled")
        await choose(page, "Source Audio", "Spanish")
        await until(lambda: speech_stopped(page, french), "French speech stopped after switching to Spanish")
        await expect_only_channel(page, media, ch, "es", "fr")
        await choose(page, "Target Language", "German")
        await expect_only_channel(page, media, ch, "es", "de")
        await shot("2-spanish")

        # Leaving a channel whose WHEP is failing stops its back-off loop.
        media.offline.add(ch.whep("floor"))
        attempts = len(media.posts)
        await choose(page, "Source Audio", SOURCES["floor"])
        await expect(page.locator("#status")).to_contain_text("Reconnecting", timeout=10000)
        await until(lambda: len(media.posts) - attempts >= 3, "floor WHEP retrying with back-off")
        assert {channel for channel, _ in media.posts[attempts:]} == {ch.whep("floor")}, media.posts[attempts:]
        await choose(page, "Source Audio", "Spanish")
        await expect_only_channel(page, media, ch, "es", "de")
        await expect_no_retries(media, ch, page, ["floor"])
        media.offline.clear()

        # Rapid A -> B -> A within one task: floor's peer is created and dropped before it sends an offer.
        await page.evaluate(
            """(names) => {
              const select = document.getElementById('language-select');
              for (const name of names) {
                select.selectedIndex = Array.from(select.options).findIndex((o) => o.text === name);
                select.dispatchEvent(new Event('change'));
              }
            }""",
            [SOURCES["floor"], "Spanish"],
        )
        await expect_only_channel(page, media, ch, "es", "de")
        await feeds.expect_routing(page, "es", "de")
        await expect_no_retries(media, ch, page, ["floor"])

        # Rapid A -> B -> A through the dropdown.
        await choose(page, "Source Audio", "English")
        await choose(page, "Source Audio", "Spanish")
        await expect_only_channel(page, media, ch, "es", "de")
        await feeds.expect_routing(page, "es", "de")
        await expect_no_retries(media, ch, page, ["en"])

        # Rapid A -> B -> A with B's WHEP offer still waiting for an answer.
        arrived, gate = media.hold(ch.whep("floor"))
        await choose(page, "Source Audio", SOURCES["floor"])
        await asyncio.wait_for(arrived.wait(), 10)
        await choose(page, "Source Audio", "Spanish")
        gate.set()
        del media.gates[ch.whep("floor")]
        await expect_only_channel(page, media, ch, "es", "de")
        await feeds.expect_routing(page, "es", "de")
        await expect_no_retries(media, ch, page, ["floor"])
        await shot("3-after-rapid-switches")

        # Token scope: tokens for another event are refused on the new channel even with a valid
        # code cookie, a malformed token is refused, and a token for this event is accepted.
        foreign = create_listener_token(event_slug=f"elsewhere-{run}")
        own = create_listener_token(event_slug=ch.slug)
        verdicts = {
            "captions, other event": (f"{ch.captions_path('es')}?token={foreign}", 4003),
            "tts, other event": (f"{ch.tts_path('es', 'de')}?token={foreign}", 4003),
            "captions, malformed": (f"{ch.captions_path('es')}?token=not-a-jwt", 4001),
            "captions, this event": (f"{ch.captions_path('es')}?token={own}", "open"),
            "tts, this event": (f"{ch.tts_path('es', 'de')}?token={own}", "open"),
        }
        for name, (path, expected) in verdicts.items():
            got = await page.evaluate(SOCKET_VERDICT_SCRIPT, path)
            assert got == expected, f"{name}: expected {expected}, got {got}"
        await expect_only_channel(page, media, ch, "es", "de")

        # Join-code scope: after the organiser rotates the code, switching channel is refused
        # (captions 4001, TTS 4003), the listener is told, and nothing retries.
        async with get_session() as s:
            (await s.get(Event, event_id)).listener_join_code = "ROTATED"
        before = len((await snapshot(page))["sockets"])
        since = time.monotonic()
        await choose(page, "Source Audio", "English")
        await expect(page.locator("#status")).to_contain_text("Access ended", timeout=10000)
        await asyncio.sleep(2.5)  # longer than the 1 s caption and TTS retry delays
        snap = await snapshot(page)
        opened = sorted((ws["path"], ws["closeCode"]) for ws in snap["sockets"][before:])
        assert opened == sorted([(ch.captions_path("en"), 4001), (ch.tts_path("en", "de"), 4003)]), (
            f"expected one refused caption and TTS socket and no retries, got {opened}"
        )
        still_open = [ws for ws in snap["sockets"] if ws["readyState"] in (0, 1)]
        assert not still_open, f"sockets still open after refusal: {still_open}"
        assert all(peer["state"] == "closed" for peer in snap["peers"]), snap["peers"]
        assert media.posts_since(ch.whep("en"), since) == 0, "WHEP started without caption access"
        await until(
            lambda: ch.server_caption_sockets() == {} and ch.server_tts_sockets() == {},
            "server drops every socket for this listener",
        )
        await expect_no_retries(media, ch, page, ["es"])
        await shot("4-code-rotated")
        assert not errors, errors
    finally:
        await context.close()
        for code in ("en", "es"):
            await booths.remove_booth(ch.slug, ch.room_id, code)


async def main() -> None:
    artifacts = Path(os.environ["LISTENER_ARTIFACT_DIR"]) if os.environ.get("LISTENER_ARTIFACT_DIR") else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    runs = runs_requested()
    async with live_portal() as origin, async_playwright() as p:
        browser = await p.chromium.launch(args=CHROMIUM_ARGS)
        try:
            for run in range(1, runs + 1):
                started = time.monotonic()
                await rehearse(browser, origin, run, artifacts)
                print(f"run {run}/{runs} passed in {time.monotonic() - started:.1f}s", flush=True)
        finally:
            await browser.close()
    print(
        "PASS: booth, translation and floor switches move WHEP, caption and TTS feeds; old peers and sockets "
        "close and never retry; rapid A->B->A ends on A; token and join-code scope hold; no browser JS errors"
    )


if __name__ == "__main__":
    asyncio.run(main())
