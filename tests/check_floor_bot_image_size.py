"""Check the floor-bot base image's download budget before pulling its layers.

Run: python tests/check_floor_bot_image_size.py
The 900 MB budget allows headroom over the known-good 743 MB amd64 image.
This limits growth; it cannot guarantee free space on a deployment host.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

MAX_COMPRESSED_BYTES = 900_000_000
REGISTRY = "https://mcr.microsoft.com/v2/playwright/python"
MANIFEST_TYPES = ", ".join((
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
))


def fetch_manifest(reference: str) -> dict:
    """Resolve registry metadata without downloading browser image layers."""
    request = urllib.request.Request(
        f"{REGISTRY}/manifests/{reference}", headers={"Accept": MANIFEST_TYPES}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def compressed_size(manifest: dict) -> int:
    """Validate and sum registry layer sizes for the pre-pull budget."""
    layers = manifest["layers"]
    if not layers or any(not isinstance(layer["size"], int) or layer["size"] < 0 for layer in layers):
        raise ValueError("Registry returned invalid layer sizes")
    return sum(layer["size"] for layer in layers)


def main() -> None:
    """Reject base images that exceed the production architecture's growth limit."""
    dockerfile = (Path(__file__).resolve().parents[1] / "floor-bot/Dockerfile").read_text()
    match = re.search(r"^FROM\s+mcr\.microsoft\.com/playwright/python:([^\s]+)", dockerfile, re.MULTILINE)
    if match is None:
        raise ValueError("Expected a pinned Microsoft Playwright base image")
    manifest = fetch_manifest(match[1])
    if "manifests" in manifest:
        platforms = [entry for entry in manifest["manifests"]
                     if entry.get("platform", {}).get("os") == "linux"
                     and entry["platform"].get("architecture") == "amd64"]
        if len(platforms) != 1:
            raise ValueError("Expected exactly one Linux amd64 image for production")
        manifest = fetch_manifest(platforms[0]["digest"])
    size = compressed_size(manifest)
    print(f"Floor-bot Linux amd64 base image: {size:,} compressed bytes (budget {MAX_COMPRESSED_BYTES:,})")
    if size > MAX_COMPRESSED_BYTES:
        raise ValueError("Floor-bot image exceeds download budget; slim the image before upgrading")


if __name__ == "__main__":
    main()
