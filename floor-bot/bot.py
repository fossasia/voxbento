from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess  # nosec B404
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("floor-bot")
# Authenticated Jitsi deployments carry credentials in the room URL and XMPP traffic.
# Browser console lines use query (jwt=...), JSON ("jwt":"...") and prose (token: ...) forms.
SECRET_VALUE = re.compile(
    r"""(?i)(["']?\b(?:jwt|(?:access_|refresh_)?token|password)\b["']?\s*[:=]\s*["']?)[^&\s#"',;}]+"""
)


class SubprocessManager:
    def __init__(self):
        self.rooms: Dict[str, subprocess.Popen] = {}
        # Lifecycle stage per event_slug, updated from subprocess stdout markers.
        # Possible values: launching, joining, in_meeting, leaving, stopping, stopped, dead.
        self.room_states: Dict[str, str] = {}
        # Last failure code from the subprocess (join_failed, not_jitsi, redirected, left_meeting, audio_failed).
        self.room_failures: Dict[str, str] = {}
        self.room_meta: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def start_room(self, event_slug: str, room_id: int, jitsi_url: str, mediamtx_rtsp_base: str):
        process_key = f"{event_slug}-{room_id}"
        with self.lock:
            if process_key in self.rooms:
                self.stop_room_locked(process_key)

            logger.info(f"Starting subprocess for event: {event_slug}, room: {room_id}")
            env = os.environ.copy()
            env["BOT_EVENT_SLUG"] = event_slug
            env["BOT_ROOM_ID"] = str(room_id)
            env["BOT_JITSI_URL"] = jitsi_url
            env["BOT_MEDIAMTX_RTSP_BASE"] = mediamtx_rtsp_base

            cmd = [
                "python",
                "-u",
                "-c",
                """
import asyncio
import os
import signal
import subprocess
from playwright.async_api import async_playwright

class AgentFailure(Exception):
    pass

JOINED_JS = (
    "() => typeof APP !== 'undefined' && !!APP.conference"
    " && typeof APP.conference.isJoined === 'function' && APP.conference.isJoined()"
)
IS_JITSI_JS = "() => typeof APP !== 'undefined' && !!APP.conference"

def same_meeting(requested, final):
    # Whether a followed redirect still points at the requested room on the same Jitsi.
    # Host and room must match. The port may change only with an http-to-https upgrade;
    # a downgrade to http is refused because it would expose the room credential.
    import urllib.parse
    req, got = urllib.parse.urlsplit(requested), urllib.parse.urlsplit(final)
    def room(parts):
        return (parts.hostname or "").casefold(), parts.path.strip("/").casefold()
    if room(req) != room(got):
        return False
    if req.scheme == got.scheme:
        return req.port == got.port
    return req.scheme == "http" and got.scheme == "https"

async def evaluate(page, script):
    try:
        return bool(await page.evaluate(script))
    except Exception:
        return False

async def run_capture():
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def signal_handler():
        print("Received terminate signal, shutting down...")
        if main_task:
            main_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    loop.add_signal_handler(signal.SIGINT, signal_handler)

    event_slug = os.environ.get("BOT_EVENT_SLUG")
    room_id = os.environ.get("BOT_ROOM_ID")
    jitsi_url = os.environ.get("BOT_JITSI_URL")
    mediamtx_rtsp_base = os.environ.get("BOT_MEDIAMTX_RTSP_BASE")

    print("BOT_STAGE:launching", flush=True)

    pulse_socket = f"/tmp/pulse-{event_slug}-{room_id}.sock"
    sink_name = f"sink_{event_slug}_{room_id}"

    pulse_dir = None
    pulse_proc = None
    pw = None
    browser = None
    ffmpeg_proc = None
    user_data_dir = None
    exit_code = 0

    try:
        import tempfile
        pulse_dir = tempfile.mkdtemp(prefix="pulse-dir-", dir="/tmp")

        subprocess.run(["pkill", "-f", f"ffmpeg.*{event_slug}.*{room_id}"], stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", f"pulseaudio.*{event_slug}.*{room_id}"], stderr=subprocess.DEVNULL)
        if os.path.exists(pulse_socket):
            try:
                os.remove(pulse_socket)
            except OSError:
                pass

        pulse_proc = subprocess.Popen([
            "pulseaudio",
            "--daemonize=no",
            "--exit-idle-time=-1",
            "--disallow-exit",
            "-n",
            "-L", f"module-native-protocol-unix auth-anonymous=1 socket={pulse_socket}",
            "-L", f"module-null-sink sink_name={sink_name}",
            "-L", "module-always-sink"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ.copy(), "PULSE_RUNTIME_PATH": pulse_dir, "PULSE_STATE_PATH": pulse_dir})

        await asyncio.sleep(2)

        env = os.environ.copy()
        env["PULSE_SERVER"] = f"unix:{pulse_socket}"

        pw = await async_playwright().start()

        user_data_dir = tempfile.mkdtemp(prefix="chromium-data-", dir="/tmp")

        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=True,
            env=env,
            ignore_default_args=["--mute-audio"],
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--use-file-for-fake-audio-capture=/dev/null",
                "--disable-gesture-requirement-for-media-playback",
                "--ignore-certificate-errors",
                "--unsafely-treat-insecure-origin-as-secure=http://jitsi-web,http://jitsi-web:80,https://jitsi-web",
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ],
            permissions=["microphone", "camera"],
            ignore_https_errors=True
        )

        browser = context  # Aliased for the existing cleanup logic

        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        page.on("console", lambda m: print(f"Browser: {m.text}"))
        page.on("pageerror", lambda err: print(f"Browser Error: {err}"))

        import urllib.parse
        display_name = urllib.parse.quote('"VoxBento FloorBot"')

        join_url = (
            f"{jitsi_url}"
            f"#config.startWithAudioMuted=false"
            f"&config.startWithVideoMuted=true"
            f"&config.prejoinPageEnabled=false"
            f"&config.disableDeepLinking=true"
            f"&config.p2p.enabled=false"
            f"&config.requireDisplayName=false"
            f"&userInfo.displayName={display_name}"
        )

        print("BOT_STAGE:joining", flush=True)
        # Never print join_url: it can carry meeting credentials.
        print("Joining meeting", flush=True)
        try:
            await page.goto(join_url, timeout=30000)
        except Exception:
            raise AgentFailure("join_failed")
        # goto follows HTTP redirects. Refuse one that leaves the requested room, so the
        # requested-URL fingerprint in /status names the room actually joined.
        if not same_meeting(jitsi_url, page.url):
            raise AgentFailure("redirected")

        try:
            await page.click("div[aria-label='Join meeting']", timeout=5000)
            print("Clicked Join Meeting dialog")
        except Exception:
            pass

        deadline = loop.time() + 30
        while not await evaluate(page, JOINED_JS):
            if loop.time() > deadline:
                raise AgentFailure("join_failed" if await evaluate(page, IS_JITSI_JS) else "not_jitsi")
            await asyncio.sleep(1)

        # Wait a bit for Jitsi audio to start flowing into PulseAudio
        await asyncio.sleep(3)

        print("BOT_STAGE:in_meeting", flush=True)

        async def stream_audio():
            nonlocal ffmpeg_proc
            # Retry loop: ffmpeg can exit if RTSP or PulseAudio isn't ready yet
            max_retries = 5
            retry_delay = 3
            for attempt in range(1, max_retries + 1):
                print(f"Starting ffmpeg (attempt {attempt}/{max_retries})...", flush=True)
                ffmpeg_proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y",
                    "-f", "pulse",
                    "-i", f"{sink_name}.monitor",
                    "-ac", "1",
                    "-ar", "16000",
                    "-c:a", "libopus",
                    "-b:a", "32k",
                    "-vbr", "on",
                    "-compression_level", "10",
                    "-application", "lowdelay",
                    "-f", "rtsp",
                    "-rtsp_transport", "tcp",
                    f"{mediamtx_rtsp_base}/{event_slug}/{room_id}/floor",
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Read stderr in background so we can log it
                async def _drain_stderr(proc):
                    while True:
                        line = await proc.stderr.readline()
                        if not line:
                            break
                        text = line.decode(errors="replace").rstrip()
                        if text:
                            print(f"ffmpeg: {text}", flush=True)

                stderr_task = asyncio.create_task(_drain_stderr(ffmpeg_proc))

                await ffmpeg_proc.wait()
                await stderr_task

                rc = ffmpeg_proc.returncode
                print(f"ffmpeg exited with code {rc}", flush=True)

                if rc == 0:
                    # A publisher never ends on its own; the media server closed the session.
                    print("ffmpeg exited cleanly", flush=True)
                    raise AgentFailure("audio_failed")

                if attempt < max_retries:
                    print(f"Retrying ffmpeg in {retry_delay}s...", flush=True)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 15)
                else:
                    print("ffmpeg failed after all retries", flush=True)
                    raise AgentFailure("audio_failed")

        async def watch_membership():
            misses = 0
            while True:
                await asyncio.sleep(5)
                misses = 0 if await evaluate(page, JOINED_JS) else misses + 1
                if misses >= 3:
                    raise AgentFailure("left_meeting")

        tasks = [asyncio.create_task(stream_audio()), asyncio.create_task(watch_membership())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()

    except asyncio.CancelledError:
        print("run_capture cancelled")
    except AgentFailure as failure:
        print(f"BOT_FAILURE:{failure}", flush=True)
        exit_code = 3
    finally:
        print("BOT_STAGE:leaving", flush=True)
        if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
            ffmpeg_proc.terminate()
            try:
                await asyncio.wait_for(ffmpeg_proc.wait(), timeout=3)
            except Exception:
                try:
                    ffmpeg_proc.kill()
                except Exception:
                    pass
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=4)
            except Exception:
                pass
        if pw is not None:
            await pw.stop()
        if pulse_proc is not None and pulse_proc.poll() is None:
            pulse_proc.terminate()
        if os.path.exists(pulse_socket):
            try:
                os.remove(pulse_socket)
            except OSError:
                pass

        import shutil
        if user_data_dir and os.path.exists(user_data_dir):
            try:
                shutil.rmtree(user_data_dir)
            except OSError:
                pass
        if pulse_dir and os.path.exists(pulse_dir):
            try:
                shutil.rmtree(pulse_dir)
            except OSError:
                pass

    return exit_code

raise SystemExit(asyncio.run(run_capture()))
""",
            ]

            proc = subprocess.Popen(  # nosec B603
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env
            )

            def log_stream(stream, prefix):
                for line in iter(stream.readline, ""):
                    text = SECRET_VALUE.sub(r"\1[redacted]", line.strip())
                    logger.info(f"{prefix}: {text}")
                    if "BOT_STAGE:" in text:
                        self.room_states[process_key] = text.split("BOT_STAGE:", 1)[1].strip()
                    elif "BOT_FAILURE:" in text:
                        self.room_failures[process_key] = text.split("BOT_FAILURE:", 1)[1].strip()

            threading.Thread(target=log_stream, args=(proc.stdout, f"bot[{process_key}] stdout"), daemon=True).start()
            threading.Thread(target=log_stream, args=(proc.stderr, f"bot[{process_key}] stderr"), daemon=True).start()

            self.room_states[process_key] = "launching"
            self.room_failures.pop(process_key, None)
            self.room_meta[process_key] = {
                "event_slug": event_slug,
                "room_id": room_id,
                # Lets the portal detect a stale meeting without the URL (which may hold a jwt) leaving the bot.
                "url_fingerprint": hashlib.sha256(jitsi_url.encode("utf-8")).hexdigest()[:16],
            }
            self.rooms[process_key] = proc

    def stop_room_locked(self, process_key: str):
        if process_key in self.rooms:
            logger.info(f"Terminating subprocess for {process_key}")
            self.room_states[process_key] = "stopping"
            proc = self.rooms[process_key]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Graceful stop timed out for {process_key}; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.error(f"Subprocess for {process_key} did not exit after SIGKILL")
            del self.rooms[process_key]
            self.room_states[process_key] = "stopped"
            self.room_failures.pop(process_key, None)
            self.room_meta.pop(process_key, None)

    def stop_room(self, process_key: str):
        with self.lock:
            self.stop_room_locked(process_key)


manager = SubprocessManager()


class RequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            req = json.loads(post_data.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        if self.path == "/start":
            event_slug = req.get("event_slug")
            room_id = req.get("room_id")
            jitsi_url = req.get("jitsi_url")
            mediamtx_rtsp_base = req.get("mediamtx_rtsp_base")
            if not all([event_slug, room_id is not None, jitsi_url, mediamtx_rtsp_base]):
                self.send_error(400, "Missing parameters")
                return
            manager.start_room(event_slug, room_id, jitsi_url, mediamtx_rtsp_base)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started", "process_key": f"{event_slug}-{room_id}"}).encode())

        elif self.path == "/stop":
            event_slug = req.get("event_slug")
            room_id = req.get("room_id")
            if not event_slug or room_id is None:
                self.send_error(400, "Missing event_slug or room_id")
                return
            manager.stop_room(f"{event_slug}-{room_id}")
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "stopped", "event_slug": event_slug}).encode())
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        if self.path == "/status":
            status = {}
            with manager.lock:
                for process_key, proc in manager.rooms.items():
                    stage = manager.room_states.get(process_key, "unknown")
                    meta = manager.room_meta.get(process_key, {})
                    if proc.poll() is not None:
                        status[process_key] = {
                            **meta,
                            "state": "dead",
                            "stage": "dead",
                            "exit_code": proc.returncode,
                            "failure": manager.room_failures.get(process_key),
                        }
                    else:
                        status[process_key] = {**meta, "state": "healthy", "stage": stage, "pid": proc.pid}
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"active_rooms": status}).encode())
        else:
            self.send_error(404, "Not Found")


if __name__ == "__main__":
    server_address = ("0.0.0.0", 8080)  # nosec B104
    httpd = HTTPServer(server_address, RequestHandler)
    logger.info("Starting floor-bot on port 8080...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p_key in list(manager.rooms.keys()):
            manager.stop_room(p_key)
        httpd.server_close()
