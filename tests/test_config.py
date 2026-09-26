"""Unit tests for portal.config.Settings derived properties."""

from __future__ import annotations

from portal.config import Settings


def test_debug_defaults_false():
    assert Settings(_env_file=None).debug is False
