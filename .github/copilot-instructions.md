# GitHub Copilot Instructions — Eventyay Interpretation Portal

Canonical project policy is in [`../agents.md`](../agents.md). Read that first.

## Copilot-Specific Defaults

- Match the existing code style in the file you are editing.
- For Python: follow the patterns in `app.py` and `portal/`. Keep `from __future__ import annotations` at the top of every Python file.
- For JavaScript/Vue: follow the Composition API patterns in `src/composables/` and `src/services/`. No Options API, no jQuery, no inline scripts.
- Prefer minimal, local edits. Do not refactor code that is not directly related to the current task.
- If you are unsure whether a change is safe, leave a comment rather than guessing.

## What this repo is

A browser-first interpretation booth console for Eventyay live events. Interpreters monitor the floor session via a Jitsi iframe and broadcast their audio via WebRTC → aiortc ingest → FFmpeg HLS.

**Current phase:** Interpreter Console MVP — mic tests, preflight checklist, ingest wiring. The ingest server infrastructure is a future phase.

## Non-negotiable rules

1. No OBS/RTMP/external encoder requirements.
2. Jitsi is monitoring only — not the ingest transport.
3. Exactly one active interpreter publishes per language channel.
4. Never route interpreter mic audio to `AudioContext.destination`.
5. No jQuery, no inline `<script>` blocks.
6. Do not change `uv.lock` unless you run `uv sync --python 3.13 --dev` and all tests pass.

Do not duplicate or override project rules in this file. Use `agents.md` as the single source of truth.
