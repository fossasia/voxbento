# Skill: Production Readiness Review

> Use this skill to evaluate whether VoxBento is ready for production deployment.
> Combines security, deployment, and operational readiness checks.

---

## Security Hardening

- [ ] `SECRET_KEY` ≠ `change-me` (raises no error but is insecure).
- [ ] `API_KEY_ENCRYPTION_KEY` is set and ≥32 chars (raises `RuntimeError` if default).
- [ ] `ADMIN_PASSWORD` is set (empty = admin login disabled — decide deliberately).
- [ ] `DEBUG=false` in production settings (`portal/config.py` `debug: bool`).
- [ ] TLS termination configured (Caddy via `Caddyfile` or nginx).
- [ ] `Strict-Transport-Security` header set by reverse proxy.
- [ ] `secure=True` on session cookies when served over HTTPS (reverse proxy may handle this via `X-Forwarded-Proto`).
- [ ] `JVB_AUTH_PASSWORD` and `JICOFO_AUTH_PASSWORD` changed from `changeme`.
- [ ] Rate limiting on `/login` and `/register` (see TD-08 — NOT YET IMPLEMENTED).

---

## Infrastructure

- [ ] `DATABASE_URL` is PostgreSQL (`postgresql+asyncpg://...`), not SQLite.
- [ ] Database is backed up (automated snapshots).
- [ ] `portal-data` volume is NOT used (SQLite), or DB is external PostgreSQL.
- [ ] `MEDIAMTX_WHIP_BASE` is the public HTTPS URL, not `localhost`.
- [ ] `DOCKER_HOST_ADDRESS` is set to host's public/LAN IP (required for JVB ICE candidates).
- [ ] MediaMTX UDP ICE port (8189) is open and reachable from browser clients.
- [ ] JVB UDP port (10000) is open for Jitsi media.

---

## Observability

- [ ] Portal logs are captured and retained (Docker logging driver or external).
- [ ] `/healthz` endpoint monitored (returns `mediamtx_ok: true`).
- [ ] Alerting on portal restart or MediaMTX unreachability.
- [ ] Transcription worker failure logging monitored (portal logs).

---

## Functional Validation

Run the full test suite:
```bash
uv sync --python 3.13 --dev
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
node --check static/js/whep-listener.js
uv run alembic upgrade head
```

Manual browser check:
1. Admin login + create event + room + booth + invite token.
2. Open invite link → booth page → Jitsi iframe loads → mic test works → go live → WHIP connects.
3. Open listener page → WHEP connects → audio plays.
4. Open second interpreter tab (backup) → coordinator reassigns → first tab becomes backup.
5. Transcription enabled → captions appear on listener page.

---

## Known Production Gaps (from TECHNICAL_DEBT_REPORT.md)

| Item | Impact | Status |
|---|---|---|
| In-memory booth state lost on restart | Active sessions dropped on deploy | Not fixed — deploy in low-traffic window |
| No rate limiting on /login | Brute-force risk | Not fixed |
| No CSRF on admin forms | Low risk with lax cookies | Not fixed |
| Single shared `ADMIN_PASSWORD` | Weaker than per-user admin | Partially mitigated by `is_admin` user flag |
| `_created_paths` cache not invalidated on MTX restart | WHIP may fail | Mitigated by PATCH fallback |

---

## Capacity Planning

| Component | Limitation |
|---|---|
| Transcription workers | `MAX_TOTAL_WORKERS = 10` — hard limit |
| Booth state | Single-process; no horizontal scaling |
| MediaMTX streams | One per language channel; limited by server resources |
| Jitsi participants | Limited by JVB server capacity |
| DB connections | SQLAlchemy async pool (default settings) |

For events with >10 simultaneous transcribed booths: increase `MAX_TOTAL_WORKERS` in `worker.py` or add a settings override.
