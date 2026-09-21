"""Tests for booth identity scheme — validation, ID generation, and path conversion."""

from __future__ import annotations

import pytest

from portal.booth_identity import (
    booth_id_to_mediamtx_path,
    make_booth_id,
    make_mediamtx_path,
    mediamtx_path_to_booth_id,
    parse_booth_id,
    validate_event_slug,
    validate_instance,
    validate_language_code,
    validate_room_id,
)

# ── validate_event_slug ──────────────────────────────────────────────────────


class TestValidateEventSlug:
    def test_simple_slug(self):
        assert validate_event_slug("pycon2026") == "pycon2026"

    def test_slug_with_hyphens(self):
        assert validate_event_slug("my-great-event") == "my-great-event"

    def test_normalises_to_lowercase(self):
        assert validate_event_slug("PyCon2026") == "pycon2026"

    def test_strips_whitespace(self):
        assert validate_event_slug("  pycon2026  ") == "pycon2026"

    def test_empty_slug_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_event_slug("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_event_slug("   ")

    def test_consecutive_hyphens_rejected(self):
        with pytest.raises(ValueError, match="consecutive hyphens"):
            validate_event_slug("my--event")

    def test_leading_hyphen_rejected(self):
        with pytest.raises(ValueError, match="start and end with an alphanumeric"):
            validate_event_slug("-pycon")

    def test_trailing_hyphen_rejected(self):
        with pytest.raises(ValueError, match="start and end with an alphanumeric"):
            validate_event_slug("pycon-")

    def test_special_characters_rejected(self):
        with pytest.raises(ValueError):
            validate_event_slug("my_event")

    def test_spaces_in_slug_rejected(self):
        with pytest.raises(ValueError):
            validate_event_slug("my event")

    def test_too_long_slug_rejected(self):
        with pytest.raises(ValueError, match="must not exceed"):
            validate_event_slug("a" * 65)

    def test_max_length_slug_accepted(self):
        slug = "a" * 64
        assert validate_event_slug(slug) == slug

    def test_too_long_slug_message_is_formatted(self):
        # The substring check above also matched the old, unformatted tuple
        # message, so pin the exact text the API returns to clients.
        with pytest.raises(ValueError) as exc_info:
            validate_event_slug("a" * 70)
        assert str(exc_info.value) == "Event slug must not exceed 64 characters (got 70)."


# ── validate_language_code ────────────────────────────────────────────────────


class TestValidateLanguageCode:
    def test_valid_code(self):
        assert validate_language_code("en") == "en"

    def test_normalises_to_lowercase(self):
        assert validate_language_code("FR") == "fr"

    def test_strips_whitespace(self):
        assert validate_language_code("  de  ") == "de"

    def test_three_letter_code_rejected(self):
        with pytest.raises(ValueError, match="two lowercase ASCII letters"):
            validate_language_code("eng")

    def test_single_letter_rejected(self):
        with pytest.raises(ValueError, match="two lowercase ASCII letters"):
            validate_language_code("e")

    def test_numeric_code_rejected(self):
        with pytest.raises(ValueError, match="two lowercase ASCII letters"):
            validate_language_code("12")

    def test_unrecognised_code_rejected(self):
        with pytest.raises(ValueError, match="not a recognised ISO 639-1"):
            validate_language_code("zz")

    def test_empty_code_rejected(self):
        with pytest.raises(ValueError):
            validate_language_code("")

    def test_common_codes_accepted(self):
        for code in ("en", "fr", "de", "es", "zh", "ja", "ar", "hi", "pt", "ru"):
            assert validate_language_code(code) == code


# ── validate_instance ─────────────────────────────────────────────────────────


class TestValidateInstance:
    def test_primary(self):
        assert validate_instance("primary") == "primary"

    def test_backup(self):
        assert validate_instance("backup") == "backup"

    def test_case_insensitive(self):
        assert validate_instance("PRIMARY") == "primary"
        assert validate_instance("Backup") == "backup"

    def test_strips_whitespace(self):
        assert validate_instance("  primary  ") == "primary"

    def test_invalid_instance_rejected(self):
        with pytest.raises(ValueError, match="must be 'primary' or 'backup'"):
            validate_instance("secondary")

    def test_empty_instance_rejected(self):
        with pytest.raises(ValueError):
            validate_instance("")


# ── validate_room_id ──────────────────────────────────────────────────────────


class TestValidateRoomId:
    @pytest.mark.parametrize("room_id", [0, 1, 14, 10**9])
    def test_non_negative_integers_accepted(self, room_id):
        assert validate_room_id(room_id) == room_id

    @pytest.mark.parametrize("room_id", [-1, -3, True, False, 3.7, "14", "abc", None])
    def test_everything_else_rejected(self, room_id):
        with pytest.raises(ValueError, match="non-negative integer"):
            validate_room_id(room_id)


# ── make_booth_id ─────────────────────────────────────────────────────────────


class TestMakeBoothId:
    def test_basic(self):
        assert make_booth_id("pycon2026", 1, "en") == "pycon2026-1-en"

    def test_with_hyphens_in_slug(self):
        assert make_booth_id("my-great-event", 2, "fr") == "my-great-event-2-fr"

    def test_normalises_inputs(self):
        assert make_booth_id("PyCon2026", 1, "EN") == "pycon2026-1-en"

    def test_invalid_slug_raises(self):
        with pytest.raises(ValueError):
            make_booth_id("", 1, "en")

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError):
            make_booth_id("pycon2026", 1, "xyz")

    @pytest.mark.parametrize("room_id", [-3, "abc", True, 3.7])
    def test_invalid_room_id_raises(self, room_id):
        with pytest.raises(ValueError, match="non-negative integer"):
            make_booth_id("pycon2026", room_id, "en")


# ── make_mediamtx_path ────────────────────────────────────────────────────────


class TestMakeMediamtxPath:
    def test_basic(self):
        assert make_mediamtx_path("pycon2026", 1, "en") == "pycon2026/1/en"

    def test_with_hyphens(self):
        assert make_mediamtx_path("my-great-event", 2, "fr") == "my-great-event/2/fr"

    def test_normalises_inputs(self):
        assert make_mediamtx_path("PyCon2026", 1, "EN") == "pycon2026/1/en"

    def test_invalid_slug_raises(self):
        with pytest.raises(ValueError):
            make_mediamtx_path("", 1, "en")

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError):
            make_mediamtx_path("pycon2026", 1, "xyz")

    @pytest.mark.parametrize("room_id", [-3, "abc", True, 3.7])
    def test_invalid_room_id_raises(self, room_id):
        with pytest.raises(ValueError, match="non-negative integer"):
            make_mediamtx_path("pycon2026", room_id, "en")


# ── parse_booth_id ────────────────────────────────────────────────────────────


class TestParseBoothId:
    def test_simple(self):
        assert parse_booth_id("pycon2026-1-en") == ("pycon2026", 1, "en")

    def test_slug_with_hyphens(self):
        assert parse_booth_id("my-great-event-2-fr") == ("my-great-event", 2, "fr")

    def test_normalises_case(self):
        assert parse_booth_id("PyCon2026-1-EN") == ("pycon2026", 1, "en")

    def test_strips_whitespace(self):
        assert parse_booth_id("  pycon2026-1-en  ") == ("pycon2026", 1, "en")

    def test_missing_language_raises(self):
        with pytest.raises(ValueError):
            parse_booth_id("pycon2026-1")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_booth_id("")

    def test_three_letter_code_rejected(self):
        with pytest.raises(ValueError):
            parse_booth_id("pycon2026-1-eng")

    def test_unrecognised_code_rejected(self):
        with pytest.raises(ValueError):
            parse_booth_id("pycon2026-1-zz")


# ── booth_id_to_mediamtx_path ────────────────────────────────────────────────


class TestBoothIdToMediamtxPath:
    def test_basic(self):
        assert booth_id_to_mediamtx_path("pycon2026-1-en") == "pycon2026/1/en"

    def test_slug_with_hyphens(self):
        assert booth_id_to_mediamtx_path("my-great-event-2-fr") == "my-great-event/2/fr"

    def test_invalid_id_raises(self):
        with pytest.raises(ValueError):
            booth_id_to_mediamtx_path("invalid")


# ── mediamtx_path_to_booth_id ────────────────────────────────────────────────


class TestMediamtxPathToBoothId:
    def test_basic(self):
        assert mediamtx_path_to_booth_id("pycon2026/1/en") == "pycon2026-1-en"

    def test_with_hyphens(self):
        assert mediamtx_path_to_booth_id("my-great-event/2/fr") == "my-great-event-2-fr"

    def test_strips_slashes(self):
        assert mediamtx_path_to_booth_id("/pycon2026/1/en/") == "pycon2026-1-en"

    def test_too_many_segments_raises(self):
        with pytest.raises(ValueError, match="exactly three segments"):
            mediamtx_path_to_booth_id("a/b/c/d")

    def test_single_segment_raises(self):
        with pytest.raises(ValueError, match="exactly three segments"):
            mediamtx_path_to_booth_id("pycon2026")

    def test_invalid_slug_raises(self):
        with pytest.raises(ValueError):
            mediamtx_path_to_booth_id("-invalid/1/en")

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError):
            mediamtx_path_to_booth_id("pycon2026/1/xyz")

    @pytest.mark.parametrize("room_segment", ["-3", "+3", " 3", "1_000", "abc", "3.7"])
    def test_non_integer_room_segment_raises(self, room_segment):
        # int() accepts several of these; parse_booth_id would not take the
        # resulting ID back, so they must be rejected here too.
        with pytest.raises(ValueError, match="non-negative integer"):
            mediamtx_path_to_booth_id(f"pycon2026/{room_segment}/en")


# ── Bidirectional round-trip ──────────────────────────────────────────────────


class TestRoundTrip:
    @pytest.mark.parametrize(
        "event_slug,room_id,lang_code",
        [
            ("pycon2026", 1, "en"),
            ("my-great-event", 2, "fr"),
            ("fossasia2026", 3, "de"),
            ("event1", 4, "zh"),
        ],
    )
    def test_booth_id_roundtrip(self, event_slug, room_id, lang_code):
        booth_id = make_booth_id(event_slug, room_id, lang_code)
        path = booth_id_to_mediamtx_path(booth_id)
        recovered_id = mediamtx_path_to_booth_id(path)
        assert recovered_id == booth_id

    @pytest.mark.parametrize(
        "event_slug,room_id,lang_code",
        [
            ("pycon2026", 1, "en"),
            ("my-great-event", 2, "fr"),
        ],
    )
    def test_mediamtx_path_roundtrip(self, event_slug, room_id, lang_code):
        path = make_mediamtx_path(event_slug, room_id, lang_code)
        booth_id = mediamtx_path_to_booth_id(path)
        recovered_path = booth_id_to_mediamtx_path(booth_id)
        assert recovered_path == path

    @pytest.mark.parametrize("room_id", [0, 1, 14, 10**9])
    def test_every_built_booth_id_parses_back(self, room_id):
        # The invariant the room_id validation exists to guarantee: whatever
        # make_booth_id accepts, parse_booth_id recovers exactly.
        booth_id = make_booth_id("pycon2026", room_id, "en")
        assert parse_booth_id(booth_id) == ("pycon2026", room_id, "en")
