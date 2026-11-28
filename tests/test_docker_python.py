"""Keep the portal image's Python in line with requires-python (#1131)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).parents[1]


def requires_python() -> SpecifierSet:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return SpecifierSet(project["requires-python"])


def base_image_python() -> str:
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"^FROM python:(\d+\.\d+)", dockerfile, re.MULTILINE)
    assert match, "root Dockerfile should use an official python base image"
    return match.group(1)


def test_base_image_python_matches_requires_python():
    requires = requires_python()
    version = base_image_python()
    assert requires.contains(f"{version}.0"), (
        f"Dockerfile uses python:{version} but pyproject.toml requires {requires}; "
        "uv would download a second Python during the build"
    )


def test_uv_python_downloads_disabled_before_uv_sync():
    dockerfile = (ROOT / "Dockerfile").read_text()
    downloads = re.search(r"^ENV UV_PYTHON_DOWNLOADS=never$", dockerfile, re.MULTILINE)
    first_sync = re.search(r"^RUN uv sync\b", dockerfile, re.MULTILINE)
    assert downloads and first_sync
    assert downloads.start() < first_sync.start()


def test_python_version_file_not_copied_into_image():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".python-version" in ignored


def test_dependabot_does_not_bump_base_image_past_requires_python():
    upper = [spec.version for spec in requires_python() if spec.operator == "<"]
    assert len(upper) == 1, "requires-python should have exactly one '<' upper bound"
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    root_docker = next(u for u in config["updates"] if u["package-ecosystem"] == "docker" and u["directory"] == "/")
    ignores = {i["dependency-name"]: i.get("versions", []) for i in root_docker.get("ignore", [])}
    assert ignores.get("python") == [f">={upper[0]}"]
