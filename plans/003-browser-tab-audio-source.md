# Plan 003: Add a "Browser tab" source to the program audio sender

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan
> in `plans/README.md`, unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- portal/static/js/program-sender.js portal/templates/admin/program_sender.html tests/test_program_sender.py .github/workflows/tests.yml docs/program-stream-ingest.md website/docs/admin/program-ingest.mdx`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW (frontend only; no backend, auth or schema change)
- **Depends on**: `plans/002-youtube-intake-spike.md` (must be marked DONE in `plans/README.md` and `plans/002-results.md` Step 1 must have confirmed Chrome/Edge tab audio). If `plans/002-results.md` is missing or plan 002 is not marked DONE, stop execution immediately (see STOP conditions).
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

Today, to caption a YouTube stream the organiser shares a YouTube tab into a Jitsi
meeting and the headless floor bot joins Jitsi to re-capture that audio:
YouTube → tab → Jitsi (re-encode, SFU) → bot Chromium → PulseAudio → ffmpeg → RTSP →
MediaMTX → STT. Eight hops, two lossy re-encodes, frequent join/lobby failures.
The program audio sender page already publishes authenticated WHIP straight to the
room's floor path. Letting it capture a **browser tab's audio** collapses the path to
tab → WebRTC → MediaMTX → STT with no Jitsi and no bot, and needs no backend change.

## Current state

- `portal/templates/admin/program_sender.html`: standalone sender page (event owners
  only, served at `/admin|workspace/events/{id}/rooms/{id}/sender/`). It has one
  `<select id='sender-device'>` for audio inputs, buttons `sender-start`,
  `sender-mute`, `sender-stop`, `sender-test`, `sender-record`, status `sender-status`,
  and loads `<script type='module' src='/static/js/program-sender.js'>`. The
  `<section id='program-sender'>` has `data-storage-key='voxbento.sender.{{ room.id }}'`.
- `portal/static/js/program-sender.js` (≈600 lines, plain ES module, 2-space indent,
  semicolons, single quotes). Key functions:
  - `supported()` (line ~63): `navigator.mediaDevices.getUserMedia && window.RTCPeerConnection`.
  - `openInput()` (line ~111): calls `getUserMedia({audio: audioConstraints})` with
    `{echoCancellation: false, noiseSuppression: false, autoGainControl: false}`, then
    `releaseInput()`, sets `stream`, adds `track.addEventListener('ended', handleInputLost)`,
    clones the track into `meterStream`, `startMeter()`, and swaps the track into an
    existing RTCPeerConnection sender via `replaceTrack`.
  - `handleInputLost()` (line ~130): waits for the preferred USB device and re-calls
    `openInput()`. **A tab capture cannot be reopened without a user click**, so tab
    mode must not call this.
  - `connect()` (line ~367) re-mints a session and re-publishes **without** reopening
    the input, so WebRTC reconnects keep an existing tab track. Good; keep it.
  - `start()` (line ~418): `if (!stream) await openInput();` then `connect()`.
  - `stop(keepState)` (line ~446): tears down peer, `releaseInput()`, releases WHIP
    resource and session.
  - `reportInputError(error)` (line ~494): maps `NotAllowedError`/`SecurityError` to
    state `permission-denied` with a microphone message.
  - `startMeter()` (line ~148) connects the clone to an `AnalyserNode` only, **never to
    `audioContext.destination`**. Keep it that way.
- Pure-module convention for testable JS: `portal/static/js/booth-reconnect.js`
  exports pure functions; `tests/test_booth_reconnect.mjs` imports them and uses a
  tiny `assert(condition, message)` helper with PASS/FAIL counters, run by
  `node tests/test_booth_reconnect.mjs`. CI lists every node test explicitly in
  `.github/workflows/tests.yml` under "run node tests" and syntax-checks some files
  under "Validate JS syntax".
- `tests/test_program_sender.py::test_standalone_sender_page_renders` asserts the
  page renders and references `/static/js/program-sender.js`.

Repository rules that apply (from `agents.md` and `.github/instructions/js.instructions.md`):
plain ES modules, no frameworks, no inline `<script>` blocks, never route captured
audio to `AudioContext.destination`, media never goes through FastAPI.

YouTube policy constraint: do **not** embed a YouTube player in VoxBento for this.
The organiser opens YouTube themselves in a normal tab; VoxBento only captures what
the user explicitly shares via the browser's picker.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Install | `uv sync --python 3.13 --dev` | exit 0 |
| JS syntax | `node --check portal/static/js/program-sender.js && node --check portal/static/js/program-sender-source.js` | exit 0 |
| JS unit test | `node tests/test_program_sender_source.mjs` | all PASS, exit 0 |
| Python tests | `uv run pytest tests/test_program_sender.py -q` | all pass |
| Lint | `uv run ruff check .` | exit 0 |

## Scope

**In scope** (the only files you should modify or create):
- `portal/static/js/program-sender-source.js` (create)
- `portal/static/js/program-sender.js`
- `portal/templates/admin/program_sender.html`
- `tests/test_program_sender_source.mjs` (create)
- `tests/test_program_sender.py` (extend the render test only)
- `.github/workflows/tests.yml` (add one `node --check` line and one node test line)
- `docs/program-stream-ingest.md` (section "Browser program-audio sender")
- `website/docs/admin/program-ingest.mdx` (short subsection)
- `plans/README.md` (status-row update for plan 003)

**Out of scope** (do NOT touch):
- `portal/program_ingest.py`, `portal/routers/program_ingest.py`: the session/auth
  flow is already correct for any audio track.
- `floor-bot/`, `portal/meeting_source.py`: Jitsi stays for real meetings.
- Anything that embeds or scripts a YouTube player.
- `README.md` (root README; repo rule: no feature docs in root README).

## Git workflow

- Branch from `dev`: `feat/sender-tab-audio-source`.
- Commit style from `git log`: `feat(sender): capture a browser tab's audio as the floor source`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the pure helper module

Create `portal/static/js/program-sender-source.js`:

```js
// Floor audio sources the program sender can publish.

export const SOURCES = Object.freeze({DEVICE: 'device', TAB: 'tab'});

/** Program audio must bypass voice DSP, which pumps levels and hurts music and STT. */
export const RAW_AUDIO = Object.freeze({echoCancellation: false, noiseSuppression: false, autoGainControl: false});

/** getDisplayMedia options that offer browser tabs with their audio and keep it audible locally. */
export function tabCaptureOptions() {
  return {
    // Chromium requires a video request; the sender stops the video track immediately.
    video: true,
    audio: {...RAW_AUDIO, suppressLocalAudioPlayback: false},
    selfBrowserSurface: 'exclude',
    surfaceSwitching: 'include',
    systemAudio: 'exclude',
    monitorTypeSurfaces: 'exclude',
  };
}

export function normalizeSource(value) {
  return value === SOURCES.TAB ? SOURCES.TAB : SOURCES.DEVICE;
}

export function tabCaptureSupported(mediaDevices) {
  return Boolean(mediaDevices && typeof mediaDevices.getDisplayMedia === 'function');
}

/** The single audio track to publish, or null when the user did not share tab audio. */
export function pickTabAudio(stream) {
  stream.getVideoTracks().forEach(track => track.stop());
  return stream.getAudioTracks()[0] || null;
}
```

**Verify**: `node --check portal/static/js/program-sender-source.js` → exit 0.

### Step 2: Unit-test the helper

Create `tests/test_program_sender_source.mjs` modelled on `tests/test_booth_reconnect.mjs`
(same header comment style, same `assert` helper, exit code 1 on any failure). Cases:
1. `normalizeSource('tab') === 'tab'`; `normalizeSource('x') === 'device'`; `normalizeSource(undefined) === 'device'`.
2. `tabCaptureOptions().video === true`, `.audio.echoCancellation === false`,
   `.audio.suppressLocalAudioPlayback === false`, `.selfBrowserSurface === 'exclude'`,
   `.systemAudio === 'exclude'`.
3. `tabCaptureSupported(undefined) === false`; `tabCaptureSupported({getDisplayMedia() {}}) === true`.
4. `pickTabAudio` with a fake stream `{getVideoTracks: () => [v], getAudioTracks: () => [a]}`
   where `v.stop` records a call: returns `a` and `v` was stopped.
5. `pickTabAudio` with no audio tracks returns `null` and still stops video.

**Verify**: `node tests/test_program_sender_source.mjs` → all PASS, exit 0.

### Step 3: Add the source picker to the template

In `portal/templates/admin/program_sender.html`, inside `.program-sender-controls`
**before** the `<label for='sender-device'>`, add a fieldset (no inline script, use
existing classes where possible):

```html
<fieldset class='program-sender-source' id='sender-source'>
  <legend>Audio source</legend>
  <label><input type='radio' name='sender-source' value='device' checked> Audio input on this computer (sound desk, USB interface)</label>
  <label><input type='radio' name='sender-source' value='tab'> A browser tab (for example a YouTube stream)</label>
</fieldset>
<div id='sender-tab-help' class='program-sender-tab-help' hidden>
  <p>Open the stream in another tab of this browser (Chrome or Edge), then click <strong>Start sending</strong> and pick that tab. Keep <strong>Also share tab audio</strong> ticked. Keep both tabs open; you can use other tabs meanwhile.</p>
  <label><input type='checkbox' id='sender-tab-rights'> I have the right to interpret and caption this stream.</label>
</div>
```

Wrap the existing `<label for='sender-device'>` and `<select id='sender-device'>` in
`<div id='sender-device-row'>…</div>` so the script can hide it in tab mode.
Change the lede paragraph to mention both a sound desk and a browser tab.

**Verify**: `uv run pytest tests/test_program_sender.py -q` → all pass.

### Step 4: Wire tab capture into `program-sender.js`

Edits (keep the file's style):
1. `import {SOURCES, RAW_AUDIO, normalizeSource, pickTabAudio, tabCaptureOptions, tabCaptureSupported} from './program-sender-source.js';`
   at the top; replace the inline `audioConstraints` literal in `openInput()` with `{...RAW_AUDIO}`.
2. New state: `let source = normalizeSource(savedSource());` with
   `savedSource()`/`saveSource()` using `localStorage` key `${card.dataset.storageKey}.source`,
   wrapped in try/catch exactly like `savedDevice()`/`saveDevice()`. On initial load, find the
   radio matching `source` (`document.querySelector(\`input[name='sender-source'][value='${source}']\`)`),
   and set its `.checked = true` before toggling container visibility (`#sender-device-row` and
   `#sender-tab-help`). This ensures the selected radio remains strictly consistent with the
   source used by `start()`, including when the restored selection is `tab`.
3. `supported()` returns true for tab mode only when `tabCaptureSupported(navigator.mediaDevices) && window.RTCPeerConnection`.
   When tab mode is selected but unsupported, set state `error` with
   `'This browser cannot share a tab\'s audio. Use Chrome or Edge.'`.
4. New `openTab()`:
   ```js
   async function openTab() {
     const shared = await navigator.mediaDevices.getDisplayMedia(tabCaptureOptions());
     const track = pickTabAudio(shared);
     if (!track) {
       shared.getTracks().forEach(item => item.stop());
       throw new SourceError('No tab audio was shared. Pick a browser tab and keep "Also share tab audio" ticked.');
     }
     releaseInput();
     stream = new MediaStream([track]);
     track.enabled = !muted;
     track.addEventListener('ended', handleTabEnded);
     meterStream = new MediaStream([track.clone()]);
     startMeter();
     const sender = pc?.getSenders().find(item => item.track?.kind === 'audio' || item.track === null);
     if (sender) await sender.replaceTrack(track);
   }
   ```
   Define `class SourceError extends Error {}` near the existing `SenderError` (or reuse
   the existing error class pattern you find in the file; match it).
5. `openSource()` dispatches: `source === SOURCES.TAB ? openTab() : openInput()`.
   Replace the calls to `openInput()` in `start()`, `startTest()` and `recordCheck()`
   with `openSource()`. Leave `changeDevice()` and `handleInputLost()` calling
   `openInput()` (device mode only).
6. `handleTabEnded()`: the user clicked the browser's "Stop sharing" or closed the
   shared tab. If sending: `await stop(true); setState('stopped', 'Tab sharing ended. Click Start sending and pick the tab again.');`.
   If only testing: `stopTest(true)`. **Never** call `getDisplayMedia` from here (it
   needs a user gesture).
7. `start()`: in tab mode, refuse with state `error` and message
   `'Confirm you have the right to interpret this stream.'` unless
   `#sender-tab-rights` is checked.
8. `reportInputError(error)`: for `SourceError` show its message with state `error`;
   for `NotAllowedError` or `SecurityError` in tab mode show `'Tab sharing was cancelled or blocked.'`
   with a source-neutral or tab-specific badge state (e.g. `'permission-denied'` with badge label
   `'Permission denied'` or `'Tab sharing blocked'`, avoiding the microphone-specific `'Microphone blocked'`
   badge label while keeping the status message's tab-sharing context intact).
9. Source radio `change` handler: if `sending`, `testing`, or `recorder` is active, revert the radio
   and show `'Stop sending or testing before changing the source.'` (rejecting source changes while
   recording ensures `releaseInput()` cannot interrupt an active 5-second recording of `meterStream`);
   otherwise set `source`, `saveSource()`, `releaseInput()`, toggle `#sender-device-row` (hidden in tab mode)
   and `#sender-tab-help` (shown in tab mode). Apply the same toggling once on load.
10. Ensure nothing new connects to `audioContext.destination`.

**Verify**:
- `node --check portal/static/js/program-sender.js` → exit 0
- `grep -n "destination" portal/static/js/program-sender.js` → no `audioContext.destination` usage
- `grep -n "getDisplayMedia" portal/static/js/program-sender.js` → exactly one call (inside `openTab`)

### Step 5: Extend the Python render test

In `tests/test_program_sender.py::test_standalone_sender_page_renders` add:
`assert "name='sender-source'" in response.text` and
`assert "id='sender-tab-rights'" in response.text`.

**Verify**: `uv run pytest tests/test_program_sender.py -q` → all pass.

### Step 6: CI wiring

In `.github/workflows/tests.yml`: under "Validate JS syntax" add
`node --check portal/static/js/program-sender-source.js`; under "run node tests" add
`node tests/test_program_sender_source.mjs`.

**Verify**: `grep -n "program-sender-source\|test_program_sender_source" .github/workflows/tests.yml` → 2 matches.

### Step 7: Docs

- `docs/program-stream-ingest.md`, section "Browser program-audio sender": add a
  paragraph "Capturing a browser tab" covering: Chrome/Edge only; organiser opens
  the stream in their own tab; pick it and keep "Also share tab audio"; keep both tabs
  open (the computer must not sleep); "Stop sharing" stops the sender; this replaces
  sharing a YouTube tab into Jitsi; VoxBento never embeds or downloads YouTube.
- `website/docs/admin/program-ingest.mdx`: a 4–6 line subsection with the same steps.

**Verify**: `grep -n "Also share tab audio" docs/program-stream-ingest.md website/docs/admin/program-ingest.mdx` → ≥2 matches.

### Step 8: Manual browser check (record result in the PR description)

1. Room with floor transcription on. Open the YouTube stream in tab A, the sender page
   in tab B (Chrome). Choose **A browser tab**, tick the rights box, **Start sending**,
   pick tab A.
2. Expect badge **Live**, meter moving, room page "Captions and translation:
   Processing", listener captions within a few seconds.
3. Click Chrome's **Stop sharing**: badge **Stopped** with the tab-ended message.
4. Restart, then turn Wi-Fi off for 10 s and on: sender shows Reconnecting then Live
   **without** asking to pick the tab again.

## Test plan

- New: `tests/test_program_sender_source.mjs` (5 cases above), modelled on `tests/test_booth_reconnect.mjs`.
- Extended: `tests/test_program_sender.py::test_standalone_sender_page_renders`.
- Existing backend tests in `tests/test_program_sender.py` must keep passing unchanged:
  the backend contract is untouched.

## Done criteria

- [ ] `node tests/test_program_sender_source.mjs` exits 0 with ≥ 10 PASS lines
- [ ] `node --check portal/static/js/program-sender.js` exits 0
- [ ] `uv run pytest tests/test_program_sender.py -q` exits 0
- [ ] `uv run ruff check .` exits 0
- [ ] `grep -c "getDisplayMedia" portal/static/js/program-sender.js` prints `1`
- [ ] `grep -rn "youtube.com/embed\|iframe_api" portal/templates portal/static/js` returns no matches
- [ ] `git diff --name-only dev...HEAD` lists only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- `plans/002-results.md` is missing, plan 002 is not marked DONE in `plans/README.md`, or Step 1
  results say Chrome/Edge do not deliver a tab audio track, or that the track dies when the
  tab is backgrounded.
- `program-sender.js` no longer has `openInput()`/`connect()`/`stop()` shaped as described
  (drift).
- Making tab mode work appears to need a backend change (route, auth, session TTL).
  Report instead; the backend is out of scope.
- Any approach would require embedding the YouTube player or fetching YouTube media.

## Maintenance notes

- Chrome may change `getDisplayMedia` hints (`selfBrowserSurface`, `systemAudio`,
  `monitorTypeSurfaces`). Unknown hints are ignored, so failures show up as a worse
  picker, not errors.
- A reviewer should check that `handleTabEnded` never calls `getDisplayMedia` and that
  `connect()` still never reopens the input.
- Deferred: making Jitsi screen-share guidance disappear from organiser docs belongs
  to plan 007.
