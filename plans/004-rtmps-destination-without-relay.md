# Plan 004: Accept RTMPS as an independent second destination (no relay required)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan
> in `plans/README.md`, unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- portal/config.py portal/program_relay.py portal/program_ingest.py portal/routers/program_ingest.py portal/templates/admin/_program_ingest.html portal/static/js/program-ingest.js docker-compose.relay.yml scripts/setup-youtube-relay.sh docs/program-stream-ingest.md docs/program-relay.md website/docs/admin/program-ingest.mdx agents.md .agents/context/CHANGE_IMPACT_MAP.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (touches publish authorization; must not loosen existing modes)
- **Depends on**: `plans/002-youtube-intake-spike.md` step 2 (which third-party platforms accept `floor?key=…`). Maintainer approval of the invariant-3 wording change (see Step 8).
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

Most organisers who stream to YouTube use StreamYard, Restream, vMix, Zoom/Teams
live streaming or OBS with a multi-output plugin. All of these can add a **"Custom
RTMP" destination** in about 30 seconds, which is how Wordly ingests event audio
(`rtmps://media.wordly.ai/live` + session key). VoxBento already authenticates RTMPS
publishes per room, but only inside **relay mode**, which also requires the
`program-relay` service and puts VoxBento in the YouTube path. A new mode
`rtmps` ("VoxBento is an extra destination") accepts the same authenticated RTMPS
feed for captions/translation/TTS while YouTube keeps receiving the organiser's own
output, so it stays failure-isolated.

## Current state

- `portal/program_relay.py:26-28`:
  ```python
  WHIP_MODE = "whip"
  RELAY_MODE = "rtmps_relay"
  MODES = (WHIP_MODE, RELAY_MODE)
  ```
- `portal/config.py:43-49`: `program_relay_enabled: bool = False`,
  `program_rtmps_public_base: str = ""`, `program_relay_base`, `program_relay_control_secret`,
  `program_relay_max`, `program_relay_allowed_hosts`. `validate_production_secrets()`
  requires `program_relay_control_secret` ≥ 32 chars when relay is enabled (line ~114).
- `portal/models.py:145`: `Room.program_ingest_mode: Mapped[str] = mapped_column(String(20), default="whip", server_default="whip")`.
  `"rtmps"` fits; **no migration needed**.
- `portal/program_ingest.py` relay-mode checks to update:
  - `publish_permitted` (line ~193):
    ```python
    if room.program_ingest_mode == program_relay.RELAY_MODE:
        return protocol == "rtmp" and settings.program_relay_enabled and key_digest_valid(room, digest)
    return protocol == "webrtc" and credential_digest_valid(room, digest)
    ```
  - `reserved_source` (line ~203): `kind = "rtmpsConn" if room.program_ingest_mode == program_relay.RELAY_MODE else "webRTCSession"`
  - `presented_secret` (line ~211): `if data.protocol == "rtmp" and settings.program_relay_enabled and not (data.token or data.password):` then parses `key` from the query.
  - `open_sender_session` (line ~490): rejects with 409 when `floor_source == "program_ingest"` and mode is `RELAY_MODE`.
  - processing health (line ~680): AAC hint `if room.program_ingest_mode == program_relay.RELAY_MODE`.
  - `relay_request` (line ~587) and `reconcile_once` (`relay_rooms`, line ~733): **relay only; leave unchanged**.
- `portal/routers/program_ingest.py`:
  - `IngestChange.mode: Literal["whip", "rtmps_relay"] = "whip"` (line ~37).
  - `ingest_status` returns `"relay_available": settings.program_ingest_enabled and settings.program_relay_enabled`
    and `"rtmps_server": _rtmps_server(event.slug, room_id)`.
  - `ingest_change` 409s `enable` with relay mode when relay disabled (line ~163); returns
    `body["stream_key"] = f"floor?key={secret}"` when `relay_mode` (line ~197, ~211).
- `docker-compose.relay.yml` sets portal `PROGRAM_INGEST_ENABLED`/`PROGRAM_RELAY_ENABLED`/
  `PROGRAM_RTMPS_PUBLIC_BASE`…, opens MediaMTX RTMPS (`MTX_RTMP: "yes"`,
  `MTX_RTMPENCRYPTION: "strict"`, `MTX_RTMPSADDRESS: ":1936"`, cert volume, port `1936:1936`)
  and defines the `program-relay` service.
- `scripts/setup-youtube-relay.sh` writes relay settings into `.env` and appends
  `docker-compose.relay.yml` to `COMPOSE_FILE` (lines ~110-116, variable `RELAY_COMPOSE`).
- UI: `portal/templates/admin/_program_ingest.html` has radios `name='program-mode'` values
  `whip` and `rtmps_relay` and many elements with `data-mode-panel='rtmps_relay'`.
  `portal/static/js/program-ingest.js:76-77` toggles panels with
  `panel.hidden = panel.dataset.modePanel !== mode;`.
- Tests: `tests/test_program_relay.py` (fixture `relay_env` monkeypatches settings;
  18 tests) is the pattern for new tests.

Invariant to respect (`agents.md`, Core Invariant 3): "…its operator-enabled relay mode
(#749) accepts room-authenticated RTMPS (never plaintext RTMP)… No YouTube pulling,
no unauthenticated or plaintext RTMP, no SRT. Floor ownership is `jitsi_bot` or
`program_ingest`; never last-publisher-wins." RTMPS stays TLS-only (`rtmpEncryption: strict`).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Install | `uv sync --python 3.13 --dev` | exit 0 |
| Focused tests | `uv run pytest tests/test_program_rtmps.py tests/test_program_relay.py tests/test_program_ingest.py tests/test_program_sender.py -q` | all pass |
| Full tests | `uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check .` | exit 0 |
| JS syntax | `node --check portal/static/js/program-ingest.js` | exit 0 |
| Compose | `COMPOSE_FILE=docker-compose.yml:docker-compose.rtmps.yml PROGRAM_RTMPS_PUBLIC_BASE=rtmps://x:1936 PROGRAM_RTMPS_CERT_DIR=/tmp docker compose config --quiet` | exit 0 |
| Script | `bash -n scripts/setup-youtube-relay.sh` | exit 0 |

## Scope

**In scope**:
- `portal/config.py`, `portal/program_relay.py`, `portal/program_ingest.py`, `portal/routers/program_ingest.py`
- `portal/templates/admin/_program_ingest.html`, `portal/static/js/program-ingest.js`
- `docker-compose.rtmps.yml` (create), `docker-compose.relay.yml` (one env line)
- `scripts/setup-youtube-relay.sh` (add `--ingest-only`)
- `tests/test_program_rtmps.py` (create)
- `docs/program-stream-ingest.md`, `docs/program-relay.md`, `website/docs/admin/program-ingest.mdx`,
  `website/docs/configuration/environment-variables.mdx`, `agents.md` (invariant 3 wording only),
  `.agents/context/CHANGE_IMPACT_MAP.md` (Program Stream Ingest section)

**Out of scope**:
- `program-relay/` service code and relay reconciliation (`relay_request`, `program_relay.sync`).
- `mediamtx.yml` base file: RTMPS stays opt-in through compose overrides.
- SRT, plaintext RTMP, codec conversion (#750).
- Any database migration.
- `README.md`.

## Git workflow

- Branch from `dev`: `feat/rtmps-second-destination`.
- Commit style: `feat(ingest): accept RTMPS as an independent second destination`.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Settings

In `portal/config.py` next to `program_relay_enabled` add:
```python
# RTMPS contribution ingest without relaying (VoxBento as an extra destination).
# Relay deployments imply it; see rtmps_ingest_enabled.
program_rtmps_enabled: bool = False
```
and a property:
```python
@property
def rtmps_ingest_enabled(self) -> bool:
    return self.program_ingest_enabled and (self.program_rtmps_enabled or self.program_relay_enabled)
```
**Verify**: `uv run python -c "from portal.config import settings; print(settings.rtmps_ingest_enabled)"` → `False`.

### Step 2: Mode constants

In `portal/program_relay.py`:
```python
WHIP_MODE = "whip"
RTMPS_MODE = "rtmps"          # VoxBento is an extra RTMPS destination; YouTube is independent
RELAY_MODE = "rtmps_relay"    # VoxBento forwards to YouTube (#749)
RTMPS_MODES = (RTMPS_MODE, RELAY_MODE)
MODES = (WHIP_MODE, RTMPS_MODE, RELAY_MODE)
```
**Verify**: `uv run pytest tests/test_program_relay.py -q` → all pass (no behaviour change yet).

### Step 3: Authorization and session plumbing in `portal/program_ingest.py`

1. `publish_permitted`:
   ```python
   if room.program_ingest_mode == program_relay.RELAY_MODE:
       return protocol == "rtmp" and settings.program_relay_enabled and key_digest_valid(room, digest)
   if room.program_ingest_mode == program_relay.RTMPS_MODE:
       return protocol == "rtmp" and settings.rtmps_ingest_enabled and key_digest_valid(room, digest)
   return protocol == "webrtc" and credential_digest_valid(room, digest)
   ```
   Browser-sender session secrets must **never** authorize RTMPS (only `key_digest_valid`).
2. `reserved_source`: `"rtmpsConn" if room.program_ingest_mode in program_relay.RTMPS_MODES else "webRTCSession"`.
3. `presented_secret`: replace `settings.program_relay_enabled` with `settings.rtmps_ingest_enabled`.
4. `open_sender_session`: preserve the existing relay-specific 409 message for
   `room.program_ingest_mode == program_relay.RELAY_MODE` ("YouTube relay mode owns the floor; switch the room to independent outputs (WHIP) first."),
   and add a separate check for `room.program_ingest_mode == program_relay.RTMPS_MODE` with message
   `"An RTMPS encoder owns the floor; switch the room to WHIP or the browser sender first."`.
   Reason: avoid replacing the relay-specific message with a generic check, while preventing the browser
   sender from rebinding the floor to WHIP while an RTMPS key is still active.
5. Health AAC hint (line ~680): use `in program_relay.RTMPS_MODES`.
6. Do **not** change `relay_request`, `reconcile_once`'s `relay_rooms`, or `program_relay.sync`.

**Verify**: `uv run pytest tests/test_program_ingest.py tests/test_program_relay.py tests/test_program_sender.py -q` → all pass.

### Step 4: Router

In `portal/routers/program_ingest.py`:
1. `IngestChange.mode: Literal["whip", "rtmps", "rtmps_relay"] = "whip"`.
2. `ingest_change`: add, after the relay 409,
   ```python
   if change.action == "enable" and change.mode == program_relay.RTMPS_MODE and not settings.rtmps_ingest_enabled:
       raise HTTPException(409, "RTMPS ingest must first be enabled by the server operator")
   ```
   Replace `relay_mode = room.program_ingest_mode == program_relay.RELAY_MODE` with
   `rtmps_mode = room.program_ingest_mode in program_relay.RTMPS_MODES` and use it for
   `body["stream_key"]`.
3. `ingest_status`: add `"rtmps_available": settings.rtmps_ingest_enabled,`. Keep
   `relay_available` unchanged. Return `rtmps_server` when `settings.rtmps_ingest_enabled` (already
   driven by `program_rtmps_public_base`; leave `_rtmps_server` as is).

**Verify**: `uv run pytest tests/test_program_ingest.py tests/test_program_relay.py -q` → all pass.

### Step 5: Tests

Create `tests/test_program_rtmps.py` modelled on `tests/test_program_relay.py`
(copy its fixture shape; set `program_ingest_enabled=True`, `program_rtmps_enabled=True`,
`program_relay_enabled=False`, `program_rtmps_public_base="rtmps://ingest.example.test:1936"`).
Cases:
1. `enable` with `mode="rtmps"` returns `secret` and `stream_key == "floor?key=<secret>"`; room mode is `rtmps`.
2. `enable` with `mode="rtmps"` when `program_rtmps_enabled=False` and relay off → 409.
3. `publish_permitted(room, "rtmp", digest_of_key)` → True; `("webrtc", same)` → False.
4. A browser-sender session secret digest is rejected for `"rtmp"` in `rtmps` mode.
5. `presented_secret` parses `floor?key=X` for `protocol="rtmp"` when only `program_rtmps_enabled`.
6. `open_sender_session` → 409 while room is `program_ingest` in `rtmps` mode.
7. `relay_request(room, …)` returns `None` for `rtmps` mode (no relay, even with a destination stored).
8. `ingest_status` JSON has `rtmps_available: True` and `relay_available: False`.
9. Regression: existing relay-mode test for `publish_permitted` still passes (already in `test_program_relay.py`).

**Verify**: `uv run pytest tests/test_program_rtmps.py -q` → 8 passed.

### Step 6: UI

`_program_ingest.html`:
1. Add a third radio between the existing two:
   `<input type='radio' name='program-mode' value='rtmps' id='program-mode-rtmps'>` labelled
   **"Add VoxBento as an extra destination"** with help text "StreamYard, Restream, vMix,
   Zoom/Teams live streaming or an OBS multi-output plugin keep sending to YouTube and
   also send to VoxBento." plus `<small id='program-rtmps-unavailable' class='program-mode-note' hidden>Not switched on for this server yet. Ask the server operator to run <code>scripts/setup-youtube-relay.sh --ingest-only</code>.</small>`.
2. Panels shared by both RTMPS modes (server/key display, "Copy full URL", advanced
   details) change from `data-mode-panel='rtmps_relay'` to `data-mode-panel='rtmps rtmps_relay'`.
   Relay-only panels (YouTube destination form, *VoxBento → YouTube* health row, YouTube
   preview note, relay signal-path diagram) keep `data-mode-panel='rtmps_relay'`.
3. Add a signal-path block `data-mode-panel='rtmps'` with aria-label
   "Your streaming tool sends to YouTube and, separately, an RTMPS stream to VoxBento for captions, translations, and speech".

`program-ingest.js`:
1. Panel toggle: `panel.hidden = !panel.dataset.modePanel.split(' ').includes(mode);`.
2. `modeLabels.rtmps = 'Streaming tool sends VoxBento an extra stream'`; action labels for
   `rtmps` same as `rtmps_relay` (`New stream key`, `Revoke stream key`, `Stream key expires`).
3. Disable the `rtmps` radio when `!data.rtmps_available`; show `#program-rtmps-unavailable`.
   Block `enable` when `rtmps` selected and unavailable (mirror the `relayBlocked` logic).
4. Wherever the code tests `mode === 'rtmps_relay'` to show **server/key** UI, use
   `['rtmps', 'rtmps_relay'].includes(mode)`; keep relay-health/YouTube-live logic relay-only.
   In `renderSteps(data)`, update the non-relay branch so connection and monitoring completion
   checks `data.enabled && data.mode === selectedMode()` instead of hard-coding `data.mode === 'whip'`,
   allowing non-relay RTMPS mode to mark steps complete while keeping relay-health checks restricted
   to `rtmps_relay`.

**Verify**: `node --check portal/static/js/program-ingest.js` → exit 0;
`grep -c "data-mode-panel='rtmps rtmps_relay'" portal/templates/admin/_program_ingest.html` → ≥ 3.

### Step 7: Deployment files

1. Create `docker-compose.rtmps.yml` containing **only** the `portal` env
   (`PROGRAM_INGEST_ENABLED: "true"`, `PROGRAM_RTMPS_ENABLED: "true"`,
   `PROGRAM_RTMPS_PUBLIC_BASE: "${PROGRAM_RTMPS_PUBLIC_BASE:?…}"`) and the `mediamtx`
   block copied verbatim from `docker-compose.relay.yml` (RTMPS env, cert volume, `1936:1936`).
   Header comment: purpose, "no program-relay service; YouTube stays independent",
   rollback (remove from `COMPOSE_FILE`, `docker compose up -d --remove-orphans`).
2. `docker-compose.relay.yml`: add `PROGRAM_RTMPS_ENABLED: "true"` under portal env. Nothing else.
3. `scripts/setup-youtube-relay.sh`: add `--ingest-only` option (update the usage
   comment lines 2–9). When set: use `docker-compose.rtmps.yml` instead of
   `docker-compose.relay.yml`, skip writing/generating `PROGRAM_RELAY_CONTROL_SECRET`,
   and skip the relay health check; everything else (host, cert discovery,
   `PROGRAM_RTMPS_PUBLIC_BASE`, compose validation) is shared. Branch the final Docker Compose
   startup command on ingest-only mode: start only `mediamtx` and `portal`
   (`docker compose up -d mediamtx portal`), while retaining the existing `program-relay`
   build and startup (`docker compose up -d --build program-relay` followed by `mediamtx portal`)
   for normal relay mode. If `COMPOSE_FILE` already contains the relay file, keep it (relay
   implies RTMPS) and print that ingest-only is already covered.

**Verify**: `bash -n scripts/setup-youtube-relay.sh` → exit 0; the Compose command
from the table → exit 0.

### Step 8: Docs and invariant wording (needs maintainer approval in the PR)

- `agents.md` Core Invariant 3: change "and its operator-enabled relay mode (#749)
  accepts room-authenticated RTMPS (never plaintext RTMP) and pushes it…" to state that
  operator-enabled RTMPS ingest accepts room-authenticated RTMPS (never plaintext RTMP),
  either as an independent extra destination or, in relay mode (#749), pushed to an
  allow-listed YouTube RTMPS destination by `program-relay`. Keep every other clause.
- `docs/program-stream-ingest.md`: replace "RTMP/SRT encoder outputs are not supported in
  this version" with the new option and a short setup list (Server, Stream key,
  platform "Custom RTMP"), linking the platform table from `plans/002-results.md` step 2.
- `docs/program-relay.md` → Server setup: document `--ingest-only`.
- `website/docs/admin/program-ingest.mdx`: new section "Add VoxBento as an extra destination".
- `website/docs/configuration/environment-variables.mdx`: `PROGRAM_RTMPS_ENABLED`.
- `.agents/context/CHANGE_IMPACT_MAP.md` Program Stream Ingest: mention `rtmps` mode.

**Verify**: `grep -rn "PROGRAM_RTMPS_ENABLED" website/docs docs docker-compose.rtmps.yml portal/config.py` → ≥ 4 matches.

## Test plan

- New `tests/test_program_rtmps.py` (8 cases above).
- Existing `tests/test_program_relay.py`, `tests/test_program_ingest.py`,
  `tests/test_program_sender.py` unchanged and passing (proves no loosening of WHIP/relay).
- Manual (record in PR): StreamYard or Restream custom RTMP to a staging room in `rtmps`
  mode → *Encoder → VoxBento: Connected*, captions on the listener page, YouTube output
  from the platform unaffected.

## Done criteria

- [ ] `uv run pytest tests/ -q` exits 0; `tests/test_program_rtmps.py` has ≥ 8 passing tests
- [ ] `uv run ruff check .` exits 0
- [ ] `node --check portal/static/js/program-ingest.js` exits 0
- [ ] `bash -n scripts/setup-youtube-relay.sh` exits 0
- [ ] `grep -n "program_relay_enabled" portal/program_ingest.py` shows only relay-specific uses (`publish_permitted` relay branch, `relay_request`, `reconcile_once`)
- [ ] `git diff --name-only dev...HEAD` lists only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- `plans/002-results.md` shows major platforms (StreamYard and Restream) reject the
  `floor?key=…` key shape. Report the accepted shapes; a key-format change is a
  separate decision (it touches `presented_secret` parsing and OBS docs).
- Maintainers reject the invariant-3 wording change.
- Any test in `tests/test_program_relay.py` must be edited to pass. Relay behaviour
  must not change.
- Making `rtmps` mode work seems to need a DB migration.

## Maintenance notes

- `RTMPS_MODES` is now the single place that decides "RTMPS transport". Any future
  SRT mode (#750) should add its own transport check, not reuse `RTMPS_MODES`.
- Reviewers: confirm session secrets never authorize RTMPS and that relay-only code
  paths still check `RELAY_MODE` exactly.
- Deferred: friendlier key formats for platforms that reject `?` (depends on spike).
