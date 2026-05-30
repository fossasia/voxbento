# GitHub Copilot Instructions — Eventyay Interpretation Portal

Canonical project policy is in [`../agents.md`](../agents.md). Read that first.

## Copilot-Specific Defaults

- Match the existing code style in the file you are editing.
- For Python: follow the patterns in `fastapi_app.py` and `portal/`. Keep `from __future__ import annotations` at the top of every Python file.
- For JavaScript: plain ES modules in `static/js/`. No frameworks, no build step, no jQuery, no inline scripts.
- Prefer minimal, local edits. Do not refactor code that is not directly related to the current task.
- If you are unsure whether a change is safe, leave a comment rather than guessing.

## What this repo is

A browser-first interpretation booth console for Eventyay live events.

**Stack:** FastAPI (ASGI/uvicorn) + MediaMTX (WHIP/HLS) + self-hosted Jitsi Meet. No Flask, no Socket.IO, no aiortc.

- Interpreters monitor the floor session via an embedded Jitsi iframe (self-hosted)
- Interpreters broadcast audio via browser WebRTC → WHIP → MediaMTX → HLS
- Attendees listen via the `/listen/{booth_id}` page (hls.js with auto-recovery)
- Coordination (booth state, roles, chat) is via native WebSocket on FastAPI

**Frontend:** Jinja2 templates + vanilla ES module JavaScript in `templates/` and `static/js/`. No Vue, no React, no build step. The `src/` directory has been removed.

## Non-negotiable rules

1. No OBS/RTMP/external encoder requirements.
2. Jitsi is monitoring only — not the ingest transport.
3. Exactly one active interpreter publishes per language channel.
4. Never route interpreter mic audio to `AudioContext.destination`.
5. No jQuery, no inline `<script>` blocks.
6. Do not change `uv.lock` unless you run `uv sync --python 3.13 --dev` and all tests pass.
7. Use `eventyay.*` or `portal.*` imports for new code.

Do not duplicate or override project rules in this file. Use `agents.md` as the single source of truth.
