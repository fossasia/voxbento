"""Translation quality harness: compare translation models on real sentences (#1200).

Sends FLORES-200 sentences through VoxBento's own translation engines, one at a time as
the worker does for a live room, and reports for each candidate:

- **chrF**: character n-gram F-score against the human reference (0-100, higher is
  better). It needs no word segmentation, so it also works for Thai, Chinese and Japanese.
- **Glossary**: how many glossary probes kept the term the glossary asks for.
- **Failed**: translations that came back empty-handed (error, timeout, or no text).
- **Extra text**: answers that look like more than a translation (several lines, or far
  longer than the reference), which listeners would hear.
- **p50 / p95**: seconds per sentence, after one warm-up call.

Run it locally, for example English to Thai against a built-in model and a model served by
Ollama through the self-hosted endpoint (#1171):

    uv run python tests/translation_quality_harness.py --target th --sentences 100 \\
        --candidate local:nllb-200-distilled-600M \\
        --candidate self_hosted:hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M \\
        --self-hosted-url http://localhost:11434/v1

A candidate is ``provider:model``; everything after the first colon is the model name, as
the provider spells it. ``local`` models are downloaded from their pinned revision when
missing. ``--out`` keeps every source, reference and output as JSON Lines, so a speaker of
the language can read them.

FLORES-200 (CC BY-SA 4.0) is downloaded once into ``--data-dir`` and checked against a
pinned SHA-256. Scores from different ``--sentences`` counts are not comparable.

This is a test tool: run it on its own, never inside a process that is serving live
events. It changes the process's settings to point at the models it compares.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import tarfile
import time
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Run as a script, the repository root is not on the path yet, and Settings need an
# encryption key to load; the harness never stores a secret. Both must be in place before
# ``portal`` is imported.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "translation-quality-harness-only")

from portal.config import settings  # noqa: E402
from portal.translations.engine import Language, TranslationContext  # noqa: E402
from portal.translations.registry import NLLB_200_DISTILLED_600M, get_translation_model  # noqa: E402
from portal.translations.vocabulary import VocabularyEntryInput  # noqa: E402

FLORES_URL = "https://dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz"
FLORES_SHA256 = "b8b0b76783024b85797e5cc75064eb83fc5288b41e9654dabc7be6ae944011f6"
DEFAULT_DATA_DIR = Path.home() / ".cache" / "voxbento" / "flores200"

#: Seconds the worker allows one engine call (``portal.translations.worker``).
HOSTED_TIMEOUT = 12.0
LOCAL_TIMEOUT = 60.0

#: An answer this many times longer than its reference probably carries more than a translation.
EXTRA_TEXT_RATIO = 2.5

#: Names that a glossary keeps as they are, as an event's glossary would for its own names.
#: Each probe sentence uses one; the translation must contain it unchanged.
GLOSSARY_PROBES: tuple[tuple[str, str], ...] = (
    ("VoxBento", "Welcome to the VoxBento session on live interpretation."),
    ("FOSSASIA", "The FOSSASIA Summit brings developers together every year."),
    ("Eventyay", "You can buy your ticket for the conference on Eventyay."),
    ("Jitsi Meet", "Speakers in the other room join through Jitsi Meet."),
    ("Mission Control", "The coordinator chooses the active interpreter in Mission Control."),
    ("Open Event", "Organisers publish the schedule with Open Event."),
)


# ── Scoring ──────────────────────────────────────────────────────────────


def _char_ngrams(text: str, n: int) -> Counter[str]:
    """Character n-grams of ``text`` with whitespace removed, as chrF counts them."""
    chars = "".join(text.split())
    return Counter(chars[i : i + n] for i in range(len(chars) - n + 1))


def corpus_chrf(hypotheses: Sequence[str], references: Sequence[str], *, order: int = 6, beta: float = 2.0) -> float:
    """Corpus-level chrF (Popović, 2015), as ``sacrebleu``'s default ``CHRF()`` computes it.

    Matches and n-gram counts are summed over every sentence. Precision and recall are
    averaged over the n-gram orders that occur in both sides, then combined into one F-score.
    """
    if len(hypotheses) != len(references):
        raise ValueError("hypotheses and references must have the same length")
    stats = [[0, 0, 0] for _ in range(order)]  # hypothesis n-grams, reference n-grams, matches
    for hypothesis, reference in zip(hypotheses, references, strict=True):
        for n in range(1, order + 1):
            hyp, ref = _char_ngrams(hypothesis, n), _char_ngrams(reference, n)
            stats[n - 1][0] += sum(hyp.values())
            stats[n - 1][1] += sum(ref.values())
            stats[n - 1][2] += sum((hyp & ref).values())
    eps = 1e-16  # sacrebleu's stand-in for a ratio over zero n-grams
    precision = recall = 0.0
    effective = 0
    for hyp_count, ref_count, matches in stats:
        precision += matches / hyp_count if hyp_count else eps
        recall += matches / ref_count if ref_count else eps
        effective += bool(hyp_count and ref_count)
    if not effective:
        return 0.0
    precision, recall = precision / effective, recall / effective
    factor = beta**2
    if not precision + recall:
        return 0.0
    return 100 * (1 + factor) * precision * recall / (factor * precision + recall)


def has_extra_text(output: str, reference: str) -> bool:
    """Whether an answer looks like more than the translation: several lines, or far too long."""
    stripped = output.strip()
    return "\n" in stripped or len(stripped) > EXTRA_TEXT_RATIO * max(len(reference.strip()), 1)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


# ── Data ─────────────────────────────────────────────────────────────────


def flores_code(language_code: str) -> str:
    """The FLORES-200 file name for an ISO 639-1 code; NLLB uses the same codes."""
    code = NLLB_200_DISTILLED_600M.engine_code(language_code)
    if not code:
        raise ValueError(f"FLORES-200 has no language for {language_code!r}")
    return code


def ensure_flores(data_dir: Path, *, url: str = FLORES_URL, sha256: str = FLORES_SHA256) -> Path:
    """The extracted ``devtest`` folder, downloading and checking the archive the first time."""
    devtest = data_dir / "flores200_dataset" / "devtest"
    if devtest.is_dir():
        return devtest
    data_dir.mkdir(parents=True, exist_ok=True)
    archive = data_dir / "flores200_dataset.tar.gz"
    if not archive.exists():
        print(f"Downloading FLORES-200 into {data_dir} ...", file=sys.stderr)
        partial = archive.with_suffix(".part")
        urllib.request.urlretrieve(url, partial)  # nosec B310: fixed https URL, checked below
        partial.rename(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != sha256:
        archive.unlink()
        raise RuntimeError(f"FLORES-200 archive checksum mismatch ({digest}); deleted it, try again")
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and "/devtest/" in m.name and ".." not in m.name]
        tar.extractall(data_dir, members=members, filter="data")
    return devtest


def load_pairs(devtest: Path, source: str, target: str, count: int) -> list[tuple[str, str]]:
    """The first ``count`` (source, reference) sentence pairs of the FLORES-200 devtest split."""
    source_file, reference_file = f"{flores_code(source)}.devtest", f"{flores_code(target)}.devtest"
    sources = (devtest / source_file).read_text(encoding="utf-8").splitlines()
    references = (devtest / reference_file).read_text(encoding="utf-8").splitlines()
    if len(sources) != len(references):
        raise RuntimeError("FLORES-200 source and reference files differ in length")
    return list(zip(sources, references, strict=True))[:count]


# ── Candidates ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> Candidate:
        """``provider:model``; the model may itself contain colons (``hf.co/org/repo:Q4_K_M``)."""
        provider, sep, model = value.partition(":")
        if not sep or not provider or not model:
            raise argparse.ArgumentTypeError(f"expected provider:model, got {value!r}")
        return cls(provider.strip(), model.strip())

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def timeout(self) -> float:
        return LOCAL_TIMEOUT if self.provider == "local" else HOSTED_TIMEOUT


#: Translates one text: (source, target, text, context) -> translation or None.
Translate = Callable[[Language, Language, str, TranslationContext], Awaitable[str | None]]


def engine_translate(candidate: Candidate, api_key: str | None) -> Translate:
    """A call into the engine the worker would use for this candidate."""
    from portal.translations.worker import ENGINES

    engine = ENGINES.get(candidate.provider)
    if engine is None:
        raise SystemExit(f"Unknown translation provider {candidate.provider!r}; choose from {sorted(ENGINES)}")

    async def translate(source: Language, target: Language, text: str, context: TranslationContext) -> str | None:
        return await engine.translate_text(source, target, text, context, model=candidate.model, api_key=api_key)

    return translate


def prepare_settings(candidates: Sequence[Candidate], self_hosted_url: str | None) -> None:
    """Offer every candidate model, as an operator would in ``.env``, and fetch missing local ones."""
    local = [c.model for c in candidates if c.provider == "local"]
    if local:
        settings.local_translation_models = ",".join(dict.fromkeys([*settings.local_translation_model_list, *local]))
        from huggingface_hub import snapshot_download

        for model in local:
            entry = get_translation_model(model)
            if entry:
                snapshot_download(repo_id=entry.hf_repo, revision=entry.hf_revision)
    hosted = [c.model for c in candidates if c.provider == "self_hosted"]
    if hosted:
        if not self_hosted_url:
            raise SystemExit("self_hosted candidates need --self-hosted-url (e.g. http://localhost:11434/v1)")
        settings.self_hosted_llm_base_url = self_hosted_url.rstrip("/")
        settings.self_hosted_llm_models = ",".join(hosted)


# ── Running ──────────────────────────────────────────────────────────────


@dataclass
class Result:
    label: str
    outputs: list[str | None] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    glossary_kept: int = 0
    glossary_total: int = 0
    chrf: float = 0.0
    failed: int = 0
    extra_text: int = 0


async def _timed(call: Awaitable[str | None], timeout: float) -> tuple[str | None, float]:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            output = await call
    except TimeoutError:
        output = None
    return output, time.perf_counter() - started


async def evaluate(
    label: str,
    translate: Translate,
    pairs: Sequence[tuple[str, str]],
    source: Language,
    target: Language,
    *,
    timeout: float,
    glossary: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Result:
    """Translate every pair and the glossary probes, one call at a time, and score them."""
    result = Result(label)
    plain = TranslationContext()
    if pairs:  # Warm-up: loading a model is not part of a sentence's latency.
        await _timed(translate(source, target, pairs[0][0], plain), timeout)
    for index, (text, _reference) in enumerate(pairs, 1):
        output, seconds = await _timed(translate(source, target, text, plain), timeout)
        result.outputs.append(output)
        result.seconds.append(seconds)
        if progress and index % 10 == 0:
            progress(f"{label}: {index}/{len(pairs)}")
    references = [reference for _text, reference in pairs]
    result.failed = sum(1 for output in result.outputs if not (output and output.strip()))
    result.extra_text = sum(
        1
        for output, reference in zip(result.outputs, references, strict=True)
        if output and has_extra_text(output, reference)
    )
    result.chrf = corpus_chrf([output or "" for output in result.outputs], references)
    if glossary:
        for term, sentence in GLOSSARY_PROBES:
            entry = VocabularyEntryInput(source_term=term, target_language=target.code, target_term=term)
            context = TranslationContext(vocabulary={target.code: [entry]})
            output, _seconds = await _timed(translate(source, target, sentence, context), timeout)
            result.glossary_total += 1
            result.glossary_kept += bool(output and term in output)
    return result


def render(results: Sequence[Result], source: Language, target: Language, sentences: int) -> str:
    """A Markdown table, best chrF first."""
    lines = [
        f"{source.name} → {target.name}, FLORES-200 devtest, first {sentences} sentences",
        "",
        "| Candidate | chrF | Glossary kept | Failed | Extra text | p50 s | p95 s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(results, key=lambda r: r.chrf, reverse=True):
        glossary = f"{r.glossary_kept}/{r.glossary_total}" if r.glossary_total else "-"
        lines.append(
            f"| `{r.label}` | {r.chrf:.1f} | {glossary} | {r.failed} | {r.extra_text} "
            f"| {percentile(r.seconds, 0.5):.2f} | {percentile(r.seconds, 0.95):.2f} |"
        )
    return "\n".join(lines)


def write_outputs(path: Path, results: Sequence[Result], pairs: Sequence[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as out:
        for r in results:
            for (text, reference), output, seconds in zip(pairs, r.outputs, r.seconds, strict=True):
                record = {
                    "candidate": r.label,
                    "source": text,
                    "reference": reference,
                    "output": output,
                    "seconds": round(seconds, 3),
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")


def _language(code: str) -> Language:
    import pycountry

    found = pycountry.languages.get(alpha_2=code)
    if not found:
        raise argparse.ArgumentTypeError(f"not an ISO 639-1 code: {code!r}")
    return Language(code, found.name)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--candidate", action="append", type=Candidate.parse, required=True, help="provider:model, repeatable"
    )
    parser.add_argument(
        "--source", type=_language, default=_language("en"), help="ISO 639-1 source language (default en)"
    )
    parser.add_argument("--target", type=_language, required=True, help="ISO 639-1 target language, e.g. th")
    parser.add_argument("--sentences", type=int, default=100, help="FLORES-200 devtest sentences to translate")
    parser.add_argument("--self-hosted-url", help="OpenAI-compatible base URL for self_hosted candidates")
    parser.add_argument("--self-hosted-api-key", default=os.environ.get("SELF_HOSTED_LLM_API_KEY"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, help="write every output as JSON Lines here")
    parser.add_argument("--no-glossary", action="store_true", help="skip the glossary probes")
    args = parser.parse_args(argv)
    if not 1 <= args.sentences <= 1012:
        parser.error("--sentences must be between 1 and 1012")
    return args


async def run(args: argparse.Namespace) -> list[Result]:
    pairs = load_pairs(ensure_flores(args.data_dir), args.source.code, args.target.code, args.sentences)
    prepare_settings(args.candidate, args.self_hosted_url)
    results = []
    for candidate in args.candidate:
        api_key = args.self_hosted_api_key if candidate.provider == "self_hosted" else None
        results.append(
            await evaluate(
                candidate.label,
                engine_translate(candidate, api_key),
                pairs,
                args.source,
                args.target,
                timeout=candidate.timeout,
                glossary=not args.no_glossary,
                progress=lambda line: print(line, file=sys.stderr),
            )
        )
        print(render(results[-1:], args.source, args.target, len(pairs)).splitlines()[-1], file=sys.stderr)
    if args.out:
        write_outputs(args.out, results, pairs)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    results = asyncio.run(run(args))
    print(render(results, args.source, args.target, args.sentences))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
