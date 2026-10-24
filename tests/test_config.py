"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from portal.config import Settings


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False


def test_effective_jitsi_internal_base_fallback():
    s = Settings(jitsi_internal_base="", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://jitsi.local"


def test_effective_jitsi_internal_base_override():
    s = Settings(jitsi_internal_base="http://internal.jitsi", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://internal.jitsi"


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://voxbento.example?tenant=events",
        "https://voxbento.example/#sitemap",
        "https://voxbento.example/voxbento?tenant=events#sitemap",
    ],
)
def test_public_base_url_rejects_query_strings_and_fragments(public_base_url: str):
    """Canonical public URLs must not contain query or fragment components."""
    with pytest.raises(ValidationError, match="query string or fragment"):
        Settings(_env_file=None, public_base_url=public_base_url)


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://voxbento.example",
        "https://voxbento.example/",
        "https://voxbento.example/voxbento/",
    ],
)
def test_public_base_url_allows_origin_and_path_prefix(public_base_url: str):
    """Canonical public URLs may include an origin and deployment path prefix."""
    assert Settings(_env_file=None, public_base_url=public_base_url).public_base_url == public_base_url


def test_legacy_admin_password_environment_variable_is_ignored(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "legacy-shared-secret")

    settings = Settings(_env_file=None)

    assert "admin_password" not in type(settings).model_fields


def test_production_requires_booth_access_token():
    settings = Settings(
        _env_file=None,
        debug=False,
        secret_key="a-secure-secret-key-that-is-long-enough",
        mediamtx_auth_hook_secret="a-secure-hook-secret",
        booth_access_token="",
    )

    try:
        settings.validate_production_secrets()
    except RuntimeError as exc:
        assert "BOOTH_ACCESS_TOKEN" in str(exc)
    else:
        raise AssertionError("empty BOOTH_ACCESS_TOKEN must fail production validation")


def test_production_rejects_known_placeholder_and_short_booth_tokens():
    for token in ("change-me", "secret", "short-token"):
        settings = Settings(
            _env_file=None,
            debug=False,
            secret_key="a-secure-secret-key-that-is-long-enough",
            mediamtx_auth_hook_secret="a-secure-hook-secret",
            booth_access_token=token,
        )
        try:
            settings.validate_production_secrets()
        except RuntimeError as exc:
            assert "BOOTH_ACCESS_TOKEN" in str(exc)
        else:
            raise AssertionError(f"weak BOOTH_ACCESS_TOKEN {token!r} must fail production validation")


def test_production_accepts_long_booth_access_token():
    settings = Settings(
        _env_file=None,
        debug=False,
        secret_key="a-secure-secret-key-that-is-long-enough",
        mediamtx_auth_hook_secret="a-secure-hook-secret",
        booth_access_token="b" * 32,
        api_key_encryption_key="k" * 32,
    )
    settings.validate_production_secrets()


def test_production_requires_an_encryption_key_for_queued_email():
    for key in (None, "", "change-this-encryption-key-in-production", "your-secure-32-char-key-here"):
        settings = Settings(
            _env_file=None,
            debug=False,
            secret_key="a-secure-secret-key-that-is-long-enough",
            mediamtx_auth_hook_secret="a-secure-hook-secret",
            booth_access_token="b" * 32,
            api_key_encryption_key=key,
        )
        try:
            settings.validate_production_secrets()
        except RuntimeError as exc:
            assert "API_KEY_ENCRYPTION_KEY" in str(exc)
        else:
            raise AssertionError(f"API_KEY_ENCRYPTION_KEY {key!r} must fail production validation")
