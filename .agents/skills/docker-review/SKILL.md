---
name: docker-review
description: Use this skill to review, debug, or modify VoxBento's Docker configuration. Files: `docker-compose.yml`, `Dockerfile`, `mediamtx.yml`.
---

# Skill: Docker Review

> Use this skill to review, debug, or modify VoxBento's Docker configuration.
> Files: `docker-compose.yml`, `Dockerfile`, `mediamtx.yml`.

---

## Services Overview

```
docker-compose.yml
  ├── portal           → FastAPI app (built from Dockerfile)
  ├── mediamtx         → bluenviron/mediamtx:1
  ├── jitsi-web        → ghcr.io/jitsi/web:stable-11248
  ├── jitsi-prosody    → ghcr.io/jitsi/prosody:stable-11248
  ├── jitsi-jicofo     → ghcr.io/jitsi/jicofo:stable-11248
  └── jitsi-jvb        → ghcr.io/jitsi/jvb:stable-11248

volumes:
  portal-data         → SQLite DB persistence
  jitsi-web-config-rootless
  jitsi-web-storage-rootless
  jitsi-prosody-config-rootless
  jitsi-prosody-plugins-rootless
  jitsi-prosody-storage-rootless
  jitsi-jicofo-config-rootless
  jitsi-jvb-config-rootless
```

---

## Portal Container

**Startup command:**
```bash
sh -c "uv run alembic upgrade head && uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 8000"
```
- Alembic migrations run on **every container start** — idempotent and safe.
- No `--reload` in production (add for local dev by overriding command).

**Volume mounts:**
- `.:/app` — source mount for hot reload.
- `/app/.venv` — anonymous volume preserves container `.venv` (not overwritten by host).
- `portal-data:/data` — SQLite DB persistence.

**Environment variables (required for production):**
| Var | Default | Must override? |
|---|---|---|
| `SECRET_KEY` | `change-me` | ✓ |
| `API_KEY_ENCRYPTION_KEY` | (empty) | ✓ if transcription used |
| `JWT_SECRET` | (empty, falls back to SECRET_KEY) | Recommended |
| `DATABASE_URL` | SQLite `/data/interpretation.db` | ✓ for PostgreSQL |
| `MEDIAMTX_WHIP_BASE` | `http://localhost:8889` | ✓ (must be browser-reachable) |
| `MEDIAMTX_API_BASE` | `http://mediamtx:9997` | Use Docker service name internally |
| `JITSI_DOMAIN` | `jitsi.voxbento.com` | ✓ |
| `JITSI_BASE_URL` | `https://jitsi.voxbento.com` | ✓ |

**Health check:** `GET http://localhost:8000/healthz` — 10s interval, 5s timeout, 3 retries.

---

## MediaMTX Container

**Image:** `bluenviron/mediamtx:1`

**Port mappings:**
- `8888:8888` — HTTP (internal health, Control API accessible via port 9997)
- `8889:8889` — WHIP/WHEP
- `8189:8189/udp` — WebRTC ICE/UDP
- `9997:9997` — Control API
- `8554:8554` — RTSP

**Config:** `./mediamtx.yml:/mediamtx.yml:ro`

Key settings in `mediamtx.yml`:
- `overridePublisher: yes` — allows handoff
- `alwaysAvailable` paths — created dynamically via Control API

---

## Jitsi Stack

- **web:** Host ports 8443/8080 map to rootless container ports 8443/8000. `BOSH_RELATIVE: "true"` ensures relative paths work behind Caddy.
- **prosody:** XMPP server. `JVB_AUTH_PASSWORD` and `JICOFO_AUTH_PASSWORD` must be set in production.
- **jicofo:** Conference focus component.
- **jvb:** Jitsi Video Bridge. `JVB_ADVERTISE_IPS` must be set to the reachable LAN/public IP.
- **hardening:** All four Jitsi containers run rootless with read-only filesystems and writable `/run`/`/tmp` tmpfs mounts.

---

## Networking Notes

- Services communicate by Docker service name: `mediamtx`, `jitsi-prosody`, etc.
- **`MEDIAMTX_WHIP_BASE`** must be the browser-reachable URL (e.g. `https://voxbento.example.com:8889`), not the Docker internal URL. Browsers make WebRTC connections directly to MediaMTX.
- **`MEDIAMTX_API_BASE`** uses Docker internal: `http://mediamtx:9997`.

---

## Common Docker Issues

| Issue | Cause | Fix |
|---|---|---|
| `connection refused` on WHIP | `MEDIAMTX_WHIP_BASE` is Docker-internal URL | Set to public host/IP |
| Jitsi join fails | `JVB_ADVERTISE_IPS` not set for JVB | Set to the reachable host IP |
| DB lost after restart | `portal-data` volume not mounted | Check volume mount in docker-compose |
| Migration error on start | Previous migration state mismatch | Run `uv run alembic downgrade base` then `upgrade head` |
| `API_KEY_ENCRYPTION_KEY` error | Key not set or is default | Set to 32+ char random string |
| Hot reload not working | `--reload` not in command | Override command: `uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --reload` |

---

## Production Hardening Checklist

- [ ] `SECRET_KEY` set to random 32+ char string.
- [ ] `API_KEY_ENCRYPTION_KEY` set if using transcription.
- [ ] At least two active administrator accounts can sign in independently.
- [ ] `DATABASE_URL` set to PostgreSQL.
- [ ] `MEDIAMTX_WHIP_BASE` set to HTTPS public URL.
- [ ] `JITSI_DOMAIN` and `JITSI_BASE_URL` set.
- [ ] `JVB_ADVERTISE_IPS` set (for JVB ICE candidates).
- [ ] `JVB_AUTH_PASSWORD` and `JICOFO_AUTH_PASSWORD` changed from default `changeme`.
- [ ] Remove `.:/app` source mount (use image with baked-in code).
- [ ] Remove `--reload` from uvicorn command.
- [ ] Add Caddy (provided `Caddyfile`) or nginx for TLS termination.
