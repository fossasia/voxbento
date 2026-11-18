"""Post final captions to a YouTube broadcast's native caption track (#1087, plan 006).

Caption URLs are capability URLs: they live in memory only and are never logged or returned.
`offer` is called from the caption hot path, so it never awaits and never raises.

YouTube rejects a caption whose timestamp is more than 60 s from its own clock, so the sender keeps
a per-room offset between this host's clock and YouTube's, learned from the `Date` header (or the
`local=` time in a skew rejection) of YouTube's responses, and stamps captions in YouTube's time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from portal.tasks import track_task

logger = logging.getLogger(__name__)

MAX_QUEUE = 50
CHUNK_CHARS = 80
CHUNK_SPACING_MS = 1200
# Once a caption is older than this and newer ones are waiting, its remaining chunks are dropped.
MAX_CAPTION_AGE_MS = 15_000
POST_TIMEOUT_S = 5.0
RETRY_DELAY_S = 1.0
# YouTube rejects a timestamp more than 60 s from its own clock with HTTP 400 (plans/002-results.md, step 4).
MAX_SKEW_MS = 55_000
# A clock offset that moves by more than this means the host clock and YouTube's disagreed.
CLOCK_SHIFT_MS = 5_000
REJECTED_REASON = "YouTube rejected the caption (HTTP {status}). Check the broadcast's caption settings."
SKEW_REASON = "YouTube rejected the caption timestamp (more than 60 s from its clock). Check this server's clock."
UNREACHABLE_REASON = "Could not reach YouTube to post the caption."
# YouTube's skew rejection looks like "Timestamp skew from local=2026-11-05T11:26:10.685 larger than 60 sec".
_LOCAL_TIME = re.compile(r"local=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def _secret_forms(url: str) -> frozenset[str]:
    """Every spelling of a caption URL that a log line could contain.

    httpx logs the URL it normalised (lower-case scheme and host, default port dropped), which can differ
    from the string that was stored, so the stored string, the normalised one and the bare `cid` all count.
    """
    forms = {url}
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return frozenset(forms)
    forms.add(str(parsed))
    forms.update(value for value in parsed.params.get_list("cid") if value)
    return frozenset(forms)


@dataclass
class CaptionTarget:
    language: str  # "floor" or a target language code
    url: str = field(repr=False)  # capability URL: never log or return it
    seq: int = 0
    # What the httpx log filter must hide for this URL; worked out once, when the target is created.
    secrets: frozenset[str] = field(init=False, repr=False, compare=False, default=frozenset())

    def __post_init__(self) -> None:
        self.secrets = _secret_forms(self.url)


@dataclass
class CaptionHealth:
    state: str = "off"  # off | waiting | posting | failing
    reason: str = ""
    last_ok_at: float | None = None


@dataclass
class PostReply:
    """What came back from one caption POST. `status` is None after a network error."""

    status: int | None
    text: str = ""
    date: str | None = None


targets: dict[int, CaptionTarget] = {}
health: dict[int, CaptionHealth] = {}
# Milliseconds to add to this host's clock to get YouTube's. Absent until a response has been seen.
clock_offset_ms: dict[int, int] = {}
_queues: dict[int, asyncio.Queue] = {}
_tasks: dict[int, asyncio.Task] = {}
# The last `seq` used per caption URL, so deactivating and activating the same stream does not restart at 1.
_last_seq: dict[str, int] = {}
MAX_REMEMBERED_STREAMS = 100

# Tests replace this to avoid real network access.
_transport: httpx.AsyncBaseTransport | None = None


class _HideCaptionUrls(logging.Filter):
    """httpx logs every request URL at INFO; a caption URL is a secret, so drop those records.

    The strings to look for are worked out once per target (``CaptionTarget.secrets``), not per log record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # A copy, so a target added or removed while a record is being filtered cannot break the iteration.
        return not any(secret in message for target in list(targets.values()) for secret in target.secrets)


logging.getLogger("httpx").addFilter(_HideCaptionUrls())


def format_timestamp(ms: int) -> str:
    """UTC with millisecond precision, the format YouTube accepted in plans/002-results.md step 4."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def body(ms: int, text: str) -> str:
    """A timestamp line, then the caption text (the working template from plans/002-results.md)."""
    return f"{format_timestamp(ms)}\n{text}\n"


def clamp_ms(ms: int, now_ms: int) -> int:
    """Keep a caption timestamp inside YouTube's accepted window around its clock."""
    return max(now_ms - MAX_SKEW_MS, min(now_ms + MAX_SKEW_MS, ms))


def host_now_ms() -> int:
    """This host's clock in milliseconds."""
    return int(time.time() * 1000)


def youtube_now_ms(room_id: int) -> int:
    """YouTube's clock as best known: this host's clock plus the learned offset (zero until a response is seen)."""
    return host_now_ms() + clock_offset_ms.get(room_id, 0)


def reply_time_ms(reply: PostReply) -> int | None:
    """YouTube's clock from a response: its `Date` header, else the `local=` time in a skew rejection."""
    if reply.date:
        try:
            return int(parsedate_to_datetime(reply.date).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    found = _LOCAL_TIME.search(reply.text)
    if found:
        try:
            return int(datetime.fromisoformat(found.group(1)).replace(tzinfo=UTC).timestamp() * 1000)
        except ValueError:
            return None
    return None


def learn_clock(room_id: int, reply: PostReply) -> int | None:
    """Update the room's clock offset from a response.

    Returns how far the offset moved in milliseconds, or None when the response carried no usable time.
    """
    server_ms = reply_time_ms(reply)
    if server_ms is None:
        return None
    learned = server_ms - host_now_ms()
    shift = learned - clock_offset_ms.get(room_id, 0)
    clock_offset_ms[room_id] = learned
    return shift


def _https(url: str) -> str:
    """The API returns an http:// caption URL; its cid is the stream key, so post over https."""
    return "https://" + url[len("http://") :] if url[: len("http://")].lower() == "http://" else url


def unusable_target_reason(language: str, floor_translation_enabled: bool, enabled_languages: set[str]) -> str | None:
    """Why a caption language cannot produce captions for a room, or None when it can.

    The floor's own language is always available. A translated language needs floor translation to be on
    and that language to be enabled, otherwise nothing would ever be posted to the track.
    """
    if language == "floor":
        return None
    if not floor_translation_enabled:
        return "Floor translation is off for this room, so no translated captions can be posted."
    if language not in enabled_languages:
        return "That language is not enabled for this room's translation."
    return None


def chunks(text: str) -> list[str]:
    """Collapse whitespace and split into pieces of at most CHUNK_CHARS at word boundaries."""
    out: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > CHUNK_CHARS:  # a single unbroken word longer than a line
            if line:
                out.append(line)
                line = ""
            out.append(word[:CHUNK_CHARS])
            word = word[CHUNK_CHARS:]
        candidate = f"{line} {word}" if line else word
        if len(candidate) <= CHUNK_CHARS:
            line = candidate
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


def _with_seq(url: str, seq: int) -> str:
    return f"{url}{'&' if urlsplit(url).query else '?'}seq={seq}"


def wants(room_id: int, language: str) -> bool:
    """Whether captions in this language are being posted to YouTube for the room.

    The translation worker uses it to keep translating a language nobody is listening to.
    """
    target = targets.get(room_id)
    return target is not None and target.language == language


def offer(room_id: int, language: str, text: str, received_at_ms: int | None = None) -> None:
    """Non-blocking. Called from the caption hot path; never awaits network I/O."""
    try:
        target = targets.get(room_id)
        if target is None or target.language != language or not text.strip():
            return
        queue = _queues.setdefault(room_id, asyncio.Queue(maxsize=MAX_QUEUE))
        if queue.full():
            queue.get_nowait()  # drop the oldest; captions are live, stale ones are worthless
        queue.put_nowait((received_at_ms or host_now_ms(), text))
        task = _tasks.get(room_id)
        if task is None or task.done():
            task = asyncio.create_task(_sender(room_id))
            _tasks[room_id] = task
            track_task(task)
    except (RuntimeError, asyncio.QueueEmpty, asyncio.QueueFull):
        # The caption hot path must never fail because YouTube posting did (RuntimeError: no running loop).
        logger.warning("youtube_captions offer_failed room_id=%s", room_id)


async def activate(room_id: int, language: str, url: str, server_time_ms: int | None = None) -> None:
    """Start posting this language to the caption URL.

    `server_time_ms` is YouTube's clock from a response already in hand (the Date header of the API call
    that set the broadcast up); with it even the first caption is stamped correctly.
    """
    await deactivate(room_id)
    url = _https(url)
    # YouTube wants the counter to keep rising for a stream, so a re-activated stream continues where it stopped.
    targets[room_id] = CaptionTarget(language=language, url=url, seq=_last_seq.get(url, 0))
    health[room_id] = CaptionHealth(state="waiting")
    if server_time_ms is not None:
        clock_offset_ms[room_id] = server_time_ms - host_now_ms()


async def deactivate(room_id: int) -> None:
    targets.pop(room_id, None)
    clock_offset_ms.pop(room_id, None)
    _queues.pop(room_id, None)
    task = _tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    health[room_id] = CaptionHealth(state="off")


async def _post(client: httpx.AsyncClient, url: str, payload: str) -> PostReply:
    """POST one caption. A network error gives a reply with no status."""
    try:
        response = await client.post(
            url,
            content=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
    except httpx.HTTPError:
        return PostReply(status=None)
    return PostReply(status=response.status_code, text=response.text, date=response.headers.get("date"))


async def _deliver(client: httpx.AsyncClient, url: str, payload: str) -> PostReply:
    """POST, and once more after a pause when YouTube could not be reached or answered 5xx."""
    reply = await _post(client, url, payload)
    if reply.status is None or reply.status >= 500:
        await asyncio.sleep(RETRY_DELAY_S)
        reply = await _post(client, url, payload)
    return reply


def _caption_body(room_id: int, received_at_ms: int, index: int, piece: str) -> str:
    """The POST body, stamped in YouTube's time and clamped inside its accepted window."""
    youtube_now = youtube_now_ms(room_id)
    stamp = clamp_ms(received_at_ms + clock_offset_ms.get(room_id, 0) + index * CHUNK_SPACING_MS, youtube_now)
    return body(stamp, piece)


def _record(room_id: int, reply: PostReply, skewed: bool) -> None:
    state = health.setdefault(room_id, CaptionHealth())
    if reply.status is not None and 200 <= reply.status < 300:
        state.state, state.reason, state.last_ok_at = "posting", "", time.time()
        return
    state.state = "failing"
    if reply.status == 400 and skewed:
        state.reason = SKEW_REASON
    elif reply.status and reply.status < 500:
        state.reason = REJECTED_REASON.format(status=reply.status)
    else:
        state.reason = UNREACHABLE_REASON
    logger.warning("youtube_captions post_failed room_id=%s status=%s", room_id, reply.status)


async def _send_one(
    client: httpx.AsyncClient, room_id: int, target: CaptionTarget, received_at_ms: int, index: int, piece: str
) -> None:
    target.seq += 1
    _last_seq.pop(target.url, None)  # re-insert last, so the oldest stream is the one forgotten
    _last_seq[target.url] = target.seq
    while len(_last_seq) > MAX_REMEMBERED_STREAMS:
        _last_seq.pop(next(iter(_last_seq)))
    url = _with_seq(target.url, target.seq)
    reply = await _deliver(client, url, _caption_body(room_id, received_at_ms, index, piece))
    shift = learn_clock(room_id, reply)
    skewed = _is_skew(reply, shift)
    if skewed and shift is not None:
        # The offset was just refreshed from this answer: stamp the caption again and try once more.
        reply = await _deliver(client, url, _caption_body(room_id, received_at_ms, index, piece))
        # What is recorded is the answer to the retry, so classify that one, not the first.
        skewed = _is_skew(reply, learn_clock(room_id, reply))
    _record(room_id, reply, skewed)


def _is_skew(reply: PostReply, shift: int | None) -> bool:
    return reply.status == 400 and ("skew" in reply.text.lower() or (shift is not None and abs(shift) > CLOCK_SHIFT_MS))


async def _sender(room_id: int) -> None:
    async with httpx.AsyncClient(timeout=POST_TIMEOUT_S, transport=_transport) as client:
        while True:
            target = targets.get(room_id)
            queue = _queues.get(room_id)
            if target is None or queue is None:
                return
            received_at_ms, text = await queue.get()
            for index, piece in enumerate(chunks(text)):
                target = targets.get(room_id)
                if target is None:
                    return
                if index and not queue.empty() and host_now_ms() - received_at_ms > MAX_CAPTION_AGE_MS:
                    # A long final should not hold up newer captions for minutes: the rest of it is stale.
                    logger.warning("youtube_captions stale_chunks_skipped room_id=%s", room_id)
                    break
                await _send_one(client, room_id, target, received_at_ms, index, piece)
