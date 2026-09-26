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
# _EVENT_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2}$")
_BOOTH_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+-(?:[a-z]{2}|floor)$")

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
        raise ValueError(f"Event slug must not exceed {_EVENT_SLUG_MAX_LENGTH} characters (got {len(normalised)}).")
    if not _EVENT_SLUG_RE.match(normalised):
        raise ValueError(
            "Event slug must contain only lowercase alphanumeric characters and hyphens, "
            "must start and end with an alphanumeric character, and must not contain "
            f"consecutive hyphens. Got: '{normalised}'."
        )
    return normalised


def validate_language_code(code: str) -> str:
    """Validate an ISO 639-1 language code or 'floor'.

    Returns the lowercased code on success.
    Raises ``ValueError`` with a descriptive message on failure.
    """
    normalised = code.strip().lower()
    if normalised == "floor":
        return normalised
    if not _LANGUAGE_CODE_RE.match(normalised):
        raise ValueError(
            f"Language code must be exactly two lowercase ASCII letters (ISO 639-1) or 'floor'. Got: '{code}'."
        )
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


def validate_room_id(room_id: int) -> int:
    """Validate a room ID.

    Returns the room ID on success. It must be a non-negative integer -- the
    same set ``parse_booth_id`` accepts -- so every ID built from it can be
    parsed back. ``bool`` is rejected explicitly: it is a subclass of ``int``
    and would otherwise be rendered as ``True`` or ``False``.
    Raises ``ValueError`` on invalid input.
    """
    if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id < 0:
        raise ValueError(f"Room ID must be a non-negative integer. Got: {room_id!r}.")
    return room_id


def _room_id_or_none(room_id: int | None) -> int | None:
    """Validate ``room_id`` unless it is ``None``.

    Only ``make_mediamtx_path`` uses this: ``start_transcription_worker``
    deliberately defaults ``room_id`` to ``None`` and builds its channel path
    with it, and changing that contract is a separate decision. ``make_booth_id``
    stays strict, because a booth ID has to parse back.
    """
    return None if room_id is None else validate_room_id(room_id)


# ── Identity construction / conversion ────────────────────────────────────────


def make_booth_id(event_slug: str, room_id: int, language_code: str) -> str:
    """Build a booth ID from validated coordinates.

    Format: ``{event_slug}-{room_id}-{language_code}`` (e.g. ``pycon2026-14-en``).
    Inputs are validated before construction.
    """
    slug = validate_event_slug(event_slug)
    room = validate_room_id(room_id)
    code = validate_language_code(language_code)
    return f"{slug}-{room}-{code}"


def make_ai_booth_id(event_slug: str, room_id: int, language_code: str) -> str:
    """Build the ID of a room's AI (TTS) booth for one target language.

    Format: ``{event_slug}-{room_id}-ai-{language_code}`` (e.g. ``pycon2026-14-ai-de``).
    AI booths have no MediaMTX path: listeners receive their audio over
    ``/ws/tts/{booth_id}``. The language code is not checked against the ISO
    subset because it comes from a room's stored translation languages.
    """
    return f"{event_slug}-{room_id}-ai-{language_code}"


def make_mediamtx_path(event_slug: str, room_id: int | None, language_code: str) -> str:
    """Build a MediaMTX stream path from validated coordinates.

    Format: ``{event_slug}/{room_id}/{language_code}`` (e.g. ``pycon2026/14/en``).
    """
    slug = validate_event_slug(event_slug)
    room = _room_id_or_none(room_id)
    code = validate_language_code(language_code)
    return f"{slug}/{room}/{code}"


def booth_id_to_mediamtx_path(booth_id: str) -> str:
    """Convert a booth ID to its corresponding MediaMTX path.

    ``pycon2026-14-en`` → ``pycon2026/14/en``

    Raises ``ValueError`` if the booth ID is malformed.
    """
    event_slug, room_id, language_code = parse_booth_id(booth_id)
    return f"{event_slug}/{room_id}/{language_code}"


def mediamtx_path_to_booth_id(path: str) -> str:
    """Convert a MediaMTX path to the corresponding booth ID.

    ``pycon2026/14/en`` → ``pycon2026-14-en``

    Raises ``ValueError`` if the path is malformed.
    """
    normalised = path.strip().strip("/")
    parts = normalised.split("/")
    if len(parts) != 3:
        raise ValueError(
            f"MediaMTX path must have exactly three segments (event_slug/room_id/language_code). Got: '{path}'."
        )
    event_slug = validate_event_slug(parts[0])
    # int() alone is too lenient for a path segment: it accepts "-3", "+3",
    # " 3" and "1_000", none of which parse_booth_id would take back.
    if not (parts[1].isascii() and parts[1].isdigit()):
        raise ValueError(f"MediaMTX path room segment must be a non-negative integer. Got: '{parts[1]}'.")
    room_id = validate_room_id(int(parts[1]))
    language_code = validate_language_code(parts[2])
    return f"{event_slug}-{room_id}-{language_code}"


# Matches a human booth ("{event_slug}-{room_id}-{language_code}") and an AI booth
# ("{event_slug}-{room_id}-ai-{language_code}") alike. The language code is matched
# by shape only, not against ISO_639_1_CODES, so that authorization never depends on
# a booth's language being in that subset.
_BOOTH_SCOPE_RE = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-[0-9]+-(?:ai-)?(?:[a-z]{2}|floor)$")


def booth_id_event_slug(booth_id: str) -> str:
    """Return the event slug that owns *booth_id*, human or AI booth alike.

    Authorization compares this against a token's ``event_slug``. A prefix test
    cannot: one slug may extend another, so ``demo`` would match a booth owned
    by ``demo-other``.

    Raises ``ValueError`` if *booth_id* is not in either booth format.
    """
    match = _BOOTH_SCOPE_RE.match(booth_id.strip().lower())
    if not match:
        raise ValueError(f"Booth ID does not name an event: '{booth_id}'.")
    return match.group("slug")


def parse_booth_id(booth_id: str) -> tuple[str, int, str]:
    """Split a booth ID into (event_slug, room_id, language_code).

    Raises ``ValueError`` if the booth ID format is invalid.
    """
    normalised = booth_id.strip().lower()
    if not _BOOTH_ID_RE.match(normalised):
        raise ValueError(
            f"Booth ID must follow the format '{{event_slug}}-{{room_id}}-{{language_code}}'. Got: '{booth_id}'."
        )
    # booth_id is event_slug-room_id-language_code
    # event_slug can contain hyphens, room_id and language_code cannot.
    # So the last hyphen separates room_id and language_code,
    # and the second to last hyphen separates event_slug and room_id.
    parts = normalised.rsplit("-", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid booth ID format: '{booth_id}'.")

    event_slug = parts[0]
    room_id = int(parts[1])
    language_code = parts[2]

    validate_event_slug(event_slug)
    validate_language_code(language_code)
    return event_slug, room_id, language_code
