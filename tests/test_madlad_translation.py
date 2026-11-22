"""MADLAD-400 3B as a built-in translation model (#1198)."""

from __future__ import annotations

import pycountry
import pytest

from portal.translations.engine import Language, TranslationContext
from portal.translations.model_languages import language_support_error
from portal.translations.providers import local
from portal.translations.providers.local import LocalProvider, _engine_code
from portal.translations.registry import (
    MADLAD_400_3B,
    NLLB_200_DISTILLED_600M,
    LanguageTagging,
    get_translation_model,
)

MADLAD = "madlad400-3b-mt"


def test_madlad_is_registered_with_a_pinned_revision_and_its_licence():
    assert get_translation_model(MADLAD) is MADLAD_400_3B
    assert MADLAD_400_3B.hf_repo == "Nextcloud-AI/madlad400-3b-mt-ct2-int8"
    assert MADLAD_400_3B.hf_revision == "aa32bbdeba7880eff2096ec044cb155a340a9400"
    assert MADLAD_400_3B.licence == "Apache-2.0"
    assert MADLAD_400_3B.language_tagging is LanguageTagging.TARGET_TAG
    assert not MADLAD_400_3B.supports_prompt


def test_nllb_still_marks_both_languages_with_tokens():
    assert NLLB_200_DISTILLED_600M.language_tagging is LanguageTagging.LANGUAGE_TOKENS


def test_madlad_tags_are_real_iso_codes_in_its_target_tag_form():
    assert len(MADLAD_400_3B.languages) == 163
    for code, tag in MADLAD_400_3B.languages.items():
        assert pycountry.languages.get(alpha_2=code), code
        assert tag == f"<2{code}>"


@pytest.mark.parametrize(("code", "tag"), [("th", "<2th>"), ("en", "<2en>"), ("zh", "<2zh>"), ("he", "<2he>")])
def test_the_local_provider_finds_madlads_tag_for_a_language(code, tag):
    assert _engine_code(MADLAD, code, "") == tag


def test_a_room_language_madlad_lacks_is_refused_on_save():
    # Avestan has an ISO 639-1 code but no MADLAD-400 tag.
    error = language_support_error("local", MADLAD, ["en", "th", "ae"])
    assert error and "Avestan" in error
    assert language_support_error("local", MADLAD, ["en", "th"]) is None


class FakeTokenizer:
    """Tokenizes like MADLAD's T5 tokenizer: the words, then ``</s>``."""

    def __init__(self):
        self.encoded = []

    def encode(self, text):
        self.encoded.append(text)
        return [*text.split(), "</s>"]

    def convert_ids_to_tokens(self, ids):
        return list(ids)

    def convert_tokens_to_ids(self, tokens):
        return list(tokens)

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(t for t in ids if not (skip_special_tokens and t in ("</s>", "<pad>")))


class FakeTranslator:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def translate_batch(self, source, **options):
        self.calls.append((source, options))
        return [type("Result", (), {"hypotheses": [self.output]})()]


def test_madlad_reads_a_target_tag_in_front_of_the_text_and_forces_no_prefix(monkeypatch):
    tokenizer, translator = FakeTokenizer(), FakeTranslator(["สวัสดี", "ครับ", "</s>"])
    monkeypatch.setattr(local, "get_model_and_tokenizer", lambda model: (translator, tokenizer))

    out = LocalProvider()._run_inference("  Hello there ", "<2en>", "<2th>", MADLAD)

    assert out == "สวัสดี ครับ"
    assert tokenizer.encoded == ["<2th> Hello there"]
    [(source, options)] = translator.calls
    assert source == [["<2th>", "Hello", "there", "</s>"]]
    assert "target_prefix" not in options


def test_nllb_inference_still_swaps_in_the_source_token_and_forces_the_target(monkeypatch):
    tokenizer, translator = FakeTokenizer(), FakeTranslator(["fra_Latn", "Bonjour"])
    monkeypatch.setattr(local, "get_model_and_tokenizer", lambda model: (translator, tokenizer))

    out = LocalProvider()._run_inference("Hello", "eng_Latn", "fra_Latn", NLLB_200_DISTILLED_600M.id)

    assert out == "Bonjour"
    [(source, options)] = translator.calls
    assert source == [["eng_Latn", "</s>"]]
    assert options["target_prefix"] == [["fra_Latn"]]


@pytest.mark.anyio
async def test_madlad_translates_english_to_thai_through_the_engine(monkeypatch):
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((text, source_token, target_token, model_size))
        return "สวัสดี"

    monkeypatch.setattr(local.settings, "local_translation_models", MADLAD)
    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    rows = await LocalProvider().translate_batch(
        Language("en", "English"), [Language("th", "Thai")], ["Hello"], TranslationContext(), model=MADLAD
    )

    assert rows == [["สวัสดี"]]
    assert calls == [("Hello", "<2en>", "<2th>", MADLAD)]
