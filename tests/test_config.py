"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from portal.config import Settings


def test_effective_mediamtx_internal_base_fallback():
    s = Settings(mediamtx_internal_base="", mediamtx_api_base="http://localhost:9997")
    assert s.effective_mediamtx_internal_base == "http://localhost:9997"


def test_effective_mediamtx_internal_base_override():
    s = Settings(mediamtx_internal_base="http://mediamtx:8888")
    assert s.effective_mediamtx_internal_base == "http://mediamtx:8888"


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False


def test_effective_jitsi_internal_base_fallback():
    s = Settings(jitsi_internal_base="", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://jitsi.local"


def test_effective_jitsi_internal_base_override():
    s = Settings(jitsi_internal_base="http://internal.jitsi", jitsi_base_url="http://jitsi.local")
    assert s.effective_jitsi_internal_base == "http://internal.jitsi"


def test_transcription_worker_limit_can_be_set_from_environment(monkeypatch):
    monkeypatch.setenv("MAX_TRANSCRIPTION_WORKERS", "24")

    assert Settings(_env_file=None).max_transcription_workers == 24


def test_transcription_worker_limit_defaults_to_ten():
    assert Settings(_env_file=None).max_transcription_workers == 10


@pytest.mark.parametrize("limit", [0, -1])
def test_transcription_worker_limit_rejects_values_below_one(limit):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_transcription_workers=limit)
