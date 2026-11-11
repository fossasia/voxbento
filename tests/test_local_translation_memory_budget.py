from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from portal.translations.providers import local
from portal.translations.providers.local import (
    LocalProvider,
    ModelEntry,
    ModelMemoryBudgetError,
    make_room_for,
)

#: Registry sizes for the fake models used here, in MB.
SIZES = {"model-a": 600, "model-b": 600, "model-c": 600, "model-d": 1000, "huge": 5000}


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    """Start every test with no loaded models, a 1300 MB budget and a registry of known sizes."""
    monkeypatch.setattr(local.settings, "local_translation_memory_mb", 1300)
    monkeypatch.setattr(
        local,
        "get_translation_model",
        lambda model_id: SimpleNamespace(approx_ram_mb=SIZES[model_id]) if model_id in SIZES else None,
    )
    with (
        patch.dict(local._loaded_models, {}, clear=True),
        patch.dict(local._active_translations_per_model, {}, clear=True),
    ):
        yield


def _load(name: str, last_used: float) -> None:
    local._loaded_models[name] = ModelEntry(model=object(), tokenizer=object(), last_used=last_used)


def test_no_budget_means_no_limit(monkeypatch):
    monkeypatch.setattr(local.settings, "local_translation_memory_mb", 0)
    _load("model-a", 1)
    _load("model-b", 2)

    make_room_for("model-c")

    assert set(local._loaded_models) == {"model-a", "model-b"}


def test_a_model_that_fits_evicts_nothing():
    _load("model-a", 1)

    make_room_for("model-b")  # 600 + 600 MB is within 1300 MB

    assert set(local._loaded_models) == {"model-a"}


def test_loading_past_the_budget_evicts_the_least_recently_used_idle_model():
    _load("model-a", last_used=1)
    _load("model-b", last_used=2)

    make_room_for("model-c")  # 600 + 600 + 600 MB is over 1300 MB

    assert set(local._loaded_models) == {"model-b"}


def test_a_model_in_use_is_never_evicted():
    _load("model-a", last_used=1)  # the oldest, but it is translating
    _load("model-b", last_used=2)
    local._active_translations_per_model["model-a"] = 1

    make_room_for("model-c")

    assert set(local._loaded_models) == {"model-a"}


def test_loading_is_refused_when_every_loaded_model_is_in_use():
    _load("model-a", 1)
    _load("model-b", 2)
    local._active_translations_per_model.update({"model-a": 1, "model-b": 2})

    with pytest.raises(ModelMemoryBudgetError, match="in use"):
        make_room_for("model-c")

    assert set(local._loaded_models) == {"model-a", "model-b"}


def test_a_model_bigger_than_the_whole_budget_is_refused_without_evicting_anything():
    _load("model-a", 1)

    with pytest.raises(ModelMemoryBudgetError, match="more than the 1300 MB"):
        make_room_for("huge")

    assert set(local._loaded_models) == {"model-a"}


def test_nothing_is_evicted_when_evicting_the_idle_models_would_still_not_make_room():
    _load("model-a", 1)  # idle
    _load("model-b", 2)  # in use
    local._active_translations_per_model["model-b"] = 1

    with pytest.raises(ModelMemoryBudgetError):
        make_room_for("model-d")  # 1000 MB: even without model-a, 600 + 1000 is over 1300

    assert set(local._loaded_models) == {"model-a", "model-b"}


@pytest.mark.parametrize(
    ("gpus", "device", "compute_type"),
    [(1, "cuda", "auto"), (0, "cpu", "int8")],
)
def test_the_model_is_loaded_on_the_gpu_when_there_is_one(tmp_path, gpus, device, compute_type):
    created = {}

    class FakeTranslator:
        def __init__(self, path, **kwargs):
            created.update(kwargs)

    fake_ctranslate2 = SimpleNamespace(get_cuda_device_count=lambda: gpus, Translator=FakeTranslator)
    fake_transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: object()))

    # A path that exists is loaded as it is, so nothing is downloaded.
    with patch.dict(sys.modules, {"ctranslate2": fake_ctranslate2, "transformers": fake_transformers}):
        local.get_model_and_tokenizer(str(tmp_path))

    assert created["device"] == device
    assert created["compute_type"] == compute_type


def test_a_refused_load_is_reported_clearly_and_the_reference_is_released(caplog):
    provider = LocalProvider()
    local._active_translations_per_model["model-a"] = 0

    with (
        patch.object(local, "get_model_and_tokenizer", side_effect=ModelMemoryBudgetError("no room for model-a")),
        caplog.at_level("ERROR"),
    ):
        result = provider._run_inference("Hello", "eng_Latn", "fra_Latn", "model-a")

    assert result is None
    assert "Cannot load model model-a: no room for model-a" in caplog.text
    assert local._active_translations_per_model["model-a"] == 0
