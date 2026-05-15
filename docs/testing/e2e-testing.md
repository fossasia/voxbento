# Testing Guide

This document describes the test suite, what is covered, and how to run manual end-to-end scenarios.

---

## Running the test suite

```bash
uv run pytest
```

Or with verbose output:

```bash
uv run pytest -v
```

Tests are located in `tests/`. All tests must pass before a PR can be merged.

---

## Test files

### `tests/test_app.py`

Tests for Flask routes and Socket.IO event handlers in `app.py`.

**HTTP route tests:**

| Test | What it covers |
|---|---|
| `test_healthz` | `/healthz` returns `ok: true` |
| `test_home_redirect` | `/` redirects to `/interpreter/demo-booth` |
| `test_interpreter_booth_page` | `/interpreter/<booth_id>` renders the booth template |
| `test_booth_state_api` | `/api/booth/<booth_id>/state` returns valid state JSON |
| `test_booth_state_api_token_required` | Returns `403` when token is wrong |
| `test_connect_ingest_missing_fields` | Returns `400` when required fields are missing |
| `test_connect_ingest_not_active` | Returns `403` when requester is not active interpreter |
| `test_disconnect_ingest` | `/api/interpreter/disconnect/{channel}` returns `ok: true` |
| `test_ingest_status` | `/api/interpreter/status/{channel}` returns state |

**Socket.IO event tests:**

| Test | What it covers |
|---|---|
| `test_socket_join_booth` | `booth:join` creates participant and emits `booth:joined` |
| `test_socket_join_invalid_token` | `booth:join` with wrong token emits `booth:error` |
| `test_socket_leave_booth` | `booth:leave` removes participant |
| `test_socket_chat_message` | `booth:chat` broadcasts message to room |
| `test_socket_chat_missing_sender` | `booth:chat` without sender_id emits `booth:error` |
| `test_socket_set_active` | `booth:set-active` reassigns active interpreter |
| `test_socket_disconnect` | Socket disconnect auto-removes participant |

### `tests/test_booth_state.py`

Unit tests for `BoothRegistry` in `portal/booth_state.py`.

| Test | What it covers |
|---|---|
| `test_get_or_create_booth` | Creates booth on first access; returns same instance on second |
| `test_join_first_interpreter_becomes_active` | First interpreter auto-assigned as active |
| `test_join_second_interpreter_is_standby` | Second interpreter does not replace active |
| `test_join_coordinator_not_active` | Coordinator role does not set active interpreter |
| `test_leave_active_interpreter_promotes_next` | Next interpreter promoted on active leave |
| `test_leave_last_participant_resets_booth` | Booth resets when empty |
| `test_set_active_coordinator_can_assign` | Coordinator can reassign active |
| `test_set_active_active_can_hand_off` | Active interpreter can hand off to backup |
| `test_set_active_backup_cannot_self_promote` | Backup cannot promote without authority |
| `test_set_active_non_interpreter_rejected` | Coordinator cannot be set active |
| `test_update_state_non_active_cannot_set_mic` | Only active interpreter can set mic/ingest flags |
| `test_add_chat_message` | Message stored and returned |
| `test_add_chat_empty_body_rejected` | Empty messages rejected |
| `test_add_chat_unknown_sender_rejected` | Unknown sender rejected |

### `tests/conftest.py`

Shared pytest fixtures:

- `client` — Flask test client
- `socketio_client` — Flask-SocketIO test client
- `booth_registry` — Fresh `BoothRegistry` instance
- `settings` — Test settings with `BOOTH_ACCESS_TOKEN` set to `test-token`

---

## Manual end-to-end scenarios

These scenarios require a browser and both the Flask server and Vite dev server running.

### Scenario 1 — Single interpreter, mic test, go live

**Setup:** Flask + Vite running. `aiortc_available = True`.

1. Open `http://localhost:5173/interpreter/demo-event/hall-a-fr` (or the Flask URL).
2. Verify the console loads with all panels visible.
3. Click **Join Monitor Room** — Jitsi iframe loads. Verify `monitoringActive` is checked.
4. Click the headphones checkbox manually. Verify it toggles.
5. Click **Test Mic**. Verify the level meter animates when you speak.
6. After mic test confirms level, verify `micTestComplete` is checked.
7. Verify `ingestReachable` auto-checks after page load.
8. Click **Go Live**. Verify the button changes to **Stop** and the health panel shows connected.
9. Click **Stop**. Verify state resets.

### Scenario 2 — Two interpreters, handoff

**Setup:** Two browser windows (or tabs in incognito).

1. Window A: Open booth as interpreter (auto-becomes active).
2. Window B: Open the same booth URL as interpreter (joins as backup).
3. Verify Window A shows the active badge. Window B shows backup/standby.
4. In Window A: Click **Set Live** on Window B's participant card.
5. Verify Window B's participant card is now marked active.
6. Verify Window A's Go Live button is now disabled (no longer active).
7. In Window B: Click **Go Live** to confirm the handoff.

### Scenario 3 — Coordinator handoff

**Setup:** Three tabs — interpreter (active), interpreter (backup), coordinator.

1. Open interpreter 1 as active.
2. Open interpreter 2 as backup.
3. Open tab 3 with `?role=coordinator`.
4. In the coordinator tab: click **Set Live** on interpreter 2's card.
5. Verify interpreter 2 becomes active across all three tabs.

### Scenario 4 — Booth chat

1. Open two tabs in the same booth.
2. Send a message from tab 1.
3. Verify the message appears in tab 2.
4. Refresh tab 1. Verify chat history is restored from localStorage.

### Scenario 5 — aiortc unavailable (frontend-only development)

**Setup:** Install `aiortc` is not available (e.g., run `uv run python -c "import aiortc"` fails in a stripped env).

1. Start the Flask server. Check `/healthz` returns `"aiortc_available": false`.
2. Open the booth.
3. Verify a warning is shown ("Ingest server not available").
4. Verify Go Live button is disabled.
5. Verify Jitsi monitoring, participant grid, and booth chat all work normally.

---

## CI

The CI pipeline (`.github/workflows/tests.yml`) runs:

```bash
uv sync --python 3.13 --dev
uv run pytest
```

All tests must pass. There is no separate lint step currently; follow the code style of the surrounding files.

---

## What is not tested

- Browser-side Vue components (no Vitest or browser-based test framework is configured yet).
- FFmpeg HLS output (integration test requiring a real media pipeline).
- Multi-worker Socket.IO behaviour (requires Redis; not tested in CI).

These are identified gaps for future test coverage.
