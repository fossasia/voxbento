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

### `tests/test_fastapi_app.py`

Tests for FastAPI routes and WebSocket event handlers in `fastapi_app.py`.

**HTTP route tests:**

| Test | What it covers |
|---|---|
| `test_healthz_ok` | `/healthz` returns `ok: true` |
| `test_home_redirects_to_demo_booth` | `/` redirects to `/interpreter/demo-booth` |
| `test_interpreter_booth_page_renders` | `/interpreter/<booth_id>` renders the booth template |
| `test_auth_token_no_password` | Token auth with no password configured |
| `test_booth_state_returns_empty_booth` | `/api/booth/<booth_id>/state` returns valid state JSON |
| `test_ingest_status_endpoint` | `/api/interpreter/status/{channel}` returns state |

**WebSocket event tests:**

| Test | What it covers |
|---|---|
| `test_ws_join_receives_joined_and_state` | `booth:join` creates participant and receives `booth:joined` + `booth:state` |
| `test_ws_join_then_leave_broadcasts_state` | `booth:leave` removes participant and broadcasts state |
| `test_ws_chat_message` | `booth:chat` broadcasts message to booth |
| `test_ws_invalid_json_returns_error` | Invalid JSON payload returns `booth:error` |
| `test_ws_unknown_message_type_returns_error` | Unknown message type returns `booth:error` |
| `test_ws_chat_before_join_returns_error` | Chat before joining returns error |
| `test_ws_set_active_before_join_returns_error` | Set-active before joining returns error |
| `test_ws_set_active_missing_target_returns_error` | Set-active without target returns error |
| `test_ws_update_state_active_interpreter` | Active interpreter can update mic/ingest state |
| `test_ws_disconnect_without_leave_auto_removes_participant` | WebSocket disconnect auto-removes participant |
| `test_ws_active_interpreter_can_set_active` | Active interpreter can hand off to backup |
| `test_ws_standby_cannot_set_mic_active` | Standby interpreter cannot set mic active |
| `test_ws_three_way_coordinator_flow` | Full coordinator + two interpreters flow |
| `test_ws_full_flow_join_update_chat_leave` | End-to-end: join → update state → chat → leave |
| `test_ws_auth_required_with_token` | WebSocket rejects invalid tokens when `BOOTH_ACCESS_TOKEN` is set |
| `test_ws_coordinator_can_switch_active_interpreter` | Coordinator can reassign active interpreter |

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

Shared pytest fixtures:\n\n- `anyio_backend` — Configures async test backend (`asyncio`)

---

## Manual end-to-end scenarios

These scenarios require a browser and the FastAPI server running via Docker Compose.

### Scenario 1 — Single interpreter, mic test, go live

**Setup:** `docker compose up` with all 6 services running.

1. Open `http://localhost:8000/interpreter/demo-event/hall-a-fr`.
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

### Scenario 5 — MediaMTX unavailable (coordination-only development)

**Setup:** MediaMTX container is not running.

1. Start the portal with `uv run uvicorn fastapi_app:app`. Check `/healthz` returns `ok: true`.
2. Open the booth.
3. Verify the Go Live button is available but WHIP publish will fail with a connection error.
4. Verify Jitsi monitoring, participant grid, and booth chat all work normally.
5. Verify WebSocket coordination (join/leave/chat/handoff) is unaffected.

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

- Browser-side JavaScript (no browser-based test framework is configured yet).
- MediaMTX WHIP/HLS pipeline (integration test requiring a running MediaMTX instance).
- Multi-worker WebSocket behaviour (requires Redis; not tested in CI).

These are identified gaps for future test coverage.
