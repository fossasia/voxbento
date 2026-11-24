from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
from huggingface_hub.utils import LocalEntryNotFoundError

from portal.translations.providers.local import (
    LocalProvider,
    ModelEntry,
    _active_translations_per_model,
    _loaded_models,
    eviction_loop,
    get_download_progress,
)


@pytest.mark.anyio
async def test_local_provider_translate_success():
    provider = LocalProvider()
    with patch.object(provider, "_run_inference", return_value="Bonjour") as mock_inference:
        result = await provider.translate(
            provider_name="local",
            text="Hello",
            target_lang_name="French",
            target_lang_code="fr",
            source_lang_name="English",
            model="nllb-200-distilled-600M",
            api_key=None,
        )
        assert result == "Bonjour"
        mock_inference.assert_called_once_with("Hello", "eng_Latn", "fra_Latn", "nllb-200-distilled-600M")


@pytest.mark.anyio
async def test_local_provider_translate_invalid_language():
    provider = LocalProvider()
    result = await provider.translate(
        provider_name="local",
        text="Hello",
        target_lang_name="UnknownLanguage",
        target_lang_code="xx",
        source_lang_name="English",
        model="nllb-200-distilled-600M",
        api_key=None,
    )
    assert result is None


def test_local_provider_ref_count_decrements_on_exception():
    provider = LocalProvider()
    model_size = "test-model-exception"

    _active_translations_per_model[model_size] = 0

    with patch("portal.translations.providers.local.get_model_and_tokenizer", side_effect=ValueError("Mock Error")):
        result = provider._run_inference("Hello", "eng_Latn", "fra_Latn", model_size)
        assert result is None

    assert _active_translations_per_model.get(model_size, 0) == 0


@pytest.mark.anyio
async def test_local_provider_eviction_respects_ref_count():
    model_size = "test-model-eviction"

    # Populate loaded models with an idle timestamp (older than 1 hour)
    _loaded_models[model_size] = ModelEntry(model=None, tokenizer=None, last_used=time.time() - 4000)

    # Active reference prevents eviction
    _active_translations_per_model[model_size] = 1

    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        try:
            await eviction_loop()
        except asyncio.CancelledError:
            pass

    assert model_size in _loaded_models

    # Releasing the reference allows eviction
    _active_translations_per_model[model_size] = 0

    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        try:
            await eviction_loop()
        except asyncio.CancelledError:
            pass

    assert model_size not in _loaded_models


@pytest.mark.anyio
async def test_eviction_loop_does_not_block_on_model_load():
    model_size = "test-model-slow-load"
    if model_size in _loaded_models:
        del _loaded_models[model_size]

    lock_acquired_event = threading.Event()
    release_lock_event = threading.Event()

    def simulated_slow_download(*args, **kwargs):
        # Signal that the background thread is actively holding the load lock
        lock_acquired_event.set()
        # Block until the main thread tells us to release
        release_lock_event.wait(timeout=5.0)
        return model_size

    def background_loader():
        with (
            patch("huggingface_hub.snapshot_download", side_effect=simulated_slow_download),
            patch("ctranslate2.Translator"),
            patch("transformers.AutoTokenizer.from_pretrained"),
        ):
            from portal.translations.providers.local import get_model_and_tokenizer

            get_model_and_tokenizer(model_size)

    # Start the slow model load in a background thread
    t = threading.Thread(target=background_loader)
    t.start()

    # Yield control until the thread firmly acquires the load lock
    while not lock_acquired_event.is_set():
        await asyncio.sleep(0.01)

    start_time = time.time()

    # Run eviction loop for one pass, expecting it to NOT block on the slow download
    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        try:
            await asyncio.wait_for(eviction_loop(), timeout=1.0)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            pytest.fail("Eviction loop timed out because it was blocked by the model loading lock!")

    elapsed = time.time() - start_time
    assert elapsed < 1.0, f"Eviction loop blocked for {elapsed} seconds, indicating lock contention!"

    # Release the background thread so it can finish
    release_lock_event.set()
    t.join()


@pytest.mark.parametrize("error_type", [PermissionError, ImportError, RuntimeError])
def test_download_status_unexpected_probe_failure_warns_without_private_details(error_type, caplog):
    """Unexpected probe failures return no status and log only the exception type."""
    with patch("huggingface_hub.snapshot_download", side_effect=error_type("private model path")):
        assert get_download_progress("uncached-test-model") == {}

    assert f"Local model cache lookup failed ({error_type.__name__})" in caplog.text
    assert "private model path" not in caplog.text


def test_download_status_cache_miss_is_quiet(caplog):
    """An uncached model is an ordinary miss and must not produce a warning."""
    with patch("huggingface_hub.snapshot_download", side_effect=LocalEntryNotFoundError("not cached")):
        assert get_download_progress("uncached-test-model") == {}

    assert "Local model cache lookup failed" not in caplog.text
