from __future__ import annotations

import re
from pathlib import Path


def test_floor_bot_playwright_sdk_matches_bundled_browsers() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "floor-bot/Dockerfile").read_text()
    image_versions = re.findall(
        r"^FROM\s+mcr\.microsoft\.com/playwright/python:v([\d.]+)-[\w-]+",
        dockerfile,
        flags=re.MULTILINE,
    )
    sdk_versions = re.findall(r"playwright==([\d.]+)", dockerfile)
    assert len(image_versions) == 1, "Expected one pinned Playwright browser image"
    assert sdk_versions, "Expected an explicitly pinned Playwright Python SDK"
    assert all(version == image_versions[0] for version in sdk_versions), (
        f"Floor bot browser image {image_versions[0]} does not match SDK {sdk_versions}. "
        "Upgrade both together: mismatched Playwright versions cannot find Chromium."
    )
