"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

from portal.config import Settings


def test_effective_mediamtx_internal_base_fallback():
    s = Settings(mediamtx_internal_base="", mediamtx_api_base="http://localhost:9997")
    assert s.effective_mediamtx_internal_base == "http://localhost:9997"


def test_effective_mediamtx_internal_base_override():
    s = Settings(mediamtx_internal_base="http://mediamtx:8888")
    assert s.effective_mediamtx_internal_base == "http://mediamtx:8888"


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False
