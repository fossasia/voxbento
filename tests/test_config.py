"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

from portal.config import Settings


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False


def test_effective_jitsi_internal_base_fallback():
    s = Settings(jitsi_internal_base="", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://jitsi.local"


def test_effective_jitsi_internal_base_override():
    s = Settings(jitsi_internal_base="http://internal.jitsi", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://internal.jitsi"
