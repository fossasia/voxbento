# Plan 007: Add a guided "From a YouTube stream" choice to Room audio

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan
> in `plans/README.md`, unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- portal/room_setup.py portal/templates/admin/room_detail.html portal/static/js/room-detail.js tests/test_room_setup.py website/docs docs`
> Plans 003, 004, 005 and 006 must be merged first:
> `grep -q "getDisplayMedia" portal/static/js/program-sender.js && grep -q "RTMPS_MODE" portal/program_relay.py && test -f portal/youtube_account.py && test -f portal/youtube_captions.py`.
> Also check fossasia/voxbento#751 (guided floor-source setup): if it merged a different
> room-audio chooser, STOP and re-plan this as an addition to that chooser.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (composition of existing pieces; UI + docs)
- **Depends on**: plans 003, 004, 005, 006 (DONE). Coordinate with #751.
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

After plans 003–006 every building block exists, but an organiser still has to know
which one fits their setup. The room page currently asks "How will VoxBento hear this
room?" with three protocol-flavoured choices (online meeting, streaming software,
sound-desk computer). The team's most common case, "our event is on YouTube", has
no direct entry, so people fall back to the Jitsi screen-share workaround. This plan
adds a fourth choice, **From a YouTube stream**, that asks two plain questions and shows
exactly one recipe.

## Current state

- `portal/room_setup.py:570-587`:
  ```python
  AUDIO_MEETING = "meeting"
  AUDIO_ENCODER = "encoder"
  AUDIO_DESK = "desk"

  def room_audio_choice(room: Room) -> str:
      """Which way VoxBento hears the room today, so the Room audio tab opens on it ('' if none yet)."""
      if room.floor_source == "program_ingest":
          if room.program_key_hash:
              return AUDIO_ENCODER
          if room.program_session_secret_hash:
              return AUDIO_DESK
      if room.meeting_agent_enabled:
          return AUDIO_MEETING
      return ""
  ```
  Tested by `tests/test_room_setup.py` (parametrised, line ~927).
- `portal/templates/admin/room_detail.html` (~lines 160–183), owner-only section
  `#room-panel-audio`: a `[data-audio-chooser]` card with radios
  `name="audio_choice"` values `meeting|encoder|desk` (each a `<label class="audio-choice">`
  with `<strong>` title and `<small>` hint), then panels:
  ```html
  <div data-audio-panel="meeting">{% include 'admin/_meeting_source.html' %}</div>
  <div data-audio-panel="encoder">{% include 'admin/_program_ingest.html' %}</div>
  <div data-audio-panel="desk">{% include 'admin/_program_sender.html' %}</div>
  ```
- `portal/static/js/room-detail.js:67-80` toggles `[data-audio-panel]` by the checked radio:
  `panels.forEach((panel) => { panel.hidden = panel.dataset.audioPanel !== choice; });`.
- After plans 003–006: sender page supports **A browser tab**; program ingest supports
  modes `whip`, `rtmps` (extra destination), `rtmps_relay` (VoxBento forwards);
  `_youtube_connect.html` + `youtube-connect.js` provide Sign in with YouTube, broadcast
  picker and captions toggle; `ingest_status` has `youtube` + `youtube.captions`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest tests/test_room_setup.py tests/test_youtube_account.py tests/test_youtube_captions.py -q` | all pass |
| Full | `uv run pytest tests/ -q` | all pass |
| JS | `node --check portal/static/js/room-detail.js && node --check portal/static/js/youtube-source.js` | exit 0 |
| Lint | `uv run ruff check .` | exit 0 |
| Docs build (if configured) | see `website/package.json` scripts | exit 0 |

## Scope

**In scope**:
- `portal/room_setup.py` (`AUDIO_YOUTUBE`, `room_audio_choice`)
- `portal/templates/admin/room_detail.html` (fourth radio + panel)
- `portal/templates/admin/_youtube_source.html` (create)
- `portal/static/js/youtube-source.js` (create), `portal/static/js/room-detail.js` (only if the chooser needs to know the new value)
- `tests/test_room_setup.py` (extend parametrised cases)
- Docs: `website/docs/admin/youtube.mdx` (create), sidebar entry if the site uses one,
  `website/docs/admin/events-and-rooms.mdx` (link), `docs/youtube-connect.md` (link back),
  `docs/meeting-source.md` (one line: Jitsi is for real meetings; YouTube streams use the YouTube choice)

**Out of scope**:
- Backend behaviour of ingest, relay, sender, OAuth or captions (already built).
- Removing the Jitsi meeting source or the floor bot.
- `README.md`.

## Git workflow

- Branch from `dev`: `feat/room-audio-youtube`.
- Commit style: `feat(room): guided setup for YouTube streams`.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Remember the choice

`portal/room_setup.py`: add `AUDIO_YOUTUBE = "youtube"`. In `room_audio_choice`,
return `AUDIO_YOUTUBE` first when `room.youtube_broadcast_id` is set **and** the room
uses a YouTube-compatible source: `floor_source == "program_ingest"` (any mode) or no
source chosen yet. Keep the other branches unchanged.
Extend the parametrised test in `tests/test_room_setup.py` with: broadcast linked +
relay key → `youtube`; broadcast linked + sender session → `youtube`; no broadcast +
key → `encoder` (unchanged).

**Verify**: `uv run pytest tests/test_room_setup.py -q` → all pass.

### Step 2: Radio + panel

In `room_detail.html` add, **first** in the radiogroup:
```html
<label class="audio-choice">
  <input type="radio" name="audio_choice" value="youtube" {% if audio_choice == 'youtube' %}checked{% endif %}>
  <span><strong>From a YouTube stream</strong><small>Your event streams on YouTube. VoxBento adds captions, translations and translated speech.</small></span>
</label>
```
and the panel `<div data-audio-panel="youtube">{% include 'admin/_youtube_source.html' %}</div>`.

**Verify**: `uv run pytest tests/ -q -k "room"` → all pass.

### Step 3: `_youtube_source.html` + `youtube-source.js`

A three-step card (reuse `.program-setup-step`, `.program-step` classes from
`_program_ingest.html`; no inline script; module script tag at the end like other partials):

1. **Which broadcast?** If `settings.youtube_connect_enabled`: include
   `_youtube_connect.html` (sign-in + broadcast picker). Below it, a quiet link:
   "Not your channel, or can't sign in? Skip to step 2." If not enabled: a sentence
   telling the owner the operator has not set up YouTube sign-in, and go to step 2.
2. **How does video reach YouTube?** Radio group `name='youtube-route'`:
   - `encoder`: "OBS, vMix, Wirecast or a hardware encoder". Result: "VoxBento forwards
     to YouTube." Button **Set up forwarding** switches the encoder panel to relay mode
     (call the existing `POST …/program-ingest` with `{"action": "enable", "mode": "rtmps_relay"}`
     after a confirm dialog that states the reliability trade-off verbatim:
     "While this is on, a VoxBento outage also interrupts your YouTube stream."). Ensure the linked
     broadcast's destination is stored after relay mode is enabled (if a broadcast was already linked
     in Step 1, immediately re-resolve and store its stream destination, e.g. by re-triggering
     `PUT …/youtube/broadcast` or ensuring relay enablement resolves the linked broadcast's stream key).
     Only when `destination.configured` is confirmed true in the ingest status response, display the
     confirmation: "Your YouTube stream key was filled in for you." Then reveal the server/stream-key
     display from the encoder panel (link or scroll to `data-audio-panel="encoder"`). Disable this option
     when `relay_available` is false, with the operator hint.
   - `multi`: "StreamYard, Restream, Zoom or Teams live streaming, or OBS with a
     multi-output plugin". Result: enable mode `rtmps` and show Server + Stream key with
     platform-specific one-liners (StreamYard: *Destinations → Custom RTMP*; Restream:
     *Add destination → Custom RTMP*; Zoom: *Settings → Allow live streaming → Custom live streaming service*).
     Disable when `rtmps_available` is false.
   - `none`: "YouTube Studio webcam or phone, or a stream I don't control". Result:
     **Open sender** link to the sender page with `?source=tab` (the sender preselects
     the tab source when the query is present: add that one-liner to
     `program-sender.js` only if plan 003 did not already) and the instruction
     "Open the YouTube stream in another tab, click Start sending, pick that tab and keep
     'Also share tab audio' ticked. Keep both tabs open."
3. **Check it works**: compact status rows from `GET …/program-ingest`:
   *YouTube broadcast* (`youtube.life_cycle_status`), *Audio → VoxBento*
   (`health.ingest`), *VoxBento → YouTube* (relay only), *Captions on YouTube*
   (`youtube.captions`), *Captions and translation* (`health.processing`); plus the
   listener link/QR that already exists for the event (reuse its URL helper; do not
   create a new token type).

`youtube-source.js` only orchestrates existing endpoints; it must not duplicate
`program-ingest.js` logic. If shared helpers are needed, import them from
`program-ingest.js` only if that file exports them, otherwise call the HTTP endpoints directly.

**Verify**: `node --check portal/static/js/youtube-source.js` → exit 0;
`grep -n "<script>" portal/templates/admin/_youtube_source.html` → no matches (no inline script).

### Step 4: Docs

- Create `website/docs/admin/youtube.mdx` "Captions and translation for YouTube streams":
  the three routes as a decision table (row per answer: what to do, encoder change,
  whether YouTube is independent of VoxBento), Sign in with YouTube, captions on YouTube,
  the tab-audio fallback, and a "Why VoxBento can't just take a YouTube link" box: YouTube's
  developer policies forbid downloading or separating a stream's audio, and automated
  pulling is blocked.
- Link it from `website/docs/admin/events-and-rooms.mdx` and `docs/youtube-connect.md`.
- `docs/meeting-source.md`: add one line under the intro: "Streaming on YouTube? Use
  **Room audio → From a YouTube stream** instead of sharing YouTube into the meeting."

**Verify**: `grep -rn "From a YouTube stream" website/docs docs` → ≥ 3 matches.

### Step 5: Manual walk-through (record in the PR with screenshots)

Three rooms, one per route (encoder relay, StreamYard extra destination, tab capture).
For each: captions on the VoxBento listener page; for the signed-in rooms, CC on the
YouTube player. Keyboard-only navigation through the card works; focus order follows
the steps.

## Test plan

- Extended `tests/test_room_setup.py` cases (Step 1).
- A render test (follow `tests/test_program_sender.py::test_standalone_sender_page_renders`
  style, in the existing room-page test module) asserting `value="youtube"` and
  `data-audio-panel="youtube"` are present for owners and absent for coordinators.

## Done criteria

- [ ] `uv run pytest tests/ -q` exits 0
- [ ] `node --check portal/static/js/youtube-source.js` exits 0
- [ ] `uv run ruff check .` exits 0
- [ ] `grep -rn "youtube.com/embed\|iframe_api" portal` returns no matches
- [ ] ! grep -rnE "stream_name|refresh_token|captions_url" portal/static/js/youtube-source.js | grep -i "log" (machine-checkable no-match check confirming secrets are never logged)
- [ ] `git diff --name-only dev...HEAD` lists only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- #751 has landed a different chooser (see drift check).
- Any route needs a backend change to work (that belongs in plans 003–006; report the gap).
- The relay confirm wording would have to be softened to fit the UI. The outage
  trade-off must stay explicit (#746 success criteria).

## Maintenance notes

- This card is the place to add future YouTube-adjacent options (e.g. RTMP-out of a
  translated programme per language) without new top-level choices.
- Reviewers: check that the three routes map to the right modes, and that copy never
  implies VoxBento reads audio from YouTube.
