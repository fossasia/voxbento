from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ctranslate2
    from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

from tqdm.auto import tqdm

from portal.translations.constants import NLLB_LANGUAGE_MAP
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)

_download_progress = {}
_progress_lock = threading.Lock()


class ModelDownloadTqdm(tqdm):
    def update(self, n=1):
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


def get_hf_repo_and_revision(model_size: str) -> tuple[str, str]:
    hf_repo_id = model_size
    rev = "main"
    if model_size == "nllb-200-distilled-600M":
        hf_repo_id = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
        rev = "302d78f00e6fdb50a1064059df7c392b735e9d05"
    return hf_repo_id, rev


def trigger_download(model_size: str):
    logger.info(f"Triggering manual download for {model_size}")

    class ScopedTqdm(ModelDownloadTqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._custom_model_id = model_size

    with _progress_lock:
        _download_progress[model_size] = {"n": 0, "total": 100, "rate": 0, "status": "downloading"}

    try:
        from huggingface_hub import snapshot_download

        hf_repo_id, rev = get_hf_repo_and_revision(model_size)

        snapshot_download(repo_id=hf_repo_id, revision=rev, tqdm_class=ScopedTqdm)
        with _progress_lock:
            _download_progress[model_size]["status"] = "completed"
    except Exception as e:
        logger.error(f"Failed manual download: {e}")
        with _progress_lock:
            _download_progress[model_size]["status"] = "error"


def get_download_progress(model_size: str):
    with _progress_lock:
        prog = _download_progress.get(model_size)
        if prog:
            return dict(prog)

    # If not in active download, check if it exists on disk
    try:
        from huggingface_hub import snapshot_download

        hf_repo_id, rev = get_hf_repo_and_revision(model_size)

        snapshot_download(repo_id=hf_repo_id, revision=rev, local_files_only=True)
        return {"status": "completed", "n": 1, "total": 1, "rate": 0}
    except Exception:
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


def get_model_and_tokenizer(model_size: str):
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

        if not os.path.exists(model_size):
            try:
                logger.info(f"Starting download of {model_size} from HuggingFace. This may take a few minutes...")

                class ScopedTqdm(ModelDownloadTqdm):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self._custom_model_id = model_size

                with _progress_lock:
                    _download_progress[model_size] = {"n": 0, "total": 100, "rate": 0, "status": "downloading"}

                hf_repo_id, rev = get_hf_repo_and_revision(model_size)

                local_model_path = snapshot_download(repo_id=hf_repo_id, revision=rev, tqdm_class=ScopedTqdm)
                with _progress_lock:
                    _download_progress[model_size]["status"] = "completed"
                logger.info(f"Successfully downloaded {model_size} to {local_model_path}")
            except Exception as e:
                logger.error(f"Failed to download {model_size} from HuggingFace: {e}")
                with _progress_lock:
                    _download_progress[model_size] = {"status": "error"}
                local_model_path = model_size
        else:
            local_model_path = model_size
            with _progress_lock:
                _download_progress[model_size] = {"status": "completed"}

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


class LocalProvider(TranslationProvider):
    def __init__(self):
        start_eviction_loop()

    async def translate(
        self,
        provider_name: str,
        text: str,
        target_lang_name: str,
        target_lang_code: str,
        source_lang_name: str,
        model: str,
        api_key: str | None,
    ) -> str | None:
        target_lang_token = NLLB_LANGUAGE_MAP.get(target_lang_name)
        source_lang_token = NLLB_LANGUAGE_MAP.get(source_lang_name)
        if not target_lang_token:
            logger.error(f"Target language {target_lang_name} not found in NLLB_LANGUAGE_MAP")
            return None
        if not source_lang_token:
            logger.error(f"Source language {source_lang_name} not found in NLLB_LANGUAGE_MAP")
            return None

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

        except Exception as e:
            logger.error(f"NLLB local translation failed: {e}")
            return None
        finally:
            decrement_model_ref(model_size)
