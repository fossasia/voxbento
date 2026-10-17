"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

import pytest

from portal.config import Settings


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False


def test_effective_jitsi_internal_base_fallback():
    s = Settings(jitsi_internal_base="", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://jitsi.local"


def test_effective_jitsi_internal_base_override():
    s = Settings(jitsi_internal_base="http://internal.jitsi", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://internal.jitsi"


def test_legacy_admin_password_environment_variable_is_ignored(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "legacy-shared-secret")

    settings = Settings(_env_file=None)

    assert "admin_password" not in type(settings).model_fields


@pytest.mark.parametrize(
    ("public_base_url", "expected"),
    [("https://voxbento.com", True), ("HTTPS://voxbento.com", True), ("http://localhost:8000", False)],
)
def test_is_https_reads_the_public_base_url_scheme(public_base_url, expected):
    assert Settings(public_base_url=public_base_url).is_https is expected
