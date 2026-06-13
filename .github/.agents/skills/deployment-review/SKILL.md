# Skill: Deployment Review

> Use this skill to review deployments, validate production readiness, or assist with deployment procedures.
> Reference: `DEPLOYMENT_GUIDE.md`, `docker-compose.yml`, `Caddyfile`, `Dockerfile`.

---

## Pre-Deployment Checklist

### Code Validation
```bash
uv sync --python 3.13 --dev
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
node --check static/js/whep-listener.js
uv run alembic upgrade head
```

### Environment Variables (must be set in production)
| Variable | Requirement |
|---|---|
| `SECRET_KEY` | 32+ char random string — NOT `change-me` |
| `API_KEY_ENCRYPTION_KEY` | 32+ char random string — required if transcription API keys are stored |
| `ADMIN_PASSWORD` | Set — empty string disables admin login |
| `JWT_SECRET` | Recommended — otherwise falls back to `SECRET_KEY` |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host/db` for production |
| `MEDIAMTX_WHIP_BASE` | Browser-reachable HTTPS URL (e.g. `https://media.example.com:8889`) |
| `JITSI_DOMAIN` | Hostname of self-hosted Jitsi |
| `JITSI_BASE_URL` | Full HTTPS URL of Jitsi |
| `DOCKER_HOST_ADDRESS` | Host LAN/public IP for JVB ICE candidates |
| `JVB_AUTH_PASSWORD` | Change from `changeme` |
| `JICOFO_AUTH_PASSWORD` | Change from `changeme` |

### Database
- Run `uv run alembic upgrade head` on first deploy and after any migration PR.
- Verify `portal-data` Docker volume is mounted and persistent.
- For PostgreSQL: ensure `asyncpg` is available (`included in SQLAlchemy[asyncio]` dependency).

---

## Startup Sequence

```
1. docker-compose up mediamtx jitsi-* -d
2. docker-compose up portal -d
   → portal container runs: alembic upgrade head && uvicorn ...
3. Verify: curl http://localhost:8000/healthz
   → expected: {"ok": true, "server": "fastapi", "mediamtx_ok": true}
4. Open admin panel: http://localhost:8000/admin/
5. Create event → room → booths → invite tokens
6. Distribute invite links to interpreters
```

---

## Reverse Proxy (Caddy)

`Caddyfile` is provided for TLS termination and routing:
- Portal: `localhost:8000` → HTTPS.
- MediaMTX WHIP/WHEP: `localhost:8889` → exposed via separate subdomain or path.
- Jitsi: `localhost:8443` → HTTPS.

Ensure `MEDIAMTX_WHIP_BASE` in `.env` matches the Caddy-exposed HTTPS URL for MediaMTX.

---

## Health Monitoring

**Endpoint:** `GET /healthz`
```json
{"ok": true, "server": "fastapi", "mediamtx_ok": true}
```
- `ok: false` — FastAPI itself is down.
- `mediamtx_ok: false` — MediaMTX Control API unreachable.

**Docker health check:** configured in `docker-compose.yml` for portal service (10s interval).

---

## Rollback Procedure

1. Stop portal container: `docker-compose stop portal`
2. Revert to previous image/tag or restore `.:/app` source.
3. If DB schema rolled forward: `uv run alembic downgrade -1` (one step back).
4. Restart: `docker-compose up portal -d`

---

## Zero-Downtime Deploy Notes

VoxBento uses **in-memory booth state** (`BoothRegistry`). A portal restart:
- Drops all active WS connections.
- Loses all in-memory participant state (active interpreter, chat history).
- Browser clients will reconnect (WebSocket auto-reconnects in `interpreter-booth.js`).
- MediaMTX stream continues unaffected — WHIP/WHEP connections survive portal restart.
- Recommend deploying during low-traffic periods (between sessions).

---

## Scaling Notes

- Current architecture is **single-process** (module-level `BoothRegistry`).
- Multiple uvicorn workers (`--workers N`) would not share booth state — do NOT use.
- Horizontal scaling requires Redis-backed booth state (see TD-03 in TECHNICAL_DEBT_REPORT.md).
- MediaMTX and Jitsi can be scaled independently.

---

## Post-Deploy Verification

1. `GET /healthz` → `{ok: true, mediamtx_ok: true}`
2. Admin login at `/admin/login`.
3. Create test event + booth + invite token.
4. Open invite link in browser → redirected to booth page.
5. Verify Jitsi iframe loads.
6. Click "Mic Test" → confirm level meter activates.
7. Click "Go Live" → WHIP connects to MediaMTX.
8. Open `/listener/{event_slug}` in second tab → WHEP audio plays.
