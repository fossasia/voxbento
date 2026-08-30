# Plan 007: Add characterization tests for transcription providers and caption aggregator

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- portal/transcription/aggregator.py portal/transcription/providers/base.py portal/transcription/providers/openai.py`
> If any in-scope file changed, compare the "Current state" excerpts below
> before proceeding.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

The transcription pipeline — `CaptionAggregator`, `TranscriptionProvider`
base class, and all five provider implementations — has zero dedicated unit
tests. `test_transcription_concurrency.py` only tests worker lifecycle with
fully-mocked providers; it never exercises real provider or aggregator logic.
Plans 008 and 009 (access-control dedup and N+1 fix) refactor code that
interacts with the transcription subsystem. Characterization tests here provide
a safety net so those refactors don't silently break captioning behaviour.

## Current state

Files and their roles:
- `portal/transcription/aggregator.py` — `CaptionAggregator` class: `handle_partial`, `handle_chunk`, `handle_final`, `handle_clear`. Forced finalization at 50 words or 15 s. State keyed by `booth_id` in `self.states: dict[str, CaptionState]`.
- `portal/transcription/providers/base.py` — `TranscriptionProvider` base class with `process_chunk` and `run_stream`. Also: `pcm_to_wav(pcm_data, sample_rate)` utility function and `ProviderConfig(api_key)` dataclass.
- `portal/transcription/providers/openai.py` — `OpenAIProvider`: `process_chunk` sends WAV to OpenAI REST, `run_stream` handles both batch whisper-1 and realtime websocket.
- `portal/transcription/providers/local.py` — `LocalProvider`: model caching with `_loaded_models`, reference-counting via `increment_model_ref`/`decrement_model_ref`, `get_model(size)`, and LRU eviction.
- `portal/transcription/providers/deepgram.py`, `elevenlabs.py`, `nvidia.py` — similar streaming providers.
- `tests/test_transcription_concurrency.py` — integration-level tests. Uses `MockProvider` class (lines 30–52) that is a good structural pattern to follow for new tests.
- `tests/conftest.py` — sets `API_KEY_ENCRYPTION_KEY` and `BOOTH_ACCESS_TOKEN` in `os.environ`. Registers `anyio` plugin. New test files must set the same env vars or import from conftest.

Key aggregator state facts (from reading `aggregator.py`):
```python
# forced finalization triggers
if state.current_word_count >= 50 or (time.time() - state.utterance_start_time) >= 15.0:
    await self.handle_final(booth_id, text)
    return
```

```python
# handle_final resets state
state.current_utterance = ""
state.utterance_start_time = 0.0
state.current_word_count = 0
```

```python
# handle_chunk splits on sentence-boundary regex
match = re.search(r'^([^.?!]*[.?!]+)(?:\s+|$)', state.current_utterance)
```

## Commands you will need

| Purpose      | Command                                                                                        | Expected on success |
|--------------|------------------------------------------------------------------------------------------------|---------------------|
| All tests    | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385+ passed         |
| New tests    | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_caption_aggregator.py tests/test_transcription_providers.py -v` | all pass            |

## Scope

**In scope** (files to CREATE):
- `tests/test_caption_aggregator.py`
- `tests/test_transcription_providers.py`

**Out of scope** (do NOT touch):
- Any source file under `portal/`
- Existing test files
- `fastapi_app.py`

## Git workflow

- Branch: `advisor/007-transcription-characterization-tests`
- One commit: `tests: add characterization tests for transcription aggregator and providers`

## Steps

### Step 1: Create tests/test_caption_aggregator.py

Create the file with the following test cases. Model the file structure after
`tests/test_booth_state.py` — it uses `from __future__ import annotations`,
`import pytest`, `@pytest.mark.anyio`, and direct class instantiation (no
FastAPI test client needed).

The aggregator receives a `broadcast_callback` coroutine. Use a simple
`list.append` collector as a fake callback:

```python
from __future__ import annotations

import asyncio
import pytest
from portal.transcription.aggregator import CaptionAggregator, CaptionState


async def collect_broadcasts(booth_id, message, received: list):
    received.append((booth_id, message))
```

Tests to write (all `@pytest.mark.anyio`):

**1. `test_handle_partial_emits_partial_status`**
- Create aggregator with a collector callback.
- Call `await aggregator.handle_partial('booth-1', 'Hello world')`.
- Assert received contains one message with `{"type": "caption", "status": "partial", "text": "Hello world"}`.

**2. `test_handle_partial_ignores_whitespace_only_text`**
- Call `await aggregator.handle_partial('booth-1', '   ')`.
- Assert received is empty.

**3. `test_handle_chunk_splits_on_sentence_boundary`**
- Call `await aggregator.handle_chunk('booth-1', 'Hello world. Goodbye.')`.
- Assert received contains at least one `final` message with `text == 'Hello world.'`.

**4. `test_handle_chunk_no_sentence_boundary_emits_partial`**
- Call `await aggregator.handle_chunk('booth-1', 'Hello world without ending')`.
- Assert received contains a `partial` message.

**5. `test_handle_final_resets_state`**
- Call `handle_partial('b', 'some text')` then `handle_final('b', 'some text')`.
- Assert `aggregator.states['b'].current_utterance == ''`.
- Assert `aggregator.states['b'].current_word_count == 0`.

**6. `test_handle_final_emits_final_status`**
- Call `await aggregator.handle_final('booth-1', 'Some finalized text')`.
- Assert received contains one `final` message.

**7. `test_forced_finalization_at_50_words`**
- Call `handle_partial` with a string of exactly 50 words (e.g.
  `' '.join(['word'] * 50)`).
- Assert received contains a `final` message (forced finalization path).

**8. `test_multi_booth_state_isolation`**
- Call `handle_partial('booth-A', 'text A')` and `handle_partial('booth-B', 'text B')`.
- Assert `aggregator.states['booth-A'].current_utterance == 'text A'`.
- Assert `aggregator.states['booth-B'].current_utterance == 'text B'`.

**9. `test_handle_clear_finalizes_pending_partial`**
- Set up partial state by calling `handle_partial('b', 'in progress')`.
- Call `handle_clear('b')`.
- Assert received contains a `final` message with `text == 'in progress'`.

**10. `test_get_metrics_returns_word_count`**
- Call `handle_partial('b', 'one two three')`.
- Call `aggregator.get_metrics('b')`.
- Assert `result['current_word_count'] == 3`.

**Verify**: `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_caption_aggregator.py -v` → 10 tests pass.

### Step 2: Create tests/test_transcription_providers.py

Create the file. All provider tests mock external API calls (httpx, websockets)
so they run without network access. Use `unittest.mock.patch` and `AsyncMock`
(already used in `tests/test_transcription_concurrency.py`).

```python
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from portal.transcription.providers.base import (
    ProviderConfig, pcm_to_wav, TranscriptionProvider
)
```

Tests to write:

**1. `test_pcm_to_wav_produces_valid_wav_header`**
- Call `pcm_to_wav(b'\x00' * 3200, sample_rate=16000)`.
- Assert result starts with `b'RIFF'`.
- Assert `len(result) > 3200` (WAV header overhead added).

**2. `test_provider_config_get_key_returns_api_key`**
- `config = ProviderConfig(api_key='test-key-abc')`.
- Assert `config.get_key() == 'test-key-abc'`.

**3. `test_provider_config_get_key_returns_none`**
- `config = ProviderConfig(api_key=None)`.
- Assert `config.get_key() is None`.

**4. `test_openai_process_chunk_returns_empty_on_missing_key` (no mock needed)**
- `from portal.transcription.providers.openai import OpenAIProvider`.
- `provider = OpenAIProvider()`.
- `config = ProviderConfig(api_key=None)`.
- `result = await provider.process_chunk(b'\x00'*100, 'en', 'whisper-1', config)`.
- Assert `result == ''` (early return on missing key).

**5. `test_openai_process_chunk_calls_api_with_wav`**
- Mock `httpx.AsyncClient.post` to return a response with `status_code=200`
  and `json={"text": "Hello"}`.
- Call `provider.process_chunk(b'\x00'*3200, 'en', 'whisper-1', ProviderConfig(api_key='fake'))`.
- Assert result is `'Hello'`.
- Assert the mock was called with URL containing `openai.com`.

**6. `test_openai_process_chunk_returns_empty_on_api_error`**
- Mock `httpx.AsyncClient.post` to raise `httpx.ConnectError`.
- Call `process_chunk` with a valid config.
- Assert the function raises `Exception` (it re-raises after retries exhausted) — wrap in `pytest.raises(Exception)`.

**7. `test_local_model_ref_counting`**
- `from portal.transcription.providers.local import increment_model_ref, decrement_model_ref, _active_booths_per_model`.
- Call `increment_model_ref('tiny')` twice, assert `_active_booths_per_model['tiny'] == 2`.
- Call `decrement_model_ref('tiny')` once, assert `_active_booths_per_model['tiny'] == 1`.
- Call `decrement_model_ref('tiny')` once, assert `_active_booths_per_model['tiny'] == 0`.
- Call `decrement_model_ref('tiny')` again (underflow case), assert still `== 0`.

**8. `test_local_model_ref_decrement_never_goes_negative`**
- Call `decrement_model_ref('nonexistent-model')` — should not raise.
- Assert `_active_booths_per_model.get('nonexistent-model', 0) == 0`.

**Verify**: `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_transcription_providers.py -v` → 8 tests pass.

### Step 3: Run the full test suite

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, at least 403 passed (385 + 10 + 8 new tests).

## Test plan

These files ARE the test plan. No additional tests needed beyond what is
specified in Steps 1–2.

## Done criteria

- [ ] `tests/test_caption_aggregator.py` exists with 10 tests, all passing
- [ ] `tests/test_transcription_providers.py` exists with 8 tests, all passing
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0 with 403+ passed
- [ ] `git diff --name-only HEAD` shows only the two new test files
- [ ] `plans/README.md` status row updated

## STOP conditions

- Aggregator methods in `portal/transcription/aggregator.py` don't match the
  excerpts above (codebase drifted — verify the forced-finalization threshold
  values and state-reset code before writing tests that assert them).
- `test_openai_process_chunk_calls_api_with_wav` can't be written cleanly
  because `OpenAIProvider` lazily creates its own httpx client via
  `ts.shared_http_client`. If so, set `portal.transcription.shared_http_client`
  to an `AsyncMock` before the test and report that you did.
- Local model tests cause `faster_whisper` to attempt a model download. If so,
  mock `faster_whisper.WhisperModel` in the test and report.

## Maintenance notes

- When a new transcription provider is added, add a corresponding
  `test_<provider>_process_chunk_returns_empty_on_missing_key` test to
  `tests/test_transcription_providers.py`.
- The `test_forced_finalization_at_50_words` test will need updating if
  `CaptionAggregator` thresholds change (currently 50 words or 15 s).
- These are characterization tests — they document current behaviour. If
  behaviour intentionally changes, update the tests in the same commit.
