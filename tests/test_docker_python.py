"""Keep the portal image's Python in line with requires-python (#1131)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def satisfies(version: str, requires: str) -> bool:
    checks = {">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "==": lambda a, b: a == b}
    for spec in requires.split(","):
        op, bound = re.match(r"\s*(>=|<|==)\s*([\d.]+)", spec).groups()
        if not checks[op](as_tuple(version), as_tuple(bound)):
            return False
    return True


def base_image_python() -> str:
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"^FROM python:(\d+\.\d+)", dockerfile, re.MULTILINE)
    assert match, "root Dockerfile should use an official python base image"
    return match.group(1)


def test_base_image_python_matches_requires_python():
    requires = (tomllib.loads((ROOT / "pyproject.toml").read_text()))["project"]["requires-python"]
    version = base_image_python()
    assert satisfies(version, requires), (
        f"Dockerfile uses python:{version} but pyproject.toml requires {requires}; "
        "uv would download a second Python during the build"
    )


def test_uv_python_downloads_disabled():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^ENV UV_PYTHON_DOWNLOADS=never$", dockerfile, re.MULTILINE)


def test_python_version_file_not_copied_into_image():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".python-version" in ignored


def test_dependabot_does_not_bump_base_image_past_requires_python():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    root_docker = next(u for u in config["updates"] if u["package-ecosystem"] == "docker" and u["directory"] == "/")
    ignores = {i["dependency-name"]: i.get("versions", []) for i in root_docker.get("ignore", [])}
    assert ignores.get("python") == [">=3.14"]
