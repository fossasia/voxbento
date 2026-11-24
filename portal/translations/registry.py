"""The translation models this server can run itself, one declarative entry per model (#1185).

Everything the rest of the code needs to know about a self-hosted model lives in its entry:
where its files come from, which languages it translates and what it can be asked to do.
Hosted models (OpenAI, Gemini, a self-hosted LLM endpoint) are chosen by name through their
provider and have no entry here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from portal.translations.engine import EngineKind


class LanguageTagging(str, Enum):
    """How a sequence-to-sequence model is told which languages to translate between."""

    #: The source text starts with the source language's token and the output is forced to start
    #: with the target language's token (NLLB-200, M2M100).
    LANGUAGE_TOKENS = "language_tokens"
    #: The source text is prefixed with a ``<2xx>`` target tag and the model detects the source
    #: language itself (MADLAD-400, a T5 model).
    TARGET_TAG = "target_tag"


@dataclass(frozen=True, slots=True)
class TranslationModel:
    """One self-hosted translation model."""

    #: The name stored on rooms and booths (``translation_model``).
    id: str
    engine_kind: EngineKind
    #: Hugging Face repository holding the converted model, and the commit it is pinned to.
    hf_repo: str
    hf_revision: str
    #: The languages the model translates: ISO 639-1 code to the code the model itself uses.
    languages: Mapping[str, str]
    #: Roughly how much memory the loaded model takes, in MB.
    approx_ram_mb: int
    #: Whether the model takes instructions, so persona, style and glossary lines can be sent as a prompt.
    supports_prompt: bool
    #: Whether one pass can translate a text into several target languages.
    supports_multi_target_batch: bool
    #: SPDX identifier of the model's licence.
    licence: str
    language_tagging: LanguageTagging = LanguageTagging.LANGUAGE_TOKENS

    def engine_code(self, language_code: str) -> str | None:
        """The model's own code for an ISO 639-1 language, or None when it lacks the language."""
        return self.languages.get(language_code.strip().lower())

    def unsupported(self, language_codes: Iterable[str]) -> list[str]:
        """The given ISO 639-1 codes this model cannot translate, in the order given, without repeats."""
        return [code for code in dict.fromkeys(language_codes) if self.engine_code(code) is None]


# FLORES-200 codes for the languages NLLB-200 covers that have an ISO 639-1 code.
_NLLB_200_LANGUAGES = {
    "af": "afr_Latn", "ak": "aka_Latn", "am": "amh_Ethi", "ar": "arb_Arab", "as": "asm_Beng", "ba": "bak_Cyrl",
    "be": "bel_Cyrl", "bg": "bul_Cyrl", "bm": "bam_Latn", "bn": "ben_Beng", "bo": "bod_Tibt", "bs": "bos_Latn",
    "ca": "cat_Latn", "cs": "ces_Latn", "cy": "cym_Latn", "da": "dan_Latn", "de": "deu_Latn", "dz": "dzo_Tibt",
    "ee": "ewe_Latn", "el": "ell_Grek", "en": "eng_Latn", "eo": "epo_Latn", "es": "spa_Latn", "et": "est_Latn",
    "eu": "eus_Latn", "fi": "fin_Latn", "fj": "fij_Latn", "fo": "fao_Latn", "fr": "fra_Latn", "ga": "gle_Latn",
    "gd": "gla_Latn", "gl": "glg_Latn", "gn": "grn_Latn", "gu": "guj_Gujr", "ha": "hau_Latn", "he": "heb_Hebr",
    "hi": "hin_Deva", "hr": "hrv_Latn", "ht": "hat_Latn", "hu": "hun_Latn", "hy": "hye_Armn", "id": "ind_Latn",
    "ig": "ibo_Latn", "is": "isl_Latn", "it": "ita_Latn", "ja": "jpn_Jpan", "jv": "jav_Latn", "ka": "kat_Geor",
    "kg": "kon_Latn", "ki": "kik_Latn", "kk": "kaz_Cyrl", "km": "khm_Khmr", "kn": "kan_Knda", "ko": "kor_Hang",
    "ks": "kas_Arab", "ky": "kir_Cyrl", "lb": "ltz_Latn", "lg": "lug_Latn", "li": "lim_Latn", "ln": "lin_Latn",
    "lo": "lao_Laoo", "lt": "lit_Latn", "mi": "mri_Latn", "mk": "mkd_Cyrl", "ml": "mal_Mlym", "mr": "mar_Deva",
    "mt": "mlt_Latn", "my": "mya_Mymr", "nb": "nob_Latn", "nl": "nld_Latn", "nn": "nno_Latn", "ny": "nya_Latn",
    "oc": "oci_Latn", "pa": "pan_Guru", "pl": "pol_Latn", "pt": "por_Latn", "rn": "run_Latn", "ro": "ron_Latn",
    "ru": "rus_Cyrl", "rw": "kin_Latn", "sa": "san_Deva", "sc": "srd_Latn", "sd": "snd_Arab", "sg": "sag_Latn",
    "si": "sin_Sinh", "sk": "slk_Latn", "sl": "slv_Latn", "sm": "smo_Latn", "sn": "sna_Latn", "so": "som_Latn",
    "sr": "srp_Cyrl", "ss": "ssw_Latn", "st": "sot_Latn", "su": "sun_Latn", "sv": "swe_Latn", "ta": "tam_Taml",
    "te": "tel_Telu", "tg": "tgk_Cyrl", "th": "tha_Thai", "ti": "tir_Ethi", "tk": "tuk_Latn", "tl": "tgl_Latn",
    "tn": "tsn_Latn", "tr": "tur_Latn", "ts": "tso_Latn", "tt": "tat_Cyrl", "tw": "twi_Latn", "ug": "uig_Arab",
    "uk": "ukr_Cyrl", "ur": "urd_Arab", "vi": "vie_Latn", "wo": "wol_Latn", "xh": "xho_Latn", "yo": "yor_Latn",
    "zh": "zho_Hans", "zu": "zul_Latn",
}

NLLB_200_DISTILLED_600M = TranslationModel(
    id="nllb-200-distilled-600M",
    engine_kind=EngineKind.CT2_SEQ2SEQ,
    hf_repo="JustFrederik/nllb-200-distilled-600M-ct2-int8",
    hf_revision="302d78f00e6fdb50a1064059df7c392b735e9d05",
    languages=MappingProxyType(_NLLB_200_LANGUAGES),
    approx_ram_mb=600,
    # Sequence-to-sequence: no prompt, so persona and style do not apply (glossary terms use placeholders).
    supports_prompt=False,
    supports_multi_target_batch=True,
    licence="CC-BY-NC-4.0",
)

# MADLAD-400 marks the target language with a "<2xx>" tag in front of the source text. These are
# its tags that are ISO 639-1 codes; it has several hundred more, for languages without one. VoxBento
# names languages by ISO 639-1 code everywhere (booths, rooms, listeners), so only these are reachable.
_MADLAD_400_LANGUAGES = {
    code: f"<2{code}>"
    for code in (
        "af ak am an ar as av ay az ba be bg bi bm bn bo br bs ca ce ch co cs cv cy da de dv dz ee el en "
        "eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy id ig io is it iu ja "
        "jv ka kg kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms "
        "mt my ne nl nn no nr nv ny oc oj om or os pa pl ps pt qu rm rn ro ru rw sa sc sd se sg sh si sk "
        "sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tn to tr ts tt ug uk ur uz ve vi wa wo xh yi "
        "yo zh zu"
    ).split()
}
# Tagalog has no MADLAD-400 tag of its own: MADLAD tags it "fil" (Filipino, its standard form).
_MADLAD_400_LANGUAGES["tl"] = "<2fil>"

MADLAD_400_3B = TranslationModel(
    id="madlad400-3b-mt",
    engine_kind=EngineKind.CT2_SEQ2SEQ,
    hf_repo="Nextcloud-AI/madlad400-3b-mt-ct2-int8",
    hf_revision="aa32bbdeba7880eff2096ec044cb155a340a9400",
    languages=MappingProxyType(_MADLAD_400_LANGUAGES),
    approx_ram_mb=3200,
    supports_prompt=False,
    # Each input carries its own target tag, so one batch can hold a text for several languages.
    supports_multi_target_batch=True,
    licence="Apache-2.0",
    language_tagging=LanguageTagging.TARGET_TAG,
)

#: Every self-hosted model, by id. The first one is the default.
LOCAL_TRANSLATION_MODELS: Mapping[str, TranslationModel] = MappingProxyType(
    {model.id: model for model in (NLLB_200_DISTILLED_600M, MADLAD_400_3B)}
)


def get_translation_model(model_id: str) -> TranslationModel | None:
    """The registry entry for a self-hosted model, or None when ``model_id`` is not one."""
    return LOCAL_TRANSLATION_MODELS.get(model_id)
