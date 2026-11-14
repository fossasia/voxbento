"""Independent provider configuration for the public live demo."""

from __future__ import annotations

from dataclasses import dataclass, field

from portal.crypto import decrypt_val
from portal.models import LiveDemoSettings
from portal.transcription.constants import ALLOWED_MODELS
from portal.translations.constants import TRANSLATION_MODELS

SPEECH_CHOICES = {
    f"{p.value}|{m}": f"{p.value} / {m}"
    for p, models in ALLOWED_MODELS.items()
    if p.value != "none"
    for m in sorted(models)
}
TRANSLATION_CHOICES = {f"{p}|{m}": f"{p} / {m}" for p, models in TRANSLATION_MODELS.items() for m in models}
VOICE_CHOICES = {
    "supertonic": "Supertonic (built in)",
    "deepgram": "Deepgram Aura",
    "elevenlabs": "ElevenLabs",
    "none": "No voice",
}
DEFAULTS = {
    "speech_choice": "local|base",
    "translation_choice": "local|nllb-200-distilled-600M",
    "voice_provider": "supertonic",
    "voice": "",
    "persona": "",
    "style": "",
}


@dataclass(frozen=True)
class DemoAI:
    """Independent demo services. Keys are decrypted here and never leave the server."""

    speech_provider: str
    speech_model: str
    translation_provider: str
    translation_model: str
    #: None when voice is disabled.
    voice_provider: str | None
    voice: str
    persona: str | None = None
    style: str | None = None
    #: What prevents these services from running, e.g. a missing API key.
    problems: tuple[str, ...] = ()
    speech_key: str | None = field(default=None, repr=False)
    translation_key: str | None = field(default=None, repr=False)
    voice_key: str | None = field(default=None, repr=False)


def load_demo_ai(row: LiveDemoSettings) -> DemoAI:
    problems: list[str] = []
    choices = {
        "speech_choice": SPEECH_CHOICES,
        "translation_choice": TRANSLATION_CHOICES,
        "voice_provider": VOICE_CHOICES,
    }
    selected = {}
    for name, allowed in choices.items():
        value = getattr(row, name)
        if value not in allowed:
            problems.append(f"Choose a supported {name.replace('_', ' ')} in the demo settings.")
            value = DEFAULTS[name]
        selected[name] = value
    speech, speech_model = selected["speech_choice"].split("|", 1)
    translation, translation_model = selected["translation_choice"].split("|", 1)
    voice = None if not row.voice_enabled or selected["voice_provider"] == "none" else selected["voice_provider"]
    keys = {}
    for service, provider in (("speech", speech), ("translation", translation), ("voice", voice)):
        if provider in {None, "local", "supertonic"}:
            keys[service] = None
            continue
        encrypted = getattr(row, f"encrypted_{service}_key")
        try:
            keys[service] = decrypt_val(encrypted)
        except ValueError:
            keys[service] = None
            problems.append(f"Replace the demo's {service} API key; it cannot be decrypted.")
        if provider not in {None, "local", "supertonic"} and not keys[service]:
            problems.append(f"The demo needs a {provider} API key for {service}.")
    return DemoAI(
        speech_provider=speech,
        speech_model=speech_model,
        translation_provider=translation,
        translation_model=translation_model,
        voice_provider=voice,
        voice=row.voice,
        persona=row.persona,
        style=row.style,
        speech_key=keys["speech"],
        translation_key=keys["translation"],
        voice_key=keys["voice"],
        problems=tuple(problems),
    )
