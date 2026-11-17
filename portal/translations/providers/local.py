from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ctranslate2
    from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

import pycountry
from huggingface_hub.utils import LocalEntryNotFoundError
from tqdm.auto import tqdm

from portal.config import settings
from portal.translations.constants import TranslationProviderEnum
from portal.translations.engine import (
    EngineKind,
    Language,
    TranslationContext,
    TranslationEngine,
    TranslationRows,
    translate_each,
)
from portal.translations.model_languages import fixed_language_model
from portal.translations.providers.base import TranslationProvider
from portal.translations.registry import get_translation_model
from portal.translations.vocabulary import protect_terms, restore_terms

logger = logging.getLogger(__name__)

_download_progress = {}
_progress_lock = threading.Lock()


class ModelDownloadTqdm(tqdm):
    def update(self, n=1):
        """Update download progress percentage for tqdm callbacks."""
        super().update(n)
        if hasattr(self, "total") and self.total is not None and self.total > 10 * 1024 * 1024:
            model_size = getattr(self, "_custom_model_id", "nllb-200-distilled-600M")
            with _progress_lock:
                if model_size not in _download_progress:
                    _download_progress[model_size] = {"n": 0, "total": 0, "rate": 0, "status": "downloading"}

                if self.total >= _download_progress[model_size].get("total", 0):
                    _download_progress[model_size]["n"] = self.n
                    _download_progress[model_size]["total"] = self.total
                    _download_progress[model_size]["rate"] = self.format_dict.get("rate", 0)
                    _download_progress[model_size]["status"] = "downloading"


def _hf_source(model_size: str) -> tuple[str, str]:
    """The Hugging Face repository and revision to load ``model_size`` from.

    A registered model comes from its pinned revision. Any other name is used as the
    repository itself, as before the registry.
    """
    entry = get_translation_model(model_size)
    if entry:
        return entry.hf_repo, entry.hf_revision
    return model_size, "main"


#: Names historically accepted for languages whose official or pycountry name differs.
_LEGACY_LANGUAGE_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "Greek, Modern (1453-)": "el",
        "Greek": "el",
    }
)


def _engine_code(model_size: str, language_code: str, language_name: str) -> str | None:
    """The code ``model_size`` uses for a language, looked up by ISO 639-1 code.

    A model without a registry entry (an operator's own NLLB build, loaded from a path) uses
    the NLLB-200 language codes. A caller that has only the English name gets it resolved
    to its ISO code first; the name may also be the ISO code itself.
    """
    entry = fixed_language_model(TranslationProviderEnum.LOCAL.value, model_size)
    for code in (language_code, language_name):
        engine_code = entry.engine_code(code) if code else None
        if engine_code:
            return engine_code
    iso_code = _LEGACY_LANGUAGE_NAMES.get(language_name)
    if not iso_code and language_name:
        record = pycountry.languages.get(name=language_name)
        iso_code = getattr(record, "alpha_2", None)
    return entry.engine_code(iso_code) if iso_code else None


def trigger_download(model_size: str):
    """Trigger manual download of a translation model repository in the background."""
    logger.info(f"Triggering manual download for {model_size}")

    class ScopedTqdm(ModelDownloadTqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._custom_model_id = model_size

    with _progress_lock:
        # Checked and set under the lock, so a second request for the same model starts no second download.
        if _download_progress.get(model_size, {}).get("status") == "downloading":
            return
        _download_progress[model_size] = {"n": 0, "total": 100, "rate": 0, "status": "downloading"}

    try:
        from huggingface_hub import snapshot_download

        hf_repo_id, rev = _hf_source(model_size)

        snapshot_download(repo_id=hf_repo_id, revision=rev, tqdm_class=ScopedTqdm)
        with _progress_lock:
            _download_progress[model_size]["status"] = "completed"
    except Exception as e:
        logger.error(f"Failed manual download: {e}")
        with _progress_lock:
            _download_progress[model_size]["status"] = "error"


def get_download_progress(model_size: str):
    """Return download/cache status; cache misses are normal, other probe failures warn."""
    with _progress_lock:
        prog = _download_progress.get(model_size)
        if prog:
            return dict(prog)

    # An operator's own build loaded from a folder is already on the server, as get_model_and_tokenizer treats it.
    if os.path.exists(model_size):
        return {"status": "completed", "n": 1, "total": 1, "rate": 0}

    # If not in active download, check if it exists on disk
    try:
        from huggingface_hub import snapshot_download

        hf_repo_id, rev = _hf_source(model_size)

        snapshot_download(repo_id=hf_repo_id, revision=rev, local_files_only=True)
        return {"status": "completed", "n": 1, "total": 1, "rate": 0}
    except LocalEntryNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Local model cache lookup failed (%s); download status unavailable", type(exc).__name__)
        return {}


@dataclass
class ModelEntry:
    model: ctranslate2.Translator
    tokenizer: PreTrainedTokenizer | PreTrainedTokenizerFast
    last_used: float


_loaded_models = {}
_active_translations_per_model = {}
_model_lock = threading.Lock()
_load_lock = threading.Lock()


def increment_model_ref(model_size: str):
    with _model_lock:
        _active_translations_per_model[model_size] = _active_translations_per_model.get(model_size, 0) + 1


def decrement_model_ref(model_size: str):
    with _model_lock:
        if _active_translations_per_model.get(model_size, 0) <= 0:
            logger.warning(f"Reference count underflow for {model_size}! This indicates a bug in tracking.")
            _active_translations_per_model[model_size] = 0
        else:
            _active_translations_per_model[model_size] -= 1


def is_model_loaded(model_size: str) -> bool:
    """Whether ``model_size`` is in memory now (idle models are evicted after an hour)."""
    with _model_lock:
        return model_size in _loaded_models


def get_model_and_tokenizer(model_size: str):
    """Load or retrieve the cached CTranslate2 generator and tokenizer for a local model."""
    with _model_lock:
        if model_size in _loaded_models:
            _loaded_models[model_size].last_used = time.time()
            return _loaded_models[model_size].model, _loaded_models[model_size].tokenizer

    with _load_lock:
        # Double-check inside the load lock
        with _model_lock:
            if model_size in _loaded_models:
                _loaded_models[model_size].last_used = time.time()
                return _loaded_models[model_size].model, _loaded_models[model_size].tokenizer

        logger.info(f"Loading NLLB model: {model_size}")
        import ctranslate2
        import transformers
        from huggingface_hub import snapshot_download

        if os.path.exists(model_size):
            local_model_path = model_size
        else:
            # Only an Admin downloads a model (#1191), so a room translating never fetches one.
            hf_repo_id, rev = _hf_source(model_size)
            local_model_path = snapshot_download(repo_id=hf_repo_id, revision=rev, local_files_only=True)

        tokenizer = transformers.AutoTokenizer.from_pretrained(local_model_path, src_lang="eng_Latn", revision="main")  # nosec
        cpu_count = os.cpu_count() or 4
        intra_threads = min(cpu_count, 2)
        model = ctranslate2.Translator(
            local_model_path,
            device="cpu",
            compute_type="int8",
            inter_threads=1,
            intra_threads=intra_threads,
        )

        with _model_lock:
            _loaded_models[model_size] = ModelEntry(model=model, tokenizer=tokenizer, last_used=time.time())
            return _loaded_models[model_size].model, _loaded_models[model_size].tokenizer


async def eviction_loop():
    while True:
        await asyncio.sleep(60 * 15)  # Check every 15 minutes
        now = time.time()
        to_delete = []
        with _model_lock:
            for size, entry in _loaded_models.items():
                refs = _active_translations_per_model.get(size, 0)
                if refs == 0 and (now - entry.last_used) > 3600:  # 1 hour idle
                    to_delete.append(size)
            for size in to_delete:
                logger.info(f"Evicting idle NLLB model: {size}")
                del _loaded_models[size]
        if to_delete:
            gc.collect()


_eviction_task = None


def start_eviction_loop():
    global _eviction_task
    if _eviction_task is None:
        try:
            loop = asyncio.get_running_loop()
            _eviction_task = loop.create_task(eviction_loop())
        except RuntimeError:
            pass


class LocalProvider(TranslationProvider, TranslationEngine):
    """NLLB through CTranslate2, in the portal process."""

    kind = EngineKind.CT2_SEQ2SEQ

    #: Set once the operator has been told the interpretation settings do not apply here.
    _warned_unsupported_settings = False

    def __init__(self):
        start_eviction_loop()

    async def translate_batch(
        self,
        source: Language,
        targets: Sequence[Language],
        texts: Sequence[str],
        context: TranslationContext,
        *,
        model: str,
        api_key: str | None = None,
    ) -> TranslationRows:
        """One model call per text and target; a language NLLB lacks is ``None`` in its column.

        A call is one forward pass, or two when the model drops a glossary placeholder and
        the text is translated again without it.
        """

        def translate_one(text: str, target: Language) -> Awaitable[str | None]:
            """Translate one text into one target language using the local engine."""
            return self.translate(
                provider_name=TranslationProviderEnum.LOCAL.value,
                text=text,
                target_lang_name=target.name,
                target_lang_code=target.code,
                source_lang_name=source.name,
                source_lang_code=source.code,
                model=model,
                api_key=api_key,
                persona=context.persona,
                style=context.style,
                vocabulary_entries=context.vocabulary_for(target),
            )

        return await translate_each(translate_one, targets, texts)

    async def translate(
        self,
        provider_name: str,
        text: str,
        target_lang_name: str,
        target_lang_code: str,
        source_lang_name: str,
        model: str,
        api_key: str | None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_entries: Sequence[Any] = (),
        source_lang_code: str = "",
    ) -> str | None:
        """Translate a single text using a local sequence-to-sequence model."""
        logger.debug(
            f"[NLLB] translate called: source='{source_lang_name}' -> target='{target_lang_name}' "
            f"model='{model}' text_len={len(text)}"
        )
        # A room left on a model the operator no longer offers stops here. The live demo's model
        # is the operator's own setting, so it works even when rooms are not offered it.
        if not settings.local_translation_allows(model) and model != settings.live_demo_translation_model:
            logger.error("[NLLB] Model %r is not offered on this server (LOCAL_TRANSLATION_MODELS)", model)
            return None
        # NLLB is sequence-to-sequence and has no system-prompt channel, so persona and style
        # cannot be applied here (glossary terms are, through placeholders). Say so once per
        # process rather than letting configured settings look active.
        ignored = [name for name, value in (("persona", persona), ("style", style)) if value]
        if ignored and not LocalProvider._warned_unsupported_settings:
            LocalProvider._warned_unsupported_settings = True
            logger.warning(
                "[NLLB] The local provider ignores %s: NLLB takes no system prompt. "
                "Select a cloud translation provider for these settings to take effect.",
                ", ".join(ignored),
            )

        target_lang_token = _engine_code(model, target_lang_code, target_lang_name)
        source_lang_token = _engine_code(model, source_lang_code, source_lang_name)
        if not target_lang_token:
            logger.error("[NLLB] Model %r does not translate into %r (%s)", model, target_lang_code, target_lang_name)
            return None
        if not source_lang_token:
            logger.error("[NLLB] Model %r does not translate from %r (%s)", model, source_lang_code, source_lang_name)
            return None

        logger.debug(
            f"[NLLB] Language tokens resolved: source_token='{source_lang_token}' target_token='{target_lang_token}'"
        )
        segment = protect_terms(text, vocabulary_entries)
        if segment.target_terms:
            translated = await asyncio.to_thread(
                self._run_inference, segment.text, source_lang_token, target_lang_token, model
            )
            if translated is None:
                return None
            restored = restore_terms(translated, segment)
            if restored is not None:
                return restored
            # Never show a placeholder to listeners: translate the original segment instead.
            logger.debug("[NLLB] Glossary placeholders did not survive translation; using the plain translation")
        return await asyncio.to_thread(self._run_inference, text, source_lang_token, target_lang_token, model)

    def _run_inference(self, text: str, source_lang_token: str, target_lang_token: str, model_size: str) -> str | None:
        if not text or not text.strip():
            return ""
        increment_model_ref(model_size)
        try:
            model, tokenizer = get_model_and_tokenizer(model_size)
            source = tokenizer.convert_ids_to_tokens(tokenizer.encode(text.strip()))
            if not source:
                return ""

            # The NLLB tokenizer hardcodes eng_Latn as the first token based on how we initialized it.
            # We must replace it with the actual source language token for the model to translate correctly.
            source[0] = source_lang_token
            # beam_size=1 provides 3x-4x speedup with high accuracy for real-time text
            results = model.translate_batch(
                [source],
                target_prefix=[[target_lang_token]],
                beam_size=1,
                max_decoding_length=256,
            )
            if not results or not results[0].hypotheses:
                return ""
            target = results[0].hypotheses[0]
            # Strip target language prefix token if present
            if target and target[0] == target_lang_token:
                target = target[1:]
            translated_text = tokenizer.decode(tokenizer.convert_tokens_to_ids(target))
            return translated_text.strip()

        except LocalEntryNotFoundError:
            logger.error(
                "[NLLB] Model %r is not downloaded. An Admin must download it on the Translation models page.",
                model_size,
            )
            return None
        except Exception as e:
            import traceback

            logger.error(
                f"[NLLB] _run_inference failed: {e}\n"
                f"source_token={source_lang_token}, target_token={target_lang_token}, model={model_size}\n"
                f"{traceback.format_exc()}"
            )
            return None
        finally:
            decrement_model_ref(model_size)
