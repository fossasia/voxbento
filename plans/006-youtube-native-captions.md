# Plan 006: Post VoxBento captions to the YouTube broadcast's native caption track

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan
> in `plans/README.md`, unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- portal/transcription/aggregator.py portal/translations/worker.py portal/transcription/timing.py portal/models.py portal/routers/program_ingest.py alembic/versions`
> Plan 005 must already be merged: `test -f portal/youtube_account.py && test -f portal/routers/youtube.py` must succeed.
> If any in-scope file changed beyond plan 005's additions, compare the "Current state"
> excerpts against the live code; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (new outbound path on the live caption hot path; must never slow STT)
- **Depends on**: `plans/005-connect-youtube-account.md` (DONE); `plans/002-youtube-intake-spike.md` step 4 (exact accepted POST body)
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

Viewers who stay on YouTube never see VoxBento captions unless they open a second
page. YouTube Live accepts third-party captions over HTTP POST to a per-stream
**caption ingestion URL** (`liveStream.contentDetails.closedCaptionsIngestionUrl`)
when the broadcast's `contentDetails.closedCaptionsType` is `closedCaptionsHttpPost`.
With the channel already connected (plan 005), VoxBento can switch that on and post
its final captions, in the floor language or one chosen translation, into YouTube's
own CC button. This is how professional caption services (StreamText, CaptionMaker)
integrate, and it touches only text, never YouTube media.

## Current state

- `portal/transcription/aggregator.py`:
  - `CaptionAggregator.__init__(self, broadcast_callback, room_id: int | None = None)`.
  - `handle_final(booth_id, text)` (line ~137) sets the `segment_timing` context var for
    floor booths of program-ingest rooms, then `await self.publish_final(...)`.
  - `publish_final(self, booth_id, final_text, segment_id, seq)` (line ~167) broadcasts
    `{"type": "caption", "status": "final", …}` then schedules `_save_and_translate`
    with `track_task(asyncio.create_task(...))` (`from portal.tasks import track_task`).
  - Floor booth ids end with `-floor` (`make_booth_id(slug, room_id, "floor")`).
- `portal/translations/worker.py::_translate_and_broadcast` (line ~174) has
  `room: Room`, `lang_code`, `booth_id_str`, `translated_text`; after saving it calls
  `listener_manager.broadcast(target_booth_id, {"type": "translated_caption", "status": "final", …})` (line ~295).
- `portal/transcription/timing.py`: `segment_timing: ContextVar[SegmentTiming | None]`,
  `SegmentTiming.source_received_at_ms` (wall-clock ms when STT received the utterance start).
- After plan 005: `portal/youtube_account.py` provides `access_token(connection)`,
  `get_broadcast`, `get_stream` (`StreamInfo.captions_url`, `repr=False`),
  `Broadcast.latency_preference`, `Broadcast.closed_captions_type`, `YouTubeAuthError`,
  `YouTubeAPIError`; `Room.youtube_broadcast_id`, `Room.youtube_stream_id`;
  `portal/routers/youtube.py` owner routes; `ingest_status` has a `youtube` block.
- YouTube rules: ultra-low-latency broadcasts do not support captions. One caption
  feed per stream. POST body is a timestamp line then the caption text. The exact
  accepted format is recorded in `plans/002-results.md` step 4. Use it verbatim.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Migrate | `uv run alembic upgrade head` | exit 0, ends at `040` |
| Focused | `uv run pytest tests/test_youtube_captions.py -q` | all pass |
| Regression | `uv run pytest tests/test_caption_aggregator.py tests/test_youtube_account.py tests/test_program_ingest.py -q` | all pass |
| Full | `uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check .` | exit 0 |

## Scope

**In scope**:
- `alembic/versions/040_youtube_captions.py` (create), `portal/models.py` (one `Room` column)
- `portal/youtube_captions.py` (create)
- `portal/youtube_account.py` (add `enable_http_captions(connection, broadcast)`)
- `portal/routers/youtube.py` (captions route), `portal/workspace_routing.py` (regex already covers `/youtube/…`? if not, extend)
- `portal/transcription/aggregator.py` (one non-blocking call in `publish_final`)
- `portal/translations/worker.py` (one non-blocking call after the translated caption broadcast)
- `portal/routers/program_ingest.py` (`youtube.captions` health in status)
- `portal/templates/admin/_youtube_connect.html`, `portal/static/js/youtube-connect.js` (toggle + language select + status)
- `fastapi_app.py` (cancel caption sender tasks on shutdown, if lifespan pattern requires)
- `tests/test_youtube_captions.py` (create)
- `docs/youtube-connect.md` (captions section)

**Out of scope**:
- Interpreter (non-floor) booths: YouTube captions are room-level floor captions only.
- Partial/interim captions: post **finals only**.
- Multiple simultaneous YouTube caption languages (YouTube accepts one feed).
- Embedded EIA-608/708 captions in the relayed video.
- Any change to listener WebSocket payloads.

## Git workflow

- Branch from `dev`: `feat/youtube-native-captions`.
- Commit style: `feat(youtube): post final captions to the broadcast's caption track`.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Column + migration

`Room.youtube_captions_language: Mapped[str | None] = mapped_column(String(10), nullable=True)`
(`None` = off; `"floor"` = source-language captions; otherwise a target language code
that exists in the room's translation languages). Migration `040` (`down_revision = "039"`),
idempotent, batch-alter for SQLite.

**Verify**: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` → exit 0.

### Step 2: Enable HTTP-POST captions on the broadcast

In `portal/youtube_account.py` add `async def enable_http_captions(connection, broadcast_id) -> None`:
1. `get_broadcast` with `part=snippet,contentDetails,status` (keep the **full** `contentDetails` object and check lifecycle status). Note the API restriction: YouTube Live API only allows caption configuration updates on `created` or `ready` broadcasts; active or live broadcasts cannot switch to HTTP-post captions via the API.
2. Check `broadcast.status.lifeCycleStatus`: if not in `("created", "ready")`, raise `ValueError("Captions must be set up before the broadcast starts. Active or live broadcasts cannot switch to HTTP-post captions via the API; turn on 'POST captions to URL' in YouTube Studio directly.")`.
3. If `latencyPreference == "ultraLow"` → `raise ValueError("Ultra-low latency YouTube broadcasts cannot show captions. Choose Normal or Low latency in YouTube Studio.")`.
4. If `closedCaptionsType == "closedCaptionsHttpPost"` → return.
5. Prepare `current = dict(broadcast.contentDetails)`. Remove `enableClosedCaptions` (`current.pop("enableClosedCaptions", None)`) so the update payload does not include conflicting caption properties before adding `closedCaptionsType`. Set `current["closedCaptionsType"] = "closedCaptionsHttpPost"`.
6. Else `PUT {API_BASE}/liveBroadcasts?part=id,snippet,contentDetails` with body
   `{"id": broadcast_id, "snippet": {"scheduledStartTime": broadcast.snippet.scheduled_start_time}, "contentDetails": current}`
   (drop read-only fields `boundStreamId`, `boundStreamLastUpdateTimeMs` if the API rejects them; this is the documented update pattern: send the whole part back).
7. On `YouTubeAPIError` raise `ValueError("YouTube would not switch captions on. In YouTube Studio open the stream settings, set Closed captions to 'POST captions to URL', then try again.")`.

**Verify**: covered by tests in Step 6.

### Step 3: `portal/youtube_captions.py`

`from __future__ import annotations`. Responsibilities: per-room bounded queue, one
sender task per room, POSTing to the in-memory caption URL. Shape:

```python
MAX_QUEUE = 50
CHUNK_CHARS = 80
CHUNK_SPACING_MS = 1200

@dataclass
class CaptionTarget:
    language: str                 # "floor" or a target language code
    url: str = field(repr=False)  # capability URL: never log or return it
    seq: int = 0

@dataclass
class CaptionHealth:
    state: str = "off"            # off | waiting | posting | failing
    reason: str = ""
    last_ok_at: float | None = None

targets: dict[int, CaptionTarget] = {}
health: dict[int, CaptionHealth] = {}
_queues: dict[int, asyncio.Queue] = {}
_tasks: dict[int, asyncio.Task] = {}

def format_timestamp(ms: int) -> str: ...      # per plans/002-results.md (UTC, millisecond precision)
def chunks(text: str) -> list[str]: ...        # collapse whitespace/newlines; split ≤ CHUNK_CHARS at word boundaries
def body(ms: int, text: str) -> str: ...       # exact template from plans/002-results.md

def offer(room_id: int, language: str, text: str, received_at_ms: int | None = None) -> None:
    """Non-blocking. Called from the caption hot path; never awaits network I/O."""
    target = targets.get(room_id)
    if target is None or target.language != language or not text.strip():
        return
    queue = _queues.setdefault(room_id, asyncio.Queue(maxsize=MAX_QUEUE))
    if queue.full():
        queue.get_nowait()          # drop the oldest; captions are live, stale ones are worthless
    queue.put_nowait((received_at_ms or int(time.time() * 1000), text))
    if room_id not in _tasks or _tasks[room_id].done():
        _tasks[room_id] = track_task(asyncio.create_task(_sender(room_id)))

async def activate(room_id: int, language: str, url: str) -> None: ...  # sets targets/health "waiting"
async def deactivate(room_id: int) -> None: ...  # cancels task, clears queue/target, health "off"
async def _sender(room_id: int) -> None: ...      # loop: get; for each chunk POST url&seq=n (text/plain; charset=utf-8), timeout 5 s
```

Rules for `_sender`:
- Append `seq` as a query parameter (`&seq=N` or `?seq=N` depending on whether the URL
  already has a query), incrementing per POST.
- Chunk timestamps: `received_at_ms + i * CHUNK_SPACING_MS`.
- 2xx → health `posting`, `last_ok_at = time.time()`.
- 4xx → health `failing` with `"YouTube rejected the caption (HTTP <code>). Check the broadcast's caption settings."`;
  keep consuming (do not retry the same item).
- Network error / 5xx → one retry after 1 s, then drop; health `failing` with a generic reason.
- Never log the URL, query or response body: `logger.warning("youtube_captions post_failed room_id=%s status=%s", …)`.
- Exit the loop when `targets` no longer has the room.

**Verify**: `uv run python -c "from portal.youtube_captions import chunks; print(chunks('a '*100))"` → list of strings each ≤ 80 chars.

### Step 4: Wire activation

1. Route in `portal/routers/youtube.py`:
   `PUT /admin/events/{event_id}/rooms/{room_id}/youtube/captions`, guard `require_event_owner`,
   origin check, body `{"language": str | None}`.
   - `None` → set column `None`, `await youtube_captions.deactivate(room_id)`.
   - Else validate: room has `youtube_broadcast_id`; language is `"floor"` or one of the
     room's translation language codes (422 otherwise); call `enable_http_captions`
     (409 with its message on `ValueError`); `get_stream` for `youtube_stream_id`; require
     `captions_url` (409 "YouTube did not provide a caption address for this stream.");
     save the column; `await youtube_captions.activate(room_id, language, url)`.
2. Re-activation after restart: in `youtube_account.room_status()` (or a small helper
   called from it), when `room.youtube_captions_language` is set and
   `youtube_captions.targets` lacks the room, fetch the stream and `activate`. This
   piggybacks on the room page's status polling and the floor pipeline; additionally
   call the same helper lazily from `offer`'s first miss via a background task
   (rate-limited to once per 60 s per room) so captions resume without the page open.
3. Unlinking a broadcast or disconnecting YouTube (plan 005 routes) must call
   `deactivate` and clear `youtube_captions_language`.

**Verify**: `uv run pytest tests/test_youtube_account.py -q` → still all pass.

### Step 5: Hot-path hooks (non-blocking only)

1. `portal/transcription/aggregator.py::publish_final`, right after the final caption
   broadcast and before scheduling `_save_and_translate`:
   ```python
   if self.room_id is not None and booth_id.endswith("-floor"):
       timing = segment_timing.get()
       youtube_captions.offer(self.room_id, "floor", final_text, timing.source_received_at_ms if timing else None)
   ```
   Import at module top: `from portal import youtube_captions`.
2. `portal/translations/worker.py::_translate_and_broadcast`, right after the
   `translated_caption` broadcast:
   ```python
   if booth_id_str.endswith("-floor"):
       timing = segment_timing.get()
       youtube_captions.offer(room.id, lang_code, translated_text, timing.source_received_at_ms if timing else None)
   ```
   (import `segment_timing` from `portal.transcription.timing` if not already imported).
3. `offer` must stay synchronous and exception-safe: wrap its body in
   `try/except Exception: logger.warning("youtube_captions offer_failed room_id=%s", room_id)`.

**Verify**: `uv run pytest tests/test_caption_aggregator.py -q` → all pass (unchanged behaviour when no target).

### Step 6: Tests (`tests/test_youtube_captions.py`)

≥ 12 cases, `httpx.MockTransport` for Google + the caption URL:
1. `format_timestamp` matches the spike template for a fixed ms value.
2. `chunks` splits at word boundaries, each ≤ 80 chars, collapses newlines.
3. `offer` with no target is a no-op (no queue/task created).
4. `offer` for a different language than the target is a no-op.
5. Queue overflow drops the oldest item (put 51 items, first one gone).
6. Sender POSTs `seq=1,2,3` in order with `Content-Type: text/plain; charset=utf-8` and the expected body.
7. 400 from YouTube → health `failing`, sender continues with the next item.
8. 5xx → one retry, then dropped.
9. `caplog` contains neither the caption URL nor its query.
10. `enable_http_captions` refuses `ultraLow` with the latency message and makes no PUT.
11. `enable_http_captions` PUTs `contentDetails` with all original fields plus `closedCaptionsType`.
12. Route: language not in room languages → 422; no linked broadcast → 409; success → column set and `targets[room_id].language` set.
13. Aggregator hook: a floor final calls `offer` with `"floor"`; an interpreter-booth final does not (monkeypatch `youtube_captions.offer`).
14. Unlink broadcast → `deactivate` called, column cleared.

**Verify**: `uv run pytest tests/test_youtube_captions.py -q` → ≥ 12 passed.

### Step 7: UI + status + docs

- `ingest_status` `youtube` block gains `"captions": {"language", "state", "reason", "last_ok_at"}`.
- `_youtube_connect.html` + `youtube-connect.js`: "Captions on YouTube" select
  (Off / floor language / each translation language), shown only when a broadcast is
  linked, with a status line ("Posting", "Waiting for speech", "Failing: …").
  Note under it: "YouTube shows one caption track. Viewers can use YouTube's auto-translate on it."
- `docs/youtube-connect.md`: captions section (how to turn on, ultra-low latency
  limitation, one language, typical delay from the spike, troubleshooting).

**Verify**: `node --check portal/static/js/youtube-connect.js` → exit 0; `uv run pytest tests/ -q` → all pass.

## Test plan

See Step 6. Pattern: `tests/test_youtube_account.py` from plan 005 (fixtures, MockTransport).
Manual (record in PR): unlisted broadcast, relay or second-destination mode, captions
on "floor" → CC visible in the YouTube player within the spike-measured delay.

## Done criteria

- [ ] `uv run alembic upgrade head` exits 0 at `040`
- [ ] `uv run pytest tests/ -q` exits 0; `tests/test_youtube_captions.py` ≥ 12 passed
- [ ] `uv run ruff check .` exits 0
- [ ] `uv run python -c "import inspect, portal.youtube_captions as m; assert not inspect.iscoroutinefunction(m.offer)"` exits 0 (offer never awaits)
- [ ] ! grep -rnE "captions_url|closedCaptionsIngestionUrl" portal --include=*.py | grep -i "log" (machine-checkable no-match check confirming caption URLs are never logged)
- [ ] `git diff --name-only dev...HEAD` lists only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- `plans/002-results.md` step 4 has no working POST body template, or captions never rendered.
- The aggregator or translation worker no longer has the call sites described (drift).
- Posting needs anything other than the caption ingestion URL (e.g. embedding captions
  into the relayed video). That is a different design.
- Hooking requires awaiting network I/O on the caption hot path.

## Maintenance notes

- Caption URLs are capability URLs held only in memory; a portal restart re-fetches them
  (Step 4.2). Single-replica assumption, like the rest of program ingest.
- If YouTube adds multi-language live caption ingestion, `targets` becomes per-language;
  the hooks already pass the language.
- Reviewers: verify `offer` cannot raise or block, and the URL never appears in logs or JSON.
