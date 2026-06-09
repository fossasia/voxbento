# Skill: Security Audit

> Use this skill for security reviews of VoxBento code.
> Covers OWASP Top 10 and VoxBento-specific threat model.

---

## Threat Model

| Actor | Capability | Risk |
|---|---|---|
| Anonymous user | Reaches public routes (/, /register, /login, /healthz, /ws/captions/*) | Low |
| Authenticated user | Joins events they are assigned to | Low |
| Malicious invite token holder | Uses token for different booth | Mitigated by WS scope check |
| Rogue interpreter | Tries to go live without being active | Mitigated by `_resolve_whip_url` |
| Admin with access | Full event/user management | Inherently trusted |
| Network attacker | MITM on HTTP connections | Use HTTPS/TLS in production |

---

## Security Checklist

### A01 — Broken Access Control
- [ ] All admin routes use `Depends(require_admin)`.
- [ ] `require_admin` checks `user_token` (is_admin or event_admin) then `admin_token`.
- [ ] WHIP URL only returned to active interpreter (`check_publish_permission` in `BoothRegistry`).
- [ ] WS `booth:set-active`: only coordinator or current active can reassign.
- [ ] WS token scope: `session_token.event_slug + language_code` must match `booth_id`.
- [ ] WS role: `session.granted_role` used — never `data['role']`.
- [ ] Booth page: `granted_role = None` → 403 (not just redirect).

### A02 — Cryptographic Failures
- [ ] `JWT_SECRET` / `SECRET_KEY` are not default values in production.
- [ ] `API_KEY_ENCRYPTION_KEY` is not `"change-this-encryption-key-in-production"` (raises `RuntimeError` if default).
- [ ] API keys stored Fernet-encrypted in DB; never plaintext.
- [ ] Passwords hashed with bcrypt (cost factor determined by bcrypt defaults ~12).
- [ ] `admin_password` / `jwt_secret` not logged.
- [ ] HTTPS enforced by reverse proxy (Caddyfile provided).

### A03 — Injection
- [ ] SQLAlchemy ORM with parameterized queries used throughout — no raw string SQL.
- [ ] Event slug and language code validated by regex before DB write (`validate_event_slug`, `validate_language_code`).
- [ ] Invite token is a 64-char hex string — no user-controlled content in PK.
- [ ] Form input passed to template is escaped by Jinja2 autoescaping.

### A05 — Security Misconfiguration
- [ ] `debug: bool = True` in default settings — must be `False` in production.
- [ ] `BOOTH_ACCESS_TOKEN` unset = no token guard on API; set it in production if API is public.
- [ ] `ADMIN_PASSWORD` must be set; empty string disables admin login (see `fastapi_app.py`).
- [ ] `database_url` default is SQLite — use PostgreSQL in production.
- [ ] `SECRET_KEY: str = 'change-me'` — must be overridden.

### A07 — Identification and Authentication Failures
- [ ] No rate limiting on `/login` or `/register` (see TD-08 in TECHNICAL_DEBT_REPORT.md).
- [ ] `user.is_active` checked on login — deactivated users cannot log in.
- [ ] JWT expiry is enforced (`jwt_expiry_seconds` default 86400 = 24h).
- [ ] Cookie flags: `httponly=True`, `samesite='lax'` on all auth cookies.
- [ ] No `secure=True` on cookies in code — must be added for HTTPS deployments or handled by reverse proxy.

### A10 — Server-Side Request Forgery (SSRF)
- [ ] `_check_mediamtx()` and `_ensure_mediamtx_path()` use `settings.mediamtx_api_base` — hardcoded from config, not user input.
- [ ] Jitsi URL construction: `_make_jitsi_url(base_url, room)` — `base_url` is from settings; `room` is from DB (admin-entered, not end-user).
- [ ] No user-controlled URLs are fetched by the server.

---

## Open Redirect Audit

All redirects in `fastapi_app.py` must use `safe_redirect(url)`:
```python
def safe_redirect(url: str, status_code: int) -> RedirectResponse:
    url = url.replace('\\', '').strip()
    parsed = urlparse(url)
    if url and not parsed.netloc and not parsed.scheme and url.startswith('/'):
        return RedirectResponse(url=url, status_code=status_code)
    return RedirectResponse(url='/', status_code=status_code)
```

Check `next_url` / `next` parameter usage:
```bash
grep -n "next_url\|next=" fastapi_app.py
```
Ensure all uses pass through `safe_redirect`.

---

## Cookie Security Flags

| Cookie | httponly | samesite | secure |
|---|---|---|---|
| `session_token` | ✓ | lax | ✗ (set by reverse proxy TLS) |
| `user_token` | ✓ | lax | ✗ |
| `admin_token` | ✓ | lax | ✗ |

Production hardening: ensure TLS termination at Caddy/nginx level; add `Strict-Transport-Security` header.

---

## Prompt Injection Detection

VoxBento does not directly pass user input to LLM APIs. Transcription providers receive audio (PCM bytes), not text, from the server. There is no LLM chain in the current implementation.

---

## Security Quick Checks
```bash
# Check for raw redirects (should be none)
grep -n "RedirectResponse(url=" fastapi_app.py | grep -v safe_redirect

# Check for debug=True in production settings
grep -n "debug" portal/config.py

# Check JWT secret default
grep -n "change-me\|secret_key" portal/config.py

# Check no inline scripts in templates
grep -rn "<script>" templates/
```
