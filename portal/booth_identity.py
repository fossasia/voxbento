"""Booth identity scheme: {event_slug}-{language_code} IDs and MediaMTX path mapping.

A booth is identified by three coordinates:
- event_slug: alphanumeric + hyphens (e.g. "pycon2026")
- language_code: ISO 639-1 two-letter code (e.g. "en")
- instance: "primary" or "backup" — only one publishes at a time

The booth ID is ``{event_slug}-{language_code}`` (e.g. ``pycon2026-en``).
The MediaMTX stream path is ``{event_slug}/{language_code}`` (one active
stream per language per event).
"""

from __future__ import annotations

import re
from typing import Literal

BoothInstance = Literal["primary", "backup"]

# ── Validation patterns ──────────────────────────────────────────────────────

# Alphanumeric and hyphens, must start and end with alphanumeric,
# at least 1 character, no consecutive hyphens.
_EVENT_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EVENT_SLUG_MAX_LENGTH = 64

# ISO 639-1: exactly two lowercase ASCII letters.
_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2}$")

# Full booth ID: {event_slug}-{language_code}
# The language code is always the last two-letter segment after the final hyphen.
_BOOTH_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[a-z]{2}$")

# ISO 639-1 codes (subset of most common; validated against this set).
ISO_639_1_CODES: frozenset[str] = frozenset(
    {
        "aa",
        "ab",
        "af",
        "ak",
        "am",
        "an",
        "ar",
        "as",
        "av",
        "ay",
        "az",
        "ba",
        "be",
        "bg",
        "bh",
        "bi",
        "bm",
        "bn",
        "bo",
        "br",
        "bs",
        "ca",
        "ce",
        "ch",
        "co",
        "cr",
        "cs",
        "cu",
        "cv",
        "cy",
        "da",
        "de",
        "dv",
        "dz",
        "ee",
        "el",
        "en",
        "eo",
        "es",
        "et",
        "eu",
        "fa",
        "ff",
        "fi",
        "fj",
        "fo",
        "fr",
        "fy",
        "ga",
        "gd",
        "gl",
        "gn",
        "gu",
        "gv",
        "ha",
        "he",
        "hi",
        "ho",
        "hr",
        "ht",
        "hu",
        "hy",
        "hz",
        "ia",
        "id",
        "ie",
        "ig",
        "ii",
        "ik",
        "io",
        "is",
        "it",
        "iu",
        "ja",
        "jv",
        "ka",
        "kg",
        "ki",
        "kj",
        "kk",
        "kl",
        "km",
        "kn",
        "ko",
        "kr",
        "ks",
        "ku",
        "kv",
        "kw",
        "ky",
        "la",
        "lb",
        "lg",
        "li",
        "ln",
        "lo",
        "lt",
        "lu",
        "lv",
        "mg",
        "mh",
        "mi",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "mt",
        "my",
        "na",
        "nb",
        "nd",
        "ne",
        "ng",
        "nl",
        "nn",
        "no",
        "nr",
        "nv",
        "ny",
        "oc",
        "oj",
        "om",
        "or",
        "os",
        "pa",
        "pi",
        "pl",
        "ps",
        "pt",
        "qu",
        "rm",
        "rn",
        "ro",
        "ru",
        "rw",
        "sa",
        "sc",
        "sd",
        "se",
        "sg",
        "si",
        "sk",
        "sl",
        "sm",
        "sn",
        "so",
        "sq",
        "sr",
        "ss",
        "st",
        "su",
        "sv",
        "sw",
        "ta",
        "te",
        "tg",
        "th",
        "ti",
        "tk",
        "tl",
        "tn",
        "to",
        "tr",
        "ts",
        "tt",
        "tw",
        "ty",
        "ug",
        "uk",
        "ur",
        "uz",
        "ve",
        "vi",
        "vo",
        "wa",
        "wo",
        "xh",
        "yi",
        "yo",
        "za",
        "zh",
        "zu",
    }
)


# ── Validation helpers ────────────────────────────────────────────────────────


def validate_event_slug(slug: str) -> str:
    """Validate and normalise an event slug.

    Returns the lowercased slug on success.
    Raises ``ValueError`` with a descriptive message on failure.
    """
    normalised = slug.strip().lower()
    if not normalised:
        raise ValueError("Event slug must not be empty.")
    if len(normalised) > _EVENT_SLUG_MAX_LENGTH:
        raise ValueError(
            "Event slug must not exceed %d characters (got %d).",
            _EVENT_SLUG_MAX_LENGTH,
            len(normalised),
        )
    if not _EVENT_SLUG_RE.match(normalised):
        raise ValueError(
            "Event slug must contain only lowercase alphanumeric characters and hyphens, "
            "must start and end with an alphanumeric character, and must not contain "
            f"consecutive hyphens. Got: '{normalised}'."
        )
    return normalised


def validate_language_code(code: str) -> str:
    """Validate an ISO 639-1 language code.

    Returns the lowercased code on success.
    Raises ``ValueError`` with a descriptive message on failure.
    """
    normalised = code.strip().lower()
    if not _LANGUAGE_CODE_RE.match(normalised):
        raise ValueError(f"Language code must be exactly two lowercase ASCII letters (ISO 639-1). Got: '{code}'.")
    if normalised not in ISO_639_1_CODES:
        raise ValueError(f"'{normalised}' is not a recognised ISO 639-1 language code.")
    return normalised


def validate_instance(instance: str) -> BoothInstance:
    """Validate a booth instance identifier.

    Returns ``'primary'`` or ``'backup'``.
    Raises ``ValueError`` on invalid input.
    """
    normalised = instance.strip().lower()
    if normalised not in ("primary", "backup"):
        raise ValueError(f"Booth instance must be 'primary' or 'backup'. Got: '{instance}'.")
    return normalised  # type: ignore[return-value]


# ── Identity construction / conversion ────────────────────────────────────────


def make_booth_id(event_slug: str, language_code: str) -> str:
    """Build a booth ID from validated coordinates.

    Format: ``{event_slug}-{language_code}`` (e.g. ``pycon2026-en``).
    Inputs are validated before construction.
    """
    slug = validate_event_slug(event_slug)
    code = validate_language_code(language_code)
    return f"{slug}-{code}"


def make_mediamtx_path(event_slug: str, language_code: str) -> str:
    """Build a MediaMTX stream path from validated coordinates.

    Format: ``{event_slug}/{language_code}`` (e.g. ``pycon2026/en``).
    """
    slug = validate_event_slug(event_slug)
    code = validate_language_code(language_code)
    return f"{slug}/{code}"


def booth_id_to_mediamtx_path(booth_id: str) -> str:
    """Convert a booth ID to its corresponding MediaMTX path.

    ``pycon2026-en`` → ``pycon2026/en``

    Raises ``ValueError`` if the booth ID is malformed.
    """
    event_slug, language_code = parse_booth_id(booth_id)
    return f"{event_slug}/{language_code}"


def mediamtx_path_to_booth_id(path: str) -> str:
    """Convert a MediaMTX path to the corresponding booth ID.

    ``pycon2026/en`` → ``pycon2026-en``

    Raises ``ValueError`` if the path is malformed.
    """
    normalised = path.strip().strip("/")
    parts = normalised.split("/")
    if len(parts) != 2:
        raise ValueError(f"MediaMTX path must have exactly two segments (event_slug/language_code). Got: '{path}'.")
    event_slug = validate_event_slug(parts[0])
    language_code = validate_language_code(parts[1])
    return f"{event_slug}-{language_code}"


def parse_booth_id(booth_id: str) -> tuple[str, str]:
    """Split a booth ID into (event_slug, language_code).

    The language code is always the last two characters after the final
    hyphen. Everything before that hyphen is the event slug.

    Raises ``ValueError`` if the booth ID format is invalid.
    """
    normalised = booth_id.strip().lower()
    if not _BOOTH_ID_RE.match(normalised):
        raise ValueError(
            f"Booth ID must follow the format '{{event_slug}}-{{language_code}}' "
            f"where language_code is a two-letter ISO 639-1 code. Got: '{booth_id}'."
        )
    # Last hyphen separates slug from language code
    last_hyphen = normalised.rfind("-")
    event_slug = normalised[:last_hyphen]
    language_code = normalised[last_hyphen + 1 :]
    # Validate both halves
    validate_event_slug(event_slug)
    validate_language_code(language_code)
    return event_slug, language_code
