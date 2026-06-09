# Skill: Incident Investigation

> Use this skill to diagnose and resolve production incidents in VoxBento.

---

## Diagnostic Entry Points

### 1. Is the portal up?
```bash
curl http://localhost:8000/healthz
# Expected: {"ok": true, "server": "fastapi", "mediamtx_ok": true}
```

### 2. Is MediaMTX up?
```bash
curl http://localhost:9997/v3/paths/list
# Expected: 200 with paths array
docker-compose ps mediamtx
docker-compose logs mediamtx --tail=50
```

### 3. Portal logs
```bash
docker-compose logs portal --tail=100
# Look for: ERROR, WebSocketDisconnect, DB errors, transcription errors
```

### 4. Active booth state (no API for this — check portal logs or DB)
```bash
# Check live booth count from admin panel: /admin/
# Or query DB directly:
docker-compose exec portal uv run python -c "
import asyncio
from portal.database import get_session, list_events
async def main():
    async with get_session() as s:
        events = await list_events(s)
        for e in events:
            print(e.slug, e.display_name)
asyncio.run(main())
"
```

---

## Common Incident Types

### IC-01: Interpreter cannot "Go Live"

**Symptoms:** "Go Live" button clicked but WHIP fails or spinner never resolves.

**Diagnosis:**
1. Check browser DevTools Network tab for `GET /api/events/{slug}/booths/{lang}/whip-url`.
   - 403? → Interpreter is not the active interpreter. Check `booth:state` WS message.
   - 404? → Booth not in memory. Was the WS `booth:join` message sent successfully?
2. Check MediaMTX reachability: `/healthz` → `mediamtx_ok`.
3. Check `MEDIAMTX_WHIP_BASE` is browser-reachable (not Docker-internal URL).
4. Check WebSocket connection in DevTools → WS tab.

**Fix:** If not active interpreter: coordinator must reassign via `booth:set-active`. If MediaMTX down: `docker-compose restart mediamtx`.

---

### IC-02: Listener hears nothing

**Symptoms:** Listener page loads but WHEP connection shows "disconnected" or audio is silent.

**Diagnosis:**
1. Check interpreter is "Go Live" — `ingest_status == 'connected'` in booth state.
2. Check `MEDIAMTX_WHIP_BASE` in listener page source — must be browser-reachable HTTPS.
3. Browser DevTools → check RTCPeerConnection state in `whep-listener.js`.
4. Check MediaMTX path: `GET http://localhost:9997/v3/paths/get/{event_slug}/{language_code}`.
5. Check `alwaysAvailable: true` on the path — if not set, WHEP fails when no publisher.

**Fix:** Ensure `_ensure_mediamtx_path` was called. Manually: `PATCH http://mediamtx:9997/v3/config/paths/patch/{path}` with `{"alwaysAvailable": true}`.

---

### IC-03: WebSocket disconnects repeatedly

**Symptoms:** Interpreters see connection status flickering; participants disappear and reappear.

**Diagnosis:**
1. Check portal logs for `WebSocketDisconnect`.
2. Check browser DevTools WS tab for close code:
   - 4001: Missing/invalid token.
   - 4003: Token scope mismatch.
   - 1006: Abnormal closure (network issue or portal crash).
3. Check for `asyncio.Lock` deadlock in `portal/booth_state.py` — portal becomes unresponsive.
4. Check for uncaught exceptions in `_handle_*` functions (portal logs).

**Fix:** Restart portal if deadlocked. Fix token scope if 4003 (token was generated for different booth).

---

### IC-04: Transcription not appearing

**Symptoms:** Booth is live, transcription enabled, but no captions appear.

**Diagnosis:**
1. Check `active_workers` state — portal logs should show worker start: `Starting {provider} transcription worker for booth {booth_id}`.
2. Check ffmpeg can reach MediaMTX RTSP: `docker-compose exec portal ffmpeg -rtsp_transport tcp -i rtsp://mediamtx:8554/{event_slug}/{language_code} -f null - -t 5`.
3. Check API key exists and is valid: portal logs for `API key missing` or `Failed to decrypt`.
4. Check `event.transcription_api_enabled` is `True` for external providers.
5. Check `MAX_TOTAL_WORKERS` (10) not exceeded: count workers in portal logs.
6. Check booth DB config: `db_booth.transcription_enabled`, `db_booth.transcription_provider`, `db_booth.transcription_model`.

**Fix:** Restart transcription worker via admin panel → booth detail → transcription settings (re-save). Or call `stop_transcription_worker(booth_id)` + `start_transcription_worker(...)` via debug endpoint if available.

---

### IC-05: Admin login fails

**Symptoms:** `/admin/login` with correct password → still shows error.

**Diagnosis:**
1. Check `ADMIN_PASSWORD` env var is set and not empty.
2. Check `admin_token` cookie is being set (DevTools → Application → Cookies).
3. Check JWT secret is consistent (`settings.effective_jwt_secret`).
4. Alternative: log in as user with `is_admin=True` — uses `/login` + `user_token` cookie.

---

### IC-06: Database errors

**Symptoms:** 500 errors on any page involving DB; portal logs show SQLAlchemy errors.

**Diagnosis:**
1. Check DB URL: `DATABASE_URL` env var.
2. Check migrations are current: `uv run alembic current` should show `head`.
3. If SQLite: check `portal-data` volume is mounted and has write permission.
4. If PostgreSQL: check connection string and DB server availability.

**Fix:** Run `uv run alembic upgrade head`. If corrupt SQLite: restore from backup volume.

---

## Emergency Commands

```bash
# Restart portal only
docker-compose restart portal

# View live logs
docker-compose logs portal -f

# Force-stop all services
docker-compose down

# Full restart
docker-compose up -d

# Apply pending migrations manually
docker-compose exec portal uv run alembic upgrade head

# Check DB migration state
docker-compose exec portal uv run alembic current
```
