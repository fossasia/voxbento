# Plan 013: De-duplicate translation API-key lookup and OpenAI-style LLM calls

> **Executor instructions**: Follow step by step, verify each, honor STOP
> conditions, update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/tts/worker.py portal/translations/worker.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/215</set>

## Why this matters

`_get_translation_api_key` is byte-for-byte identical in
`portal/tts/worker.py` and `portal/translations/worker.py`. Both files also
hand-roll the same OpenAI-compatible chat request (provider→endpoint map, system
prompt, key map). Adding a provider or rotating a key field means editing two
places; they will drift. Consolidating now is cheap and contained.

## Current state

```python
# portal/translations/worker.py:91 AND portal/tts/worker.py:301 — identical
def _get_translation_api_key(self, event, provider):
    key_map = {OPENAI: event.encrypted_translation_openai_api_key, OPENROUTER: ..., GEMINI: ..., ANTHROPIC: ..., GROQ: ...}
    encrypted = key_map.get(provider)
    return decrypt_val(encrypted) if encrypted else None
```

The provider→endpoint map (`api.openai.com`, `openrouter.ai`, `api.groq.com`)
is repeated in both files' LLM calls.

## Commands you will need

| Purpose | Command                                                                                           | Expected           |
| ------- | ------------------------------------------------------------------------------------------------- | ------------------ |
| Tests   | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass           |
| Lint    | `uv run ruff check portal/`                                                                     | All checks passed! |

## Scope

**In scope:** `portal/translations/worker.py`, `portal/tts/worker.py`, a new shared module, `tests/`.
**Out of scope:** Provider list/behavior, Deepgram/Supertonic TTS, schemas. Keep streaming-vs-blocking distinction.

## Git workflow

- Branch: `advisor/013-dedupe-translation-key`. Commit: `Extract shared translation API-key lookup`. Do NOT push.

## Steps

### Step 1: Add shared helper

Create `get_translation_api_key(event, provider) -> str | None` (move the
key_map+decrypt logic) in a new `portal/translations/keys.py`. Both workers
import and delegate to it; keep their methods as one-line wrappers if convenient.

### Step 2 (optional, low risk): unify endpoint map

Expose the OpenAI-compatible `{provider: endpoint}` map from one place and use it
in both. Skip if it risks the streaming path; note in report.

**Verify**: tests command → all pass.

## Test plan

- New: `get_translation_api_key` returns decrypted value for each provider and
  `None` for unset. Existing transcription/translation tests must stay green.

## Done criteria

- [ ] `grep -c "key_map" portal/tts/worker.py portal/translations/worker.py` ≤ 1 total
- [ ] tests command exits 0; new helper tests pass; ruff clean
- [ ] only in-scope files changed; `plans/README.md` updated

## STOP conditions

- Excerpts don't match. Unifying breaks the streaming fallback in `_stream_llm`.

## Maintenance notes

- New providers add one key_map entry in the shared helper only.

