"""Ray Serve deployment for NLLB text translation."""

import asyncio
import logging
import os

from ray import serve
from starlette.requests import Request

logger = logging.getLogger(__name__)


def get_hf_repo_and_revision(model_size: str) -> tuple[str, str]:
    if model_size == "nllb-200-distilled-600M":
        return "JustFrederik/nllb-200-distilled-600M-ct2-int8", "302d78f00e6fdb50a1064059df7c392b735e9d05"

    raise ValueError(f"Unsupported model size: {model_size}")


@serve.multiplexed(max_num_models_per_replica=2)
async def get_nllb_model(model_size: str):
    import ctranslate2
    import ray
    import transformers
    from huggingface_hub import snapshot_download

    logger.info(f"Loading NLLB model dynamically: {model_size}")

    if not os.path.exists(model_size):
        try:
            hf_repo_id, rev = get_hf_repo_and_revision(model_size)
            local_model_path = snapshot_download(repo_id=hf_repo_id, revision=rev)
        except Exception as e:
            logger.error(f"Failed to download {model_size} from HuggingFace: {e}")
            local_model_path = model_size
    else:
        local_model_path = model_size

    tokenizer = transformers.AutoTokenizer.from_pretrained(local_model_path, src_lang="eng_Latn", revision="main")  # nosec

    has_gpu = len(ray.get_gpu_ids()) > 0
    device = "cuda" if has_gpu else "cpu"
    compute_type = "float16" if has_gpu else "int8"

    assigned_cpus = int(ray.get_runtime_context().get_assigned_resources().get("CPU", 2))
    intra_threads = max(1, assigned_cpus)

    model = ctranslate2.Translator(
        local_model_path,
        device=device,
        compute_type=compute_type,
        inter_threads=1,
        intra_threads=intra_threads,
    )
    logger.info(f"NLLB model {model_size} loaded successfully.")

    return tokenizer, model


@serve.deployment
class NLLBTranslator:
    """Ray Serve deployment class for the NLLB translation model."""

    def __init__(self):
        """
        Initialize the NLLB model deployment. Models are loaded dynamically via multiplexing.
        """
        pass

    @serve.batch(max_batch_size=20, batch_wait_timeout_s=0.05)
    async def translate_batch(self, requests: list[dict]) -> list[str]:
        """
        Process a batch of translation requests concurrently.

        Args:
            requests: A list of dictionaries containing text and language tokens.

        Returns:
            A list of translated text strings.
        """
        if not requests:
            return []

        model_groups = {}
        for i, req in enumerate(requests):
            ms = req.get("_model_size", "nllb-200-distilled-600M")
            model_groups.setdefault(ms, []).append((i, req))

        results = [""] * len(requests)

        for ms, group in model_groups.items():
            tokenizer, model = await get_nllb_model(ms)

            sources = []
            target_prefixes = []
            group_indices = []

            for i, req in group:
                text = req.get("text", "")
                source_lang_token = req.get("source_lang_token")
                target_lang_token = req.get("target_lang_token")

                if not text.strip():
                    continue

                source = tokenizer.convert_ids_to_tokens(tokenizer.encode(text.strip()))
                if source and source_lang_token:
                    source[0] = source_lang_token

                sources.append(source)
                target_prefixes.append([target_lang_token])
                group_indices.append(i)

            if not sources:
                continue

            loop = asyncio.get_running_loop()
            batch_results = await loop.run_in_executor(
                None,
                lambda m=model, s=sources, p=target_prefixes: m.translate_batch(
                    s,
                    target_prefix=p,
                    beam_size=1,
                    max_decoding_length=256,
                ),
            )

            for src_idx, result in zip(group_indices, batch_results):
                if not result or not result.hypotheses:
                    continue

                target = result.hypotheses[0]
                target_lang_token = requests[src_idx].get("target_lang_token")

                if target and target[0] == target_lang_token:
                    target = target[1:]

                translated_text = tokenizer.decode(tokenizer.convert_tokens_to_ids(target))
                results[src_idx] = translated_text.strip()

        return results

    async def __call__(self, request: Request):
        """
        Handle incoming HTTP requests to the deployment.

        Args:
            request: The Starlette HTTP request containing the JSON payload.

        Returns:
            A dictionary containing the translated text or an error.
        """
        try:
            payload = await request.json()
        except ValueError:
            return {"error": "Invalid JSON payload."}

        if isinstance(payload, dict):
            model_size = serve.get_multiplexed_model_id() or "nllb-200-distilled-600M"
            payload["_model_size"] = model_size
            translated_text = await self.translate_batch(payload)
            return {"translated_text": translated_text}

        return {"error": "Expected JSON dictionary payload."}


translator_app = NLLBTranslator.bind()
