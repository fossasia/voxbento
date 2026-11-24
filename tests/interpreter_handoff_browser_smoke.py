"""Real Chromium test for the interpreter active/backup switch (#1378).

Run: uv run --with playwright python -m playwright install chromium
     PYTHONPATH=. uv run --with playwright python tests/interpreter_handoff_browser_smoke.py
Optional HANDOFF_ARTIFACT_DIR saves screenshots and a browser recording.

Four booth tabs join one booth: interpreters A (active), B (backup) and C
(passive), and a room coordinator. The coordinator sends ``booth:set-active``
for B and every tab is checked against the one-active-publisher invariant.

Stubbed: MediaMTX (WHIP and WHEP), the ingest status endpoint and the
transcription start/stop calls. Everything else is the real app: the booth page,
the booth WebSocket, roles and publish tickets.

The WHIP stub answers the tab's real SDP offer and redeems the Bearer ticket
for the path in the request URL with the portal's own ``redeem_ticket`` and
``still_entitled``, so single-use and path binding are checked for real. It also
keeps each WHIP session open until the tab sends DELETE, and the test fails if a
second session is ever opened while another is still open.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "handoff-smoke-test-encryption-key-32-chars")
os.environ.setdefault("SECRET_KEY", "handoff-smoke-test-jwt-key-for-isolated-tests")

import uvicorn
from playwright.async_api import async_playwright

from fastapi_app import app
from portal.auth import create_participant_token
from portal.booth_identity import booth_id_to_mediamtx_path, make_booth_id
from portal.booth_publish import redeem_ticket, still_entitled
from portal.config import settings
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.globals import booths

TIMEOUT = 15.0

# Records everything the assertions need, without changing what the page does.
INIT_SCRIPT = """
(() => {
  window.__frames = [];
  window.__socks = [];
  window.__pcs = [];
  const NativeWS = window.WebSocket;
  window.WebSocket = class extends NativeWS {
    constructor(url, protocols) {
      super(url, protocols);
      if (!String(url).includes('/ws/booth/')) return;
      window.__socks.push(this);
      const send = this.send.bind(this);
      this.send = (data) => { window.__frames.push({ dir: 'out', data }); return send(data); };
      this.addEventListener('message', (e) => window.__frames.push({ dir: 'in', data: e.data }));
    }
  };
  const NativePC = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends NativePC {
    constructor(...args) { super(...args); window.__pcs.push(this); }
    close() { this.__closed = true; super.close(); }
  };
  const ids = new WeakMap();
  let next = 0;
  const idOf = (o) => { if (!ids.has(o)) ids.set(o, ++next); return ids.get(o); };
  window.__audioEdges = [];
  window.__audioKinds = {};
  const connect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (dest, ...rest) {
    window.__audioKinds[idOf(this)] = this.constructor.name;
    const isDest = typeof AudioDestinationNode !== 'undefined' && dest instanceof AudioDestinationNode;
    window.__audioKinds[idOf(dest)] = isDest ? 'destination' : dest.constructor.name;
    window.__audioEdges.push([idOf(this), idOf(dest)]);
    return connect.call(this, dest, ...rest);
  };
  window.__micTracks = [];
  // Not every frame has navigator.mediaDevices (an about:blank iframe, for one).
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await getUserMedia(constraints);
      stream.getAudioTracks().forEach((track) => window.__micTracks.push(track));
      return stream;
    };
  }
})();
"""

ANSWERER_SCRIPT = """
async (offerSdp) => {
  window.__answerers = window.__answerers || [];
  const pc = new RTCPeerConnection({ iceServers: [] });
  // push() returns the new length, so take the index here, before any await lets another offer append.
  const index = window.__answerers.push(pc) - 1;
  await pc.setRemoteDescription({ type: 'offer', sdp: offerSdp });
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  return { sdp: pc.localDescription.sdp, index };
}
"""

CLOSE_ANSWERER_SCRIPT = "(index) => window.__answerers[index].close()"


def whip_target(url: str) -> tuple[str, str]:
    """The MediaMTX path and the resource id a WHIP request addresses.

    The page posts to ``/<path with / written as %2F>/whip``. The resource URL the stub
    returns, which the page later sends DELETE to, is ``/<path>/whip/<id>``.
    """
    path = unquote(urlparse(url).path.strip("/"))
    head, _, tail = path.partition("/whip")
    return head, tail.lstrip("/")


class Tab:
    """One booth tab: a page plus helpers over its recorded WebSocket frames."""

    def __init__(self, name: str, page) -> None:
        self.name = name
        self.page = page
        self.participant_id: str | None = None
        self.errors: list[str] = []
        self.whip: list[dict] = []
        self.failed_requests: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on(
            "response",
            lambda r: self.failed_requests.append(f"{r.status} {r.url}") if r.status >= 400 else None,
        )

    async def raw(self) -> list[dict]:
        return await self.page.evaluate("window.__frames")

    async def mark(self) -> int:
        return len(await self.raw())

    async def frames(self, direction: str, kind: str, since: int = 0) -> list[dict]:
        found = []
        for frame in (await self.raw())[since:]:
            if frame["dir"] != direction:
                continue
            try:
                data = json.loads(frame["data"])
            except ValueError:
                continue
            if data.get("type") == kind:
                found.append(data)
        return found

    async def send(self, message: dict) -> None:
        await self.page.evaluate("(m) => window.__socks.at(-1).send(JSON.stringify(m))", message)

    async def state(self) -> dict | None:
        """The newest booth state this tab received, from booth:state or booth:joined."""
        latest = None
        for frame in await self.raw():
            if frame["dir"] != "in":
                continue
            try:
                data = json.loads(frame["data"])
            except ValueError:
                continue
            if data.get("type") in {"booth:state", "booth:joined"} and data.get("state"):
                latest = data["state"]
        return latest

    async def live_publishers(self) -> int:
        """Open peer connections that carry a local audio track, i.e. a WHIP publish."""
        return await self.page.evaluate(
            "window.__pcs.filter((pc) => !pc.__closed && pc.getSenders().some((s) => s.track)).length"
        )

    async def mic_capture_state(self) -> dict:
        """Microphone tracks this tab captured: how many, and how many still deliver audio."""
        return await self.page.evaluate(
            """() => ({
              total: window.__micTracks.length,
              delivering: window.__micTracks.filter((t) => t.readyState === 'live' && t.enabled).length,
            })"""
        )

    async def mic_source_count(self) -> int:
        """Microphone source nodes this tab created, so the speaker check is not vacuous."""
        kinds = await self.page.evaluate("window.__audioKinds")
        return sum(1 for kind in kinds.values() if kind == "MediaStreamAudioSourceNode")

    async def mic_reaches_speakers(self) -> bool:
        """True if any microphone source node is wired, directly or not, to AudioContext.destination."""
        edges = await self.page.evaluate("window.__audioEdges")
        kinds = await self.page.evaluate("window.__audioKinds")
        targets: dict[int, set[int]] = {}
        for src, dst in edges:
            targets.setdefault(src, set()).add(dst)
        pending = [int(i) for i, kind in kinds.items() if kind == "MediaStreamAudioSourceNode"]
        seen = set(pending)
        while pending:
            for nxt in targets.get(pending.pop(), ()):
                if kinds.get(str(nxt)) == "destination":
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    pending.append(nxt)
        return False


async def wait_for(description: str, check, timeout: float = TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        result = await check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for: {description}")
        await asyncio.sleep(0.05)


def comparable(state: dict) -> dict:
    """The part of a booth state that every tab must agree on."""
    return {
        "active": state.get("active_interpreter_id"),
        "unlocked": state.get("broadcast_unlocked"),
        "handoff": state.get("handoff_state"),
        "participants": sorted(
            (p["participant_id"], p["role"], bool(p["mic_active"]), bool(p["ingest_connected"]))
            for p in state.get("participants", [])
        ),
    }


def assert_one_active_publisher(state: dict, who: str) -> None:
    """Invariant 1: one active publisher per language channel, and only the active one may be live."""
    active = state.get("active_interpreter_id")
    live = [p["participant_id"] for p in state["participants"] if p["ingest_connected"] or p["mic_active"]]
    assert len(live) <= 1, f"{who}: more than one participant is live: {live}"
    assert not live or live == [active], f"{who}: live participant {live} is not the active one ({active})"


async def main() -> None:
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        event = await create_event(s, slug="handoff-demo", display_name="Handoff rehearsal")
        room = await create_room(s, event_id=event.id, display_name="Main stage")
        db_booth = await create_booth(
            s, event_id=event.id, room_id=room.id, language_code="en", language_name="English"
        )
        event_slug, room_id, booth_pk = event.slug, room.id, db_booth.id
    booth_id = make_booth_id(event_slug, room_id, "en")
    media_path = booth_id_to_mediamtx_path(booth_id)
    booth_url_path = f"/interpreter/{event_slug}/{room_id}/en"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    artifacts = Path(os.environ["HANDOFF_ARTIFACT_DIR"]) if os.environ.get("HANDOFF_ARTIFACT_DIR") else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    # The stub answers on plain HTTP, so pin the base: an HTTPS MEDIAMTX_WHIP_BASE in the environment
    # would otherwise send the tabs to a real service.
    previous_whip_base = settings.mediamtx_whip_base
    settings.mediamtx_whip_base = "http://localhost:8889"
    whip_host = urlparse(settings.mediamtx_whip_base).netloc
    # Production sets a strong BOOTH_ACCESS_TOKEN and the booth link carries it as ?token=.
    # Tabs authenticate their state fetch with it and take their role from the cookie.
    previous_access_token = settings.booth_access_token
    access_token = "handoff-smoke-booth-access-token-0123456789abcdef"
    settings.booth_access_token = access_token
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
            )
            options = {"viewport": {"width": 1440, "height": 900}, "permissions": ["microphone"]}
            if artifacts:
                options["record_video_dir"] = str(artifacts)

            # A separate page answers the tabs' real WHIP offers, standing in for MediaMTX.
            answerer_context = await browser.new_context()
            answerer = await answerer_context.new_page()
            await answerer.goto("about:blank")

            tabs: dict[str, Tab] = {}
            contexts = []
            # Every WHIP session the stub has opened, by resource id. A session stays open until its tab
            # sends DELETE, like a MediaMTX publisher would. ``overlaps`` records every session that was
            # opened while another one was still open.
            sessions: dict[str, dict] = {}
            overlaps: list[dict] = []

            async def open_tab(name: str, role: str) -> Tab:
                context = await browser.new_context(**options)
                contexts.append(context)
                await context.add_cookies(
                    [
                        {
                            "name": "session_token",
                            "value": create_participant_token(
                                booth_id=booth_pk,
                                role=role,
                                event_slug=event_slug,
                                room_id=room_id,
                                language_code="en",
                            ),
                            "url": origin,
                        }
                    ]
                )
                await context.add_init_script(INIT_SCRIPT)
                page = await context.new_page()
                tab = Tab(name, page)

                async def handle_other(route):
                    if urlparse(route.request.url).netloc == urlparse(origin).netloc:
                        await route.fallback()
                    else:
                        # Jitsi and fonts are not part of this flow, and nothing may reach a real MediaMTX.
                        await route.abort()

                async def handle_status(route):
                    await route.fulfill(json={"reachable": True})

                async def handle_transcription(route):
                    await route.fulfill(json={})

                async def handle_whip(route):
                    request = route.request
                    cors = {
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "authorization, content-type",
                        "Access-Control-Allow-Methods": "POST, DELETE, OPTIONS",
                        "Access-Control-Expose-Headers": "Location",
                    }
                    if request.url.endswith("/whep"):  # the booth's monitoring feed; there is no media here
                        await route.fulfill(status=404, headers=cors)
                        return
                    if request.method == "OPTIONS":  # CORS preflight
                        await route.fulfill(status=204, headers=cors)
                        return
                    requested_path, resource_id = whip_target(request.url)
                    if request.method == "DELETE":  # the tab ends its publish: close MediaMTX's side too
                        session = sessions.get(resource_id)
                        if session is not None and session["open"]:
                            session["open"] = False
                            await answerer.evaluate(CLOSE_ANSWERER_SCRIPT, session["index"])
                        await route.fulfill(status=200, headers=cors)
                        return
                    secret = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                    # Redeemed for the path the tab actually posted to, not for the one we expect.
                    ticket = redeem_ticket(secret, requested_path)
                    entry = {
                        "tab": tab.name,
                        "path": requested_path,
                        "redeemed": ticket is not None,
                        "participant_id": ticket.participant_id if ticket else None,
                        "entitled": bool(ticket and still_entitled(ticket)),
                        "secret": secret,
                        "url": request.url,
                        "content_type": request.headers.get("content-type"),
                    }
                    tab.whip.append(entry)
                    if not entry["entitled"]:
                        await route.fulfill(status=401, headers=cors, body="ticket refused")
                        return
                    still_open = [s["tab"] for s in sessions.values() if s["open"]]
                    if still_open:
                        overlaps.append({"opened_by": tab.name, "still_open": still_open})
                    answer = await answerer.evaluate(ANSWERER_SCRIPT, request.post_data)
                    resource_id = str(uuid.uuid4())
                    sessions[resource_id] = {"tab": tab.name, "index": answer["index"], "open": True}
                    await route.fulfill(
                        status=201,
                        headers={
                            **cors,
                            "Content-Type": "application/sdp",
                            "Location": f"/{requested_path}/whip/{resource_id}",
                        },
                        body=answer["sdp"],
                    )

                await context.route("**/*", handle_other)
                await context.route("**/api/interpreter/status/**", handle_status)
                await context.route("**/transcription/**", handle_transcription)
                await context.route(lambda url: urlparse(url).netloc == whip_host, handle_whip)
                await page.goto(f"{origin}{booth_url_path}?token={access_token}")

                async def joined():
                    found = await tab.frames("in", "booth:joined")
                    return found[-1] if found else None

                try:
                    joined_frame = await wait_for(f"{name} joined the booth", joined)
                except AssertionError:
                    banner = (
                        await page.locator("#error-banner").inner_text()
                        if await page.locator("#error-banner").count()
                        else ""
                    )
                    raise AssertionError(
                        f"{name} never joined: url={page.url} banner={banner!r} errors={tab.errors} failed={tab.failed_requests} "
                        f"frames={await tab.raw()}"
                    ) from None
                tab.participant_id = joined_frame["participant_id"]
                tabs[name] = tab
                return tab

            # Join order decides the active slot: the first interpreter in is active.
            a = await open_tab("A", "interpreter")
            b = await open_tab("B", "interpreter")
            c = await open_tab("C", "interpreter")
            k = await open_tab("K", "room_coordinator")
            everyone = [a, b, c, k]

            async def everyone_sees(count: int):
                states = [await t.state() for t in everyone]
                return all(s and len(s["participants"]) == count for s in states)

            await wait_for("all four tabs see four participants", lambda: everyone_sees(4))
            for t in everyone:
                state = await t.state()
                assert state["active_interpreter_id"] == a.participant_id, f"{t.name}: A should start active"

            # Mission Control turns the booth on; the active tab then publishes by itself.
            await k.send({"type": "booth:set-broadcast-unlocked", "unlocked": True})

            async def a_is_live():
                state = await a.state()
                me = next((p for p in state["participants"] if p["participant_id"] == a.participant_id), None)
                return bool(me and me["ingest_connected"] and me["mic_active"])

            await wait_for("A publishes after the booth is unlocked", a_is_live)
            assert await a.live_publishers() == 1
            first_ticket = a.whip[0]
            assert first_ticket["redeemed"] and first_ticket["entitled"], first_ticket
            assert first_ticket["participant_id"] == a.participant_id
            assert first_ticket["path"] == media_path, f"A posted to the wrong WHIP path: {first_ticket['url']}"
            assert redeem_ticket(first_ticket["secret"], media_path) is None, "a publish ticket must be single-use"

            # The role comes from the server-side session, not from what the client claims.
            # A join that asks for a higher role than the session grants is refused.
            forged_mark = await c.mark()
            for claimed in ("room_coordinator", "admin"):
                await c.send(
                    {
                        "type": "booth:join",
                        "role": claimed,
                        "participant_id": c.participant_id,
                        "display_name": "Forged",
                        "event_slug": event_slug,
                        "room_id": room_id,
                    }
                )

            async def forged_joins_refused():
                errors = await c.frames("in", "booth:error", forged_mark)
                return len(errors) >= 2 and all("does not permit joining as" in e["message"] for e in errors[:2])

            await wait_for("forged-role joins are refused", forged_joins_refused)
            state_c = await c.state()
            me_c = next(p for p in state_c["participants"] if p["participant_id"] == c.participant_id)
            assert me_c["role"] == "interpreter", f"a forged join changed C's role: {me_c}"

            # Interpreters who are not coordinators cannot take or hand out the slot, whatever role they claim.
            before = {t.name: await t.mark() for t in everyone}
            await c.send({"type": "booth:set-active", "target_id": c.participant_id})
            await c.send({"type": "booth:set-active", "target_id": c.participant_id, "role": "room_coordinator"})
            await b.send({"type": "booth:set-active", "target_id": b.participant_id, "role": "admin"})
            await c.send({"type": "booth:request-publish-ticket"})

            async def refusals():
                errors_c = await c.frames("in", "booth:error", before["C"])
                errors_b = await b.frames("in", "booth:error", before["B"])
                tickets_c = await c.frames("in", "booth:publish-ticket", before["C"])
                return len(errors_c) >= 2 and len(errors_b) >= 1 and tickets_c

            await wait_for("passive interpreters are refused", refusals)
            ticket_reply = (await c.frames("in", "booth:publish-ticket", before["C"]))[-1]
            assert "ticket" not in ticket_reply and ticket_reply.get("error"), "a passive tab must not get a ticket"
            assert booths.get_booth_sync(booth_id).active_interpreter_id == a.participant_id
            assert (await a.live_publishers()) == 1

            # The switch itself.
            marks = {t.name: await t.mark() for t in everyone}
            await k.send({"type": "booth:set-active", "target_id": b.participant_id})

            async def switched():
                for t in everyone:
                    if ((await t.state()) or {}).get("active_interpreter_id") != b.participant_id:
                        return False
                return True

            await wait_for("every tab sees B as active", switched)

            async def a_released():
                updates = await a.frames("out", "booth:update-state", marks["A"])
                return any(u.get("mic_active") is False and u.get("ingest_connected") is False for u in updates)

            await wait_for("A reports mic and ingest off", a_released)
            await wait_for("A closes its publish connection", lambda: _is_zero(a.live_publishers()))

            async def b_publishes():
                got = await b.frames("in", "booth:publish-ticket", marks["B"])
                return bool(got) and bool(got[-1].get("ticket"))

            await wait_for("B receives a publish ticket", b_publishes)
            assert await b.frames("out", "booth:request-publish-ticket", marks["B"])

            async def b_whip_received():
                # The tab adds its audio track before it asks for a ticket and posts the offer, so a
                # live publisher alone does not mean the stub has seen the request yet.
                return bool(b.whip) and await _is_one(b.live_publishers())

            await wait_for("B's WHIP request reaches the stub", b_whip_received)
            b_ticket = b.whip[-1]
            assert b_ticket["redeemed"] and b_ticket["entitled"], b_ticket
            assert b_ticket["participant_id"] == b.participant_id, "the ticket must belong to the new active tab"
            assert b_ticket["path"] == media_path, f"B posted to the wrong WHIP path: {b_ticket['url']}"
            assert redeem_ticket(b_ticket["secret"], media_path) is None
            assert len(a.whip) == 1, f"A must not publish again after the switch: {a.whip}"

            # A ticket is bound to the booth's path: a fresh one is refused, and spent, for any other path.
            fresh_mark = await b.mark()
            await b.send({"type": "booth:request-publish-ticket"})

            async def fresh_ticket():
                got = await b.frames("in", "booth:publish-ticket", fresh_mark)
                return got[-1].get("ticket") if got else None

            fresh = await wait_for("B gets a fresh ticket", fresh_ticket)
            assert redeem_ticket(fresh, "another-event/1/en") is None, "a ticket must not work for another path"
            assert redeem_ticket(fresh, media_path) is None, "a ticket shown for the wrong path is spent"

            async def b_is_live_everywhere():
                for t in everyone:
                    state = await t.state()
                    me = next((p for p in state["participants"] if p["participant_id"] == b.participant_id), None)
                    if not (me and me["ingest_connected"] and me["mic_active"]):
                        return False
                return True

            await wait_for("every tab sees B live", b_is_live_everywhere)

            # Exactly one tab shows itself as active, and every tab holds the same booth state.
            tiles = {}
            for t in everyone:
                tiles[t.name] = await t.page.locator(".participant-tile.active strong").all_inner_texts()
            assert tiles["B"] == ["Interpreter (You)"] or any("(You)" in x for x in tiles["B"]), tiles
            for name in ("A", "C", "K"):
                assert not any("(You)" in x for x in tiles[name]), f"{name} still shows itself active: {tiles}"
            for t in everyone:
                assert len(tiles[t.name]) == 1, f"{t.name} shows {len(tiles[t.name])} active tiles"
            assert sum([await t.live_publishers() for t in everyone]) == 1
            # MediaMTX's side: A's session ended, B's is the only one open, and the two never overlapped.
            assert not overlaps, f"a WHIP session was opened while another was still open: {overlaps}"
            open_sessions = [s["tab"] for s in sessions.values() if s["open"]]
            assert open_sessions == ["B"], f"open WHIP sessions: {open_sessions}"
            # A let go of the microphone (its tracks are muted or ended); B is capturing.
            a_mic = await a.mic_capture_state()
            assert a_mic["total"] > 0 and a_mic["delivering"] == 0, f"A still captures audio: {a_mic}"
            assert (await b.mic_capture_state())["delivering"] > 0, "B should be capturing audio"
            states = [comparable(await t.state()) for t in everyone]
            assert all(s == states[0] for s in states), f"tabs disagree: {states}"
            assert booths.get_booth_sync(booth_id).active_interpreter_id == b.participant_id

            # No state any tab ever received had two live publishers, and no mic reached the speakers.
            # The speaker check only means something if the tabs that held the mic built a mic audio graph.
            for publisher in (a, b):
                assert await publisher.mic_source_count() > 0, f"{publisher.name}: no mic audio graph was observed"
            for t in everyone:
                for frame in await t.raw():
                    if frame["dir"] != "in":
                        continue
                    data = json.loads(frame["data"])
                    if data.get("type") in {"booth:state", "booth:joined"} and data.get("state"):
                        assert_one_active_publisher(data["state"], t.name)
                assert not await t.mic_reaches_speakers(), (
                    f"{t.name}: microphone audio reached AudioContext.destination"
                )
                assert not t.errors, f"{t.name}: {t.errors}"
            if artifacts:
                for t in everyone:
                    await t.page.screenshot(path=str(artifacts / f"handoff-{t.name}.png"), full_page=True)
            for context in contexts:
                await context.close()
            await answerer_context.close()
            await browser.close()
        print(
            "PASS: set-active handoff, one live publisher, old tab released the mic, "
            "single-use ticket for the new active tab, passive tabs refused, tabs converged, mic never routed to speakers"
        )
    finally:
        settings.mediamtx_whip_base = previous_whip_base
        settings.booth_access_token = previous_access_token
        server.should_exit = True
        await task
        sock.close()
        await dispose()


async def _is_zero(awaitable) -> bool:
    return await awaitable == 0


async def _is_one(awaitable) -> bool:
    return await awaitable == 1


if __name__ == "__main__":
    asyncio.run(main())
