"""The translation model registry (#1185): its entries, the ISO language mapping and the code that reads them."""

from __future__ import annotations

import dataclasses
import re
from unittest.mock import MagicMock, patch

import pycountry
import pytest

from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.engine import EngineKind, Language, TranslationContext
from portal.translations.model_languages import language_support_error
from portal.translations.providers.local import (
    LocalProvider,
    _engine_code,
    _hf_source,
    _loaded_models,
    get_model_and_tokenizer,
    trigger_download,
)
from portal.translations.registry import (
    LOCAL_TRANSLATION_MODELS,
    M2M100_1_2B,
    M2M100_418M,
    NLLB_200_DISTILLED_600M,
    TranslationModel,
    get_translation_model,
)

NLLB = "nllb-200-distilled-600M"


# --- Every entry ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", LOCAL_TRANSLATION_MODELS.values(), ids=lambda model: model.id)
class TestEveryEntry:
    def test_is_registered_under_its_own_id(self, model):
        assert LOCAL_TRANSLATION_MODELS[model.id] is model
        assert get_translation_model(model.id) is model

    def test_is_pinned_to_a_commit_not_a_branch(self, model):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", model.hf_repo)
        assert re.fullmatch(r"[0-9a-f]{40}", model.hf_revision)

    def test_maps_real_iso_639_1_codes(self, model):
        assert model.languages
        unknown = [code for code in model.languages if pycountry.languages.get(alpha_2=code) is None]
        assert unknown == []
        assert all(code == code.lower() and engine_code for code, engine_code in model.languages.items())

    def test_gives_each_language_its_own_engine_code(self, model):
        assert len(set(model.languages.values())) == len(model.languages)

    def test_states_its_size_capabilities_and_licence(self, model):
        assert isinstance(model.engine_kind, EngineKind)
        assert model.approx_ram_mb > 0
        assert isinstance(model.supports_prompt, bool) and isinstance(model.supports_multi_target_batch, bool)
        assert model.licence

    def test_cannot_be_changed(self, model):
        with pytest.raises(dataclasses.FrozenInstanceError):
            model.hf_revision = "main"
        with pytest.raises(TypeError):
            model.languages["xx"] = "xxx_Latn"


def test_the_registry_itself_cannot_be_changed():
    with pytest.raises(TypeError):
        LOCAL_TRANSLATION_MODELS["other"] = NLLB_200_DISTILLED_600M


def test_an_unregistered_model_has_no_entry():
    assert get_translation_model("gpt-4o") is None
    assert get_translation_model("") is None


def test_the_self_hosted_model_list_comes_from_the_registry():
    assert TRANSLATION_MODELS[TranslationProviderEnum.LOCAL.value] == list(LOCAL_TRANSLATION_MODELS)
    assert TRANSLATION_MODELS[TranslationProviderEnum.LOCAL.value][0] == NLLB


# --- The NLLB entry ------------------------------------------------------------------------


def test_nllb_is_described_as_before():
    model = get_translation_model(NLLB)

    assert model is NLLB_200_DISTILLED_600M
    assert model.engine_kind is EngineKind.CT2_SEQ2SEQ is LocalProvider.kind
    assert (model.hf_repo, model.hf_revision) == (
        "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "302d78f00e6fdb50a1064059df7c392b735e9d05",
    )
    assert model.supports_prompt is False
    assert model.supports_multi_target_batch is True
    assert model.licence == "CC-BY-NC-4.0"


def test_nllb_codes_are_the_iso_639_3_code_and_a_script():
    """FLORES-200 codes start with the language's three-letter code; Arabic is Modern Standard Arabic (arb)."""
    for code, engine_code in NLLB_200_DISTILLED_600M.languages.items():
        assert re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", engine_code), engine_code
        expected = "arb" if code == "ar" else pycountry.languages.get(alpha_2=code).alpha_3
        assert engine_code[:3] == expected, code


@pytest.mark.parametrize(
    ("code", "engine_code"),
    [
        ("en", "eng_Latn"),
        ("fr", "fra_Latn"),
        ("de", "deu_Latn"),
        ("es", "spa_Latn"),
        ("zh", "zho_Hans"),
        ("ar", "arb_Arab"),
        ("hi", "hin_Deva"),
        ("th", "tha_Thai"),
        ("ja", "jpn_Jpan"),
        ("nb", "nob_Latn"),
        # The English-name lookup missed these: pycountry and the old table spelled the names differently.
        ("el", "ell_Grek"),
    ],
)
def test_nllb_language_codes(code, engine_code):
    assert NLLB_200_DISTILLED_600M.engine_code(code) == engine_code


def test_engine_code_accepts_a_code_as_users_type_it():
    assert NLLB_200_DISTILLED_600M.engine_code(" FR ") == "fra_Latn"


@pytest.mark.parametrize("code", ["az", "no", "xx", "", "french", "fra"])
def test_engine_code_is_none_for_a_language_the_model_lacks(code):
    assert NLLB_200_DISTILLED_600M.engine_code(code) is None


def test_unsupported_lists_each_missing_language_once_in_order():
    assert NLLB_200_DISTILLED_600M.unsupported(["en", "az", "fr", "xx", "az"]) == ["az", "xx"]
    assert NLLB_200_DISTILLED_600M.unsupported(["en", "fr"]) == []
    assert NLLB_200_DISTILLED_600M.unsupported([]) == []


# --- The local provider reads the registry -------------------------------------------------


def test_a_registered_model_downloads_from_its_pinned_revision():
    assert _hf_source(NLLB) == (NLLB_200_DISTILLED_600M.hf_repo, NLLB_200_DISTILLED_600M.hf_revision)


def test_an_unregistered_model_name_is_used_as_the_repository_as_before():
    assert _hf_source("someone/their-nllb-build") == ("someone/their-nllb-build", "main")


def test_a_download_asks_for_the_registry_entrys_repository_and_revision():
    with patch("huggingface_hub.snapshot_download") as download:
        trigger_download(NLLB)

    assert download.call_args.kwargs["repo_id"] == NLLB_200_DISTILLED_600M.hf_repo
    assert download.call_args.kwargs["revision"] == NLLB_200_DISTILLED_600M.hf_revision


@pytest.mark.parametrize(
    ("code", "name", "engine_code"),
    [
        ("fr", "French", "fra_Latn"),
        # The code decides, whatever the room calls the language.
        ("fr", "Français", "fra_Latn"),
        ("el", "Modern Greek (1453-)", "ell_Grek"),
        # Callers that know only the English name, or pass the code as the name.
        ("", "English", "eng_Latn"),
        ("", "fr", "fra_Latn"),
        ("", "Chinese", "zho_Hans"),
        ("", "Greek, Modern (1453-)", "ell_Grek"),
        ("", "Greek", "ell_Grek"),
        ("xx", "UnknownLanguage", None),
        ("", "", None),
        ("az", "Azerbaijani", None),
    ],
)
def test_the_local_provider_looks_languages_up_by_iso_code(code, name, engine_code):
    assert _engine_code(NLLB, code, name) == engine_code


def test_a_model_loaded_from_a_path_uses_the_nllb_language_codes():
    assert _engine_code("/models/my-nllb-build", "fr", "French") == "fra_Latn"


def test_a_models_own_language_map_is_used_for_it(monkeypatch):
    """Resolve provider language codes from the selected model registry entry."""
    other = dataclasses.replace(NLLB_200_DISTILLED_600M, id="other", languages={"en": "__en__", "fr": "__fr__"})
    monkeypatch.setattr("portal.translations.model_languages.get_translation_model", {"other": other}.get)

    assert _engine_code("other", "fr", "French") == "__fr__"
    assert _engine_code("other", "de", "German") is None


@pytest.mark.anyio
async def test_nllb_translates_from_a_source_given_by_code(monkeypatch):
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((source_token, target_token))
        return "Hallo"

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    result = await LocalProvider().translate(
        provider_name="local",
        text="Bonjour",
        target_lang_name="Deutsch",
        target_lang_code="de",
        source_lang_name="Français",
        source_lang_code="fr",
        model=NLLB,
        api_key=None,
    )

    assert result == "Hallo"
    assert calls == [("fra_Latn", "deu_Latn")]


@pytest.mark.anyio
async def test_nllb_translates_from_legacy_greek_name_only(monkeypatch):
    """Callers passing the legacy pycountry English name without a language code resolve to ell_Grek."""
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((source_token, target_token))
        return "Bonjour"

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    result = await LocalProvider().translate(
        provider_name="local",
        text="Γεια σας",
        target_lang_name="French",
        target_lang_code="",
        source_lang_name="Greek, Modern (1453-)",
        source_lang_code="",
        model=NLLB,
        api_key=None,
    )

    assert result == "Bonjour"
    assert calls == [("ell_Grek", "fra_Latn")]


@pytest.mark.anyio
async def test_local_provider_translate_batch_forwards_source_code(monkeypatch):
    """The batch path forwards source.code so _run_inference receives the token even if source.name is unresolvable."""
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((source_token, target_token))
        return "Hallo"

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)

    source = Language(code="fr", name="CustomFrenchName")
    targets = [Language(code="de", name="German")]
    context = TranslationContext()

    rows = await LocalProvider().translate_batch(source, targets, ["Bonjour"], context, model=NLLB)

    assert rows == [["Hallo"]]
    assert calls == [("fra_Latn", "deu_Latn")]


# --- The M2M100 entries (#1196) ------------------------------------------------------------

M2M100_MODELS = [M2M100_418M, M2M100_1_2B]
#: The languages the issue asks both models to translate to and from English.
M2M100_CHECKED = ["es", "fr", "pt", "de", "zh", "ar", "hi"]


def test_both_m2m100_sizes_can_be_selected_after_nllb():
    assert list(LOCAL_TRANSLATION_MODELS) == [NLLB, "m2m100_418M", "m2m100_1.2B"]
    assert TRANSLATION_MODELS[TranslationProviderEnum.LOCAL.value] == [NLLB, "m2m100_418M", "m2m100_1.2B"]


@pytest.mark.parametrize(
    ("model", "hf_repo", "hf_revision"),
    [
        (M2M100_418M, "jncraton/m2m100_418M-ct2-int8", "7c1b2620a4e58dacecbd8bf89cfd6da7eb9eb7b0"),
        (M2M100_1_2B, "jncraton/m2m100_1.2B-ct2-int8", "e50078df6be13a88592b70ea42f4d74c2082e448"),
    ],
    ids=lambda value: value.id if isinstance(value, TranslationModel) else None,
)
def test_m2m100_is_described_by_its_entry(model, hf_repo, hf_revision):
    assert get_translation_model(model.id) is model
    assert model.engine_kind is EngineKind.CT2_SEQ2SEQ is LocalProvider.kind
    assert (model.hf_repo, model.hf_revision) == (hf_repo, hf_revision)
    assert _hf_source(model.id) == (hf_repo, hf_revision)
    assert model.supports_prompt is False
    assert model.supports_multi_target_batch is True
    assert model.licence == "MIT"
    assert model.tokenizer_type == "m2m_100"


def test_the_m2m100_sizes_share_one_language_map():
    assert M2M100_418M.languages == M2M100_1_2B.languages
    assert M2M100_1_2B.approx_ram_mb > M2M100_418M.approx_ram_mb


def test_m2m100_codes_are_the_iso_code_in_underscores():
    """M2M100 has 100 languages; Asturian, Cebuano, Iloko and Northern Sotho have no ISO 639-1 code."""
    assert len(M2M100_418M.languages) == 96
    assert all(engine_code == f"__{code}__" for code, engine_code in M2M100_418M.languages.items())


@pytest.mark.parametrize("code", ["nb", "nn", "eo", "ast", "ceb", "ns", "xx"])
def test_m2m100_lacks_languages_outside_its_list(code):
    assert M2M100_418M.engine_code(code) is None


def test_m2m100_rejects_a_room_language_it_lacks_by_name():
    assert language_support_error("local", "m2m100_418M", ["en", *M2M100_CHECKED]) is None
    assert language_support_error("local", "m2m100_1.2B", ["en", "eo"]) == (
        "The built-in translator (m2m100_1.2B) doesn't support Esperanto. "
        "Choose another translation service or change the languages."
    )


@pytest.mark.anyio
@pytest.mark.parametrize("code", M2M100_CHECKED)
@pytest.mark.parametrize("model", M2M100_MODELS, ids=lambda model: model.id)
async def test_m2m100_translates_english_both_ways(monkeypatch, model, code):
    calls = []

    def fake_inference(self, text, source_token, target_token, model_size):
        calls.append((source_token, target_token, model_size))
        return "translated"

    monkeypatch.setattr(LocalProvider, "_run_inference", fake_inference)
    english = Language(code="en", name="English")
    other = Language(code=code, name=code)

    assert await LocalProvider().translate_batch(english, [other], ["Hello"], TranslationContext(), model=model.id) == [
        ["translated"]
    ]
    assert await LocalProvider().translate_batch(other, [english], ["Hello"], TranslationContext(), model=model.id) == [
        ["translated"]
    ]
    assert calls == [("__en__", f"__{code}__", model.id), (f"__{code}__", "__en__", model.id)]


class FakeTokenizer:
    """Tokenizes like M2M100Tokenizer with its default source language: ``__en__``, the words, ``</s>``."""

    def encode(self, text):
        return ["__en__", *text.split(), "</s>"]

    def convert_ids_to_tokens(self, ids):
        return list(ids)

    def convert_tokens_to_ids(self, tokens):
        return list(tokens)

    def decode(self, ids):
        return " ".join(token for token in ids if token != "</s>")


def test_m2m100_inference_marks_the_source_language_and_forces_the_target():
    translator = MagicMock()
    translator.translate_batch.return_value = [MagicMock(hypotheses=[["__fr__", "Bonjour", "</s>"]])]

    with patch(
        "portal.translations.providers.local.get_model_and_tokenizer", return_value=(translator, FakeTokenizer())
    ):
        result = LocalProvider()._run_inference("Hallo", "__de__", "__fr__", "m2m100_418M")

    assert result == "Bonjour"
    (source,) = translator.translate_batch.call_args.args[0]
    assert source == ["__de__", "Hallo", "</s>"]
    assert translator.translate_batch.call_args.kwargs["target_prefix"] == [["__fr__"]]


@pytest.mark.parametrize(
    ("model_id", "tokenizer_options"),
    [
        (NLLB, {"src_lang": "eng_Latn"}),
        ("m2m100_418M", {"tokenizer_type": "m2m_100"}),
        ("m2m100_1.2B", {"tokenizer_type": "m2m_100"}),
        # An operator's own build is loaded as NLLB, as before.
        ("someone/their-nllb-build", {"src_lang": "eng_Latn"}),
    ],
)
def test_the_tokenizer_is_loaded_as_the_registry_entry_says(model_id, tokenizer_options):
    """M2M100's repository names no tokenizer class, and its tokenizer has no eng_Latn language."""
    _loaded_models.pop(model_id, None)
    try:
        with (
            patch("huggingface_hub.snapshot_download", return_value="/cache/model"),
            patch("ctranslate2.Translator"),
            patch("transformers.AutoTokenizer.from_pretrained") as from_pretrained,
        ):
            get_model_and_tokenizer(model_id)
    finally:
        _loaded_models.pop(model_id, None)

    from_pretrained.assert_called_once_with("/cache/model", revision="main", **tokenizer_options)
