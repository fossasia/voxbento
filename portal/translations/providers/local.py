from __future__ import annotations

import logging

from portal.ray_serve.client import RayClient
from portal.translations.constants import NLLB_LANGUAGE_MAP
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)

_CODE_TO_NAME = {
    "af": "Afrikaans",
    "ak": "Akan",
    "am": "Amharic",
    "ar": "Arabic",
    "an": "Aragonese",
    "hy": "Armenian",
    "as": "Assamese",
    "av": "Avaric",
    "ae": "Avestan",
    "ay": "Aymara",
    "az": "Azerbaijani",
    "bm": "Bambara",
    "ba": "Bashkir",
    "eu": "Basque",
    "be": "Belarusian",
    "bn": "Bengali",
    "bi": "Bislama",
    "bs": "Bosnian",
    "br": "Breton",
    "bg": "Bulgarian",
    "my": "Burmese",
    "ca": "Catalan",
    "ch": "Chamorro",
    "ce": "Chechen",
    "ny": "Chichewa",
    "zh": "Chinese",
    "cv": "Chuvash",
    "kw": "Cornish",
    "co": "Corsican",
    "cr": "Cree",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "dv": "Divehi",
    "nl": "Dutch",
    "dz": "Dzongkha",
    "en": "English",
    "eo": "Esperanto",
    "et": "Estonian",
    "ee": "Ewe",
    "fo": "Faroese",
    "fj": "Fijian",
    "fi": "Finnish",
    "fr": "French",
    "ff": "Fula",
    "gl": "Galician",
    "lg": "Ganda",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek, Modern (1453-)",
    "gn": "Guarani",
    "gu": "Gujarati",
    "ht": "Haitian",
    "ha": "Hausa",
    "he": "Hebrew",
    "hz": "Herero",
    "hi": "Hindi",
    "ho": "Hiri Motu",
    "hu": "Hungarian",
    "is": "Icelandic",
    "io": "Ido",
    "ig": "Igbo",
    "id": "Indonesian",
    "ia": "Interlingua",
    "ie": "Interlingue",
    "iu": "Inuktitut",
    "ik": "Inupiaq",
    "ga": "Irish",
    "it": "Italian",
    "ja": "Japanese",
    "jv": "Javanese",
    "kl": "Kalaallisut",
    "kn": "Kannada",
    "kr": "Kanuri",
    "ks": "Kashmiri",
    "kk": "Kazakh",
    "km": "Khmer",
    "ki": "Kikuyu",
    "rw": "Kinyarwanda",
    "rn": "Kirundi",
    "kv": "Komi",
    "kg": "Kongo",
    "ko": "Korean",
    "ku": "Kurdish",
    "kj": "Kwanyama",
    "ky": "Kirghiz",
    "lo": "Lao",
    "la": "Latin",
    "lv": "Latvian",
    "li": "Limburgish",
    "ln": "Lingala",
    "lt": "Lithuanian",
    "lu": "Luba-Katanga",
    "lb": "Luxembourgish",
    "mi": "Maori",
    "mk": "Macedonian",
    "mg": "Malagasy",
    "ms": "Malay",
    "ml": "Malayalam",
    "mt": "Maltese",
    "gv": "Manx",
    "mr": "Marathi",
    "mh": "Marshallese",
    "mn": "Mongolian",
    "na": "Nauru",
    "nv": "Navajo",
    "ng": "Ndonga",
    "ne": "Nepali",
    "nd": "Northern Ndebele",
    "se": "Northern Sami",
    "no": "Norwegian",
    "nb": "Norwegian Bokmål",
    "nn": "Norwegian Nynorsk",
    "ii": "Nuosu",
    "oc": "Occitan (post 1500)",
    "oj": "Ojibwe",
    "cu": "Old Church Slavonic",
    "or": "Oriya",
    "om": "Oromo",
    "os": "Ossetian",
    "pi": "Pāli",
    "pa": "Panjabi",
    "ps": "Pashto",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "qu": "Quechua",
    "ro": "Romanian",
    "rm": "Romansh",
    "ru": "Russian",
    "sm": "Samoan",
    "sg": "Sango",
    "sa": "Sanskrit",
    "sc": "Sardinian",
    "gd": "Scottish Gaelic",
    "sr": "Serbian",
    "sn": "Shona",
    "sd": "Sindhi",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "nr": "Southern Ndebele",
    "st": "Southern Sotho",
    "es": "Spanish",
    "su": "Sundanese",
    "sw": "Swahili",
    "ss": "Swati",
    "sv": "Swedish",
    "tl": "Tagalog",
    "ty": "Tahitian",
    "tg": "Tajik",
    "ta": "Tamil",
    "tt": "Tatar",
    "te": "Telugu",
    "th": "Thai",
    "bo": "Tibetan",
    "ti": "Tigrinya",
    "to": "Tonga",
    "ts": "Tsonga",
    "tn": "Tswana",
    "tr": "Turkish",
    "tk": "Turkmen",
    "tw": "Twi",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "ug": "Uyghur",
    "uz": "Uzbek",
    "ve": "Venda",
    "vi": "Vietnamese",
    "vo": "Volapük",
    "wa": "Walloon",
    "cy": "Welsh",
    "fy": "Western Frisian",
    "wo": "Wolof",
    "xh": "Xhosa",
    "yi": "Yiddish",
    "yo": "Yoruba",
    "za": "Zhuang",
    "zu": "Zulu",
}


class LocalProvider(TranslationProvider):
    def __init__(self):
        self.ray_client = RayClient()

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
        logger.debug(
            f"[NLLB Ray] translate called: source='{source_lang_name}' -> target='{target_lang_name}' "
            f"text_len={len(text)}"
        )

        if target_lang_name in _CODE_TO_NAME:
            target_lang_name = _CODE_TO_NAME[target_lang_name]
        if source_lang_name in _CODE_TO_NAME:
            source_lang_name = _CODE_TO_NAME[source_lang_name]

        target_lang_token = NLLB_LANGUAGE_MAP.get(target_lang_name)
        source_lang_token = NLLB_LANGUAGE_MAP.get(source_lang_name)
        if not target_lang_token or not source_lang_token:
            logger.error(f"[NLLB Ray] Failed to resolve language tokens for {source_lang_name} -> {target_lang_name}")
            return None

        payload = {"text": text, "source_lang_token": source_lang_token, "target_lang_token": target_lang_token}

        headers = {"X-Serve-Multiplexed-Model-Id": model}

        try:
            result = await self.ray_client.predict("translator", payload, headers=headers)
            if "translated_text" in result:
                return result["translated_text"]
            else:
                logger.error(f"[NLLB Ray] Unexpected response: {result}")
                raise RuntimeError(f"Unexpected response from Ray Serve: {result}")
        except Exception as e:
            logger.error(f"[NLLB Ray] Request to Ray Serve failed: {e}")
            raise
