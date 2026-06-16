---
name: route-analysis
description: Use this skill to analyse, audit, or modify HTTP and WebSocket routes in VoxBento. All routes live in `portal/routers/`.
---

# Skill: Route Analysis

> Use this skill to analyse, audit, or modify HTTP and WebSocket routes in VoxBento.
> All routes live in `portal/routers/`.

---

## Route Categories

| Prefix | Type | Auth |
|---|---|---|
| `/` | Public pages | None or cookie-optional |
| `/interpreter/*` | Booth pages | `session_token` or `user_token` cookie |
| `/listener/*` | Listener pages | `session_token` or `user_token` cookie |
| `/join/*` | Invite redemption | None (token in path) |
| `/register`, `/login`, `/logout`, `/account` | User auth | None / `user_token` |
| `/api/*` | REST API | Optional Bearer JWT or `?token=` |
| `/admin/*` | Admin panel | `admin_token` or `user_token` with `is_admin` |
| `/ws/booth/*` | WebSocket coordination | Cookies + optional `?token=` |
| `/ws/captions/*` | Caption WebSocket | None |
| `/static/*` | Static assets | None |
| `/healthz` | Health check | None |

---

## Auth Patterns

### User page routes
```python
payload = get_booth_session(request)   # checks session_token then user_token
if payload is None:
    return safe_redirect(url=f'/login?next={path}', ...)
granted_role = await resolve_booth_role(payload, booth_id)
```

### Admin routes
```python
@app.get('/admin/...', dependencies=[Depends(require_admin)])
```
`require_admin` checks `user_token` (is_admin=True or event_admin membership) then falls back to `admin_token`.

### API routes (optional auth)
```python
_require_access(credentials, token)   # passes if booth_access_token is unset
```

### WebSocket
- Cookies read at connect time: `session_token` then `user_token`.
- Role resolved via `resolve_booth_role(payload, booth_id)`.
- Token scope validated: `session_token.event_slug + language_code` must match the booth.
- Connection rejected with code 4003 on scope mismatch; 4001 on invalid token.

---

## Role Resolution (`portal/auth.py`)

`resolve_booth_role(payload, booth_id)` returns the most privileged applicable role:

1. `super_admin` — if `is_admin=True` in user token.
2. `event_admin` — if user has `EventMembership.role == 'event_admin'` for the booth's event.
3. Role from `BoothMembership` for the specific booth.
4. Role from `EventMembership` for the booth's event.
5. Role embedded in participant `session_token` — but only if `event_slug + language_code` match.
6. Returns `None` if no applicable role found → 403 on page routes.

---

## Redirect Safety

All redirects MUST use `safe_redirect(url, status_code)`:
```python
def safe_redirect(url: str, status_code: int) -> RedirectResponse:
    url = url.replace('\\', '').strip()
    parsed = urlparse(url)
    if url and not parsed.netloc and not parsed.scheme and url.startswith('/'):
        return RedirectResponse(url=url, status_code=status_code)
    return RedirectResponse(url='/', status_code=status_code)  # fallback
```
Never call `RedirectResponse(url=user_input)` directly.

---

## Adding a New Route

1. Add handler to `portal/routers/`.
2. Use correct auth pattern (see above).
3. Use `safe_redirect` for all redirects.
4. Return `templates.TemplateResponse(request, 'template.html', context)` for HTML.
5. Raise `HTTPException` for errors — never return raw error strings.
6. Add test to `tests/test_fastapi_app.py`.

---

## Critical Endpoints Reference

| Endpoint | Key behavior |
|---|---|
| `GET /join/{token}` | Validates + redeems invite token; sets `session_token` cookie; redirects to booth or listener |
| `GET /interpreter/{event_slug}/{language_code}` | Resolves Jitsi URL from DB, relay WHEP, creates MediaMTX path; passes `granted_role` to template |
| `GET /api/events/{slug}/booths/{lang}/whip-url` | Validates event ownership + active-interpreter status before returning WHIP URL |
| `WS /ws/booth/{booth_id}` | Full WebSocket lifecycle; role never from client; scope validated; disconnect cleans up participant |
| `WS /ws/captions/{booth_id}` | Open subscription; receives `caption` + `booth:state`; used by listener page |
| `POST /admin/.../.../transcription-settings` | Updates DB config; if booth live, stops and restarts transcription worker |

---

## Common Route Bugs to Check

- Missing `safe_redirect` → open redirect risk.
- Missing auth dependency on admin route → unauthenticated access.
- `data['role']` used directly from WS message → role injection vulnerability.
- `token` path param passed directly to DB query without validation → injection risk (mitigated by `redeem_invite_token` validation).
- `next_url` / `next` query param used in redirect without `safe_redirect` → open redirect.
