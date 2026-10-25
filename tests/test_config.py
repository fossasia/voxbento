"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from portal.config import Settings


def test_debug_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings model defaults to production-safe debug behavior."""
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("debug", raising=False)
    assert Settings(_env_file=None).debug is False


@pytest.fixture
def example_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the committed example without allowing the host environment to leak in."""
    # Exercise the committed example, not a developer's .env or shell overrides.
    for name in tuple(os.environ):
        if name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)
    return Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_disables_debug_by_default(example_env: Path) -> None:
    """The committed example keeps debug disabled."""
    assert Settings(_env_file=example_env).debug is False


def test_env_example_allows_explicit_local_debug(example_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Developers can explicitly enable debug in their local environment."""
    monkeypatch.setenv("DEBUG", "true")
    assert Settings(_env_file=example_env).debug is True


def test_env_example_enforces_production_secret_checks(example_env: Path) -> None:
    """The example's placeholder secrets fail production validation."""
    settings = Settings(_env_file=example_env)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        settings.validate_production_secrets()


def test_env_example_accepts_configured_secrets(example_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured test secrets pass production validation while debug stays off."""
    for name in ("SECRET_KEY", "API_KEY_ENCRYPTION_KEY", "MEDIAMTX_AUTH_HOOK_SECRET", "BOOTH_ACCESS_TOKEN"):
        monkeypatch.setenv(name, f"test-only-{name.lower()}-not-a-real-secret-00000000")
    settings = Settings(_env_file=example_env)
    assert settings.debug is False
    settings.validate_production_secrets()


def test_effective_jitsi_internal_base_fallback():
    """The internal Jitsi URL falls back to the public Jitsi base URL."""
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
    for key in (
        None,
        "",
        "short-key",
        ", ,",
        "change-this-encryption-key-in-production",
        "your-secure-32-char-key-here",
    ):
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
