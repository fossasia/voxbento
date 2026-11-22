"""The translation quality harness (#1200), offline: no model, no network."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from translation_quality_harness import (
    GLOSSARY_PROBES,
    Candidate,
    Result,
    candidate_api_keys,
    corpus_chrf,
    engine_translate,
    ensure_flores,
    evaluate,
    has_extra_text,
    load_pairs,
    main,
    parse_args,
    percentile,
    render,
    run,
)

from portal.translations.engine import Language, TranslationContext

ENGLISH = Language("en", "English")
THAI = Language("th", "Thai")


# ── chrF ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hypotheses", "references", "expected"),
    [
        # Values from sacrebleu 2.x ``CHRF().corpus_score``.
        (["the cat sat on a mat"], ["the cat sat on the mat"], 65.9797),
        (["ตอนนี้เรามีหนูอายุ 4 เดือน", "สวัสดี"], ["ขณะนี้เรามีหนูอายุ 4 เดือน", "สวัสดีครับ"], 77.6875),
        (["", "abc"], ["xyz", "abc"], 55.5556),
        # A reference too short for the higher orders: its hypothesis n-grams of those orders do not count.
        (["abcX", "abcd"], ["abc", "abcd"], 96.9626),
        (["abcdefgh", "ab"], ["abcdefgx", "abcdefg"], 51.9886),
    ],
)
def test_chrf_matches_sacrebleu(hypotheses, references, expected):
    assert corpus_chrf(hypotheses, references) == pytest.approx(expected, abs=1e-3)


def test_chrf_is_100_for_identical_text_and_ignores_spaces():
    assert corpus_chrf(["สวัสดี ครับ"], ["สวัสดีครับ"]) == pytest.approx(100.0)


def test_chrf_is_0_when_nothing_matches_or_nothing_came_back():
    assert corpus_chrf(["zzzz"], ["abcd"]) == pytest.approx(0.0, abs=1e-9)
    assert corpus_chrf([""], ["abcd"]) == 0.0


def test_chrf_needs_one_reference_per_hypothesis():
    with pytest.raises(ValueError):
        corpus_chrf(["a"], ["a", "b"])


# ── Small helpers ────────────────────────────────────────────────────────


def test_a_candidate_keeps_colons_in_its_model_name():
    candidate = Candidate.parse("self_hosted:hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")
    assert candidate == Candidate("self_hosted", "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")
    assert candidate.timeout == 12.0
    assert Candidate.parse("local:nllb-200-distilled-600M").timeout == 60.0


@pytest.mark.parametrize("value", ["local", "local:", ":model"])
def test_a_candidate_needs_a_provider_and_a_model(value):
    with pytest.raises(Exception, match="provider:model"):
        Candidate.parse(value)


def test_extra_text_is_several_lines_or_far_longer_than_the_reference():
    assert not has_extra_text("สวัสดีครับ", "สวัสดีครับ")
    assert has_extra_text("สวัสดีครับ\nNote: this is polite.", "สวัสดีครับ")
    assert has_extra_text("Here is the translation into Thai: สวัสดีครับ", "สวัสดีครับ")


def test_percentile_uses_the_nearest_rank():
    assert percentile([], 0.5) == 0.0
    assert percentile([3.0, 1.0, 2.0, 4.0], 0.5) == 2.0
    assert percentile([float(n) for n in range(1, 21)], 0.95) == 19.0


def test_an_unknown_provider_is_named_with_the_choices():
    with pytest.raises(SystemExit, match="Unknown translation provider"):
        engine_translate(Candidate("nope", "x"), None)


# ── FLORES-200 ───────────────────────────────────────────────────────────


def _flores_archive(path: Path, english: list[str], thai: list[str]) -> str:
    """A tiny archive laid out like the real one; returns its SHA-256."""
    with tarfile.open(path, "w:gz") as tar:
        for name, lines in (("eng_Latn", english), ("tha_Thai", thai)):
            data = ("\n".join(lines) + "\n").encode()
            info = tarfile.TarInfo(f"./flores200_dataset/devtest/{name}.devtest")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_flores_is_fetched_checked_and_read_once(tmp_path):
    archive = tmp_path / "source.tar.gz"
    digest = _flores_archive(archive, ["Hello.", "Thank you."], ["สวัสดี", "ขอบคุณ"])
    data_dir = tmp_path / "data"

    devtest = ensure_flores(data_dir, url=archive.as_uri(), sha256=digest)

    assert load_pairs(devtest, "en", "th", 5) == [("Hello.", "สวัสดี"), ("Thank you.", "ขอบคุณ")]
    assert load_pairs(devtest, "en", "th", 1) == [("Hello.", "สวัสดี")]
    archive.unlink()  # Already extracted: no second download.
    assert ensure_flores(data_dir, url=archive.as_uri(), sha256=digest) == devtest


def test_an_interrupted_extraction_is_redone(tmp_path):
    archive = tmp_path / "source.tar.gz"
    digest = _flores_archive(archive, ["Hello."], ["สวัสดี"])
    data_dir = tmp_path / "data"
    # A run stopped after creating the folder but before extracting every language file.
    (data_dir / "flores200_dataset" / "devtest").mkdir(parents=True)
    (data_dir / "flores200_dataset" / "devtest" / "eng_Latn.devtest").write_text("Hello.\n", encoding="utf-8")

    devtest = ensure_flores(data_dir, url=archive.as_uri(), sha256=digest)

    assert load_pairs(devtest, "en", "th", 1) == [("Hello.", "สวัสดี")]


def test_a_flores_archive_with_the_wrong_checksum_is_deleted(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _flores_archive(archive, ["Hello."], ["สวัสดี"])
    data_dir = tmp_path / "data"
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        ensure_flores(data_dir, url=archive.as_uri(), sha256="0" * 64)
    assert not (data_dir / "flores200_dataset.tar.gz").exists()


def test_a_language_flores_lacks_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no language"):
        load_pairs(tmp_path, "en", "xx", 1)


# ── Running candidates ───────────────────────────────────────────────────

PAIRS = [("Good morning.", "อรุณสวัสดิ์"), ("Thank you.", "ขอบคุณ"), ("See you.", "แล้วพบกันใหม่")]


def _scripted(answers: dict[str, str | None], *, slow: str | None = None):
    """A fake engine answering from a table; ``slow`` never answers in time."""
    calls = []

    async def translate(source, target, text, context):
        calls.append((text, dict(context.vocabulary)))
        if text == slow:
            await asyncio.sleep(10)
        if text in answers:
            return answers[text]
        # Glossary probes: keep the glossary term, as a well-behaved engine would.
        entries = context.vocabulary_for(target)
        return f"แปล {entries[0].target_term}" if entries else "แปล"

    return translate, calls


@pytest.mark.anyio
async def test_a_candidate_is_scored_for_quality_failures_extra_text_and_glossary():
    translate, calls = _scripted(
        {"Good morning.": "อรุณสวัสดิ์", "Thank you.": None, "See you.": "แล้วพบกันใหม่\nNote: informal"}
    )
    result = await evaluate("fake", translate, PAIRS, ENGLISH, THAI, timeout=1.0)

    assert result.outputs == ["อรุณสวัสดิ์", None, "แล้วพบกันใหม่\nNote: informal"]
    assert result.failed == 1
    assert result.extra_text == 1
    assert result.glossary_kept == result.glossary_total == len(GLOSSARY_PROBES)
    assert 0 < result.chrf < 100
    # One warm-up call, the sentences without a glossary, then one probe per glossary term.
    assert [text for text, _ in calls[: 1 + len(PAIRS)]] == ["Good morning.", *[text for text, _ in PAIRS]]
    assert all(vocabulary == {} for _, vocabulary in calls[: 1 + len(PAIRS)])
    probe_vocabulary = calls[-1][1]["th"]
    assert [(e.source_term, e.target_term) for e in probe_vocabulary] == [(GLOSSARY_PROBES[-1][0],) * 2]


@pytest.mark.anyio
async def test_a_glossary_term_the_engine_drops_is_not_counted():
    async def translate(source, target, text, context):
        return "แปล"

    result = await evaluate("fake", translate, PAIRS[:1], ENGLISH, THAI, timeout=1.0)
    assert result.glossary_kept == 0
    assert result.glossary_total == len(GLOSSARY_PROBES)


@pytest.mark.anyio
async def test_a_call_that_runs_past_the_workers_timeout_counts_as_failed():
    translate, _calls = _scripted({"Good morning.": "อรุณสวัสดิ์"}, slow="Thank you.")
    result = await evaluate("fake", translate, PAIRS[:2], ENGLISH, THAI, timeout=0.05, glossary=False)
    assert result.outputs == ["อรุณสวัสดิ์", None]
    assert result.failed == 1
    assert result.glossary_total == 0


def test_the_report_puts_the_best_chrf_first():
    worse = Result("local:worse", chrf=20.0, seconds=[1.0], glossary_kept=1, glossary_total=6)
    better = Result("self_hosted:better", chrf=40.0, seconds=[0.5, 1.5])
    table = render([worse, better], ENGLISH, THAI, 2).splitlines()
    assert table[0] == "English → Thai, FLORES-200 devtest, first 2 sentences"
    assert table[4].startswith("| `self_hosted:better` | 40.0 | - |")
    assert table[5].startswith("| `local:worse` | 20.0 | 1/6 |")


def test_main_runs_end_to_end_through_the_worker_engines(tmp_path, monkeypatch, capsys):
    """The command line, the data and the worker's engine table, with a fake engine in it."""
    from portal.translations import worker
    from portal.translations.engine import EngineKind, TranslationEngine

    class Echo(TranslationEngine):
        kind = EngineKind.MT_API

        async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
            return [[{"Hello.": "สวัสดี"}.get(text, text) for _ in targets] for text in texts]

    monkeypatch.setitem(worker.ENGINES, "echo", Echo())
    archive = tmp_path / "flores.tar.gz"
    digest = _flores_archive(archive, ["Hello.", "Thank you."], ["สวัสดี", "ขอบคุณ"])
    monkeypatch.setattr(
        "translation_quality_harness.ensure_flores",
        lambda data_dir: ensure_flores(data_dir, url=archive.as_uri(), sha256=digest),
    )
    out = tmp_path / "outputs.jsonl"

    assert (
        main(
            [
                "--candidate",
                "echo:any",
                "--target",
                "th",
                "--sentences",
                "2",
                "--data-dir",
                str(tmp_path / "d"),
                "--out",
                str(out),
            ]
        )
        == 0
    )

    table = capsys.readouterr().out
    assert "| `echo:any` |" in table
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["source"], r["output"]) for r in records] == [("Hello.", "สวัสดี"), ("Thank you.", "Thank you.")]
    assert all(r["candidate"] == "echo:any" for r in records)


def test_hosted_candidates_need_their_key_before_anything_runs(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    candidates = [Candidate.parse("gemini:gemini-2.5-flash"), Candidate.parse("openai:gpt-4o-mini")]
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        candidate_api_keys(candidates, None)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    assert candidate_api_keys(candidates, None) == {"gemini": "g-test", "openai": "sk-test"}


def test_local_and_self_hosted_candidates_need_no_key():
    candidates = [Candidate.parse("local:nllb-200-distilled-600M"), Candidate.parse("self_hosted:hy-mt")]
    assert candidate_api_keys(candidates, None) == {"local": None, "self_hosted": None}
    assert candidate_api_keys(candidates, "operator-key")["self_hosted"] == "operator-key"


def test_the_self_hosted_key_defaults_to_the_workers_setting(monkeypatch):
    from portal.config import settings

    monkeypatch.setattr(settings, "self_hosted_llm_api_key", "operator-key")
    args = parse_args(["--candidate", "self_hosted:m", "--target", "th"])
    assert args.self_hosted_api_key == "operator-key"
    args = parse_args(["--candidate", "self_hosted:m", "--target", "th", "--self-hosted-api-key", "other"])
    assert args.self_hosted_api_key == "other"


def test_glossary_probes_run_only_for_an_english_source(tmp_path, monkeypatch, capsys):
    from portal.translations import worker
    from portal.translations.engine import EngineKind, TranslationEngine

    class Echo(TranslationEngine):
        kind = EngineKind.MT_API

        async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
            return [[text for _ in targets] for text in texts]

    monkeypatch.setitem(worker.ENGINES, "echo", Echo())
    archive = tmp_path / "flores.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("deu_Latn", "tha_Thai", "eng_Latn"):
            data = b"Hallo.\n"
            info = tarfile.TarInfo(f"./flores200_dataset/devtest/{name}.devtest")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "translation_quality_harness.ensure_flores",
        lambda data_dir: ensure_flores(data_dir, url=archive.as_uri(), sha256=digest),
    )
    base = ["--candidate", "echo:any", "--target", "th", "--sentences", "1", "--data-dir", str(tmp_path / "d")]

    [german] = asyncio.run(run(parse_args([*base, "--source", "de"])))
    assert german.glossary_total == 0
    assert "Skipping the glossary probes" in capsys.readouterr().err
    [english] = asyncio.run(run(parse_args(base)))
    assert english.glossary_total == len(GLOSSARY_PROBES)
