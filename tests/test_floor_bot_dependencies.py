from __future__ import annotations

import json
import math
import re
import shlex
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import floor_bot_container_smoke as smoke
import pytest
from floor_bot_container_smoke import assert_fixture_tone


def assert_playwright_versions_match(dockerfile: str) -> None:
    """Require the browser image and actual pip install instructions to agree."""
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    instructions = [line.strip().split(maxsplit=1) for line in logical_lines
                    if line.strip() and not line.lstrip().startswith("#")]
    image_versions = [match[1] for instruction in instructions
                      if instruction[0].upper() == "FROM"
                      and (match := re.fullmatch(
                          r"mcr\.microsoft\.com/playwright/python:v([\d.]+)-[\w-]+",
                          instruction[1]))]
    assert len(image_versions) == 1, "Expected one pinned Playwright browser image"
    sdk_versions = []
    for instruction, command in instructions:
        if instruction.upper() not in {"RUN", "CMD", "ENTRYPOINT"}:
            continue
        if command.startswith("["):
            argv = json.loads(command)
            command = argv[2] if argv[:2] in (["sh", "-c"], ["bash", "-c"]) else shlex.join(argv)
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
        commands: list[list[str]] = [[]]
        for token in tokens:
            if token in {";", "&&", "||", "|"}:
                commands.append([])
            else:
                commands[-1].append(token)
        for argv in commands:
            if argv[:1] in (["python"], ["python3"]) and argv[1:3] == ["-m", "pip"]:
                argv = argv[2:]
            if argv[:1] not in (["pip"], ["pip3"]) or argv[1:2] != ["install"]:
                continue
            for requirement in argv[2:]:
                if re.match(r"playwright(?:$|[=<>~!\[])", requirement, re.IGNORECASE):
                    pin = re.fullmatch(r"playwright==([\d.]+)", requirement, re.IGNORECASE)
                    assert pin, f"Playwright install must use an exact SDK pin: {requirement}"
                    sdk_versions.append(pin[1])
    assert sdk_versions, "Expected an explicitly pinned Playwright Python SDK install"
    assert all(version == image_versions[0] for version in sdk_versions), (
        f"Floor bot browser image {image_versions[0]} does not match SDK {sdk_versions}. "
        "Upgrade both together: mismatched Playwright versions cannot find Chromium."
    )


def test_floor_bot_playwright_sdk_matches_bundled_browsers() -> None:
    """Catch an image-only dependency upgrade before building the container."""
    assert_playwright_versions_match(
        (Path(__file__).resolve().parents[1] / "floor-bot/Dockerfile").read_text()
    )


@pytest.mark.parametrize("install", [
    'CMD ["sh", "-c", "pip install playwright && python bot.py"]',
    'CMD ["sh", "-c", "pip install playwright>=1.40.0 && python bot.py"]',
    'CMD ["sh", "-c", "pip install playwright==1.63.0 && python bot.py"]',
    'RUN echo "playwright==1.40.0"\nCMD ["python", "bot.py"]',
    'RUN echo pip install playwright==1.40.0\nCMD ["python", "bot.py"]',
    'CMD ["sh", "-c", "pip install playwright # playwright==1.40.0"]',
])
def test_comments_and_echoes_cannot_satisfy_sdk_pin(install: str) -> None:
    """Reject unpinned/mismatched installs even with a matching pin in dead text."""
    dockerfile = ('FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy\n'
                  '# Old command: pip install playwright==1.40.0\n' + install)
    with pytest.raises(AssertionError):
        assert_playwright_versions_match(dockerfile)


@pytest.mark.parametrize("install", [
    'RUN pip install --no-cache-dir playwright==1.40.0',
    'CMD ["sh", "-c", "pip install --no-cache-dir playwright==1.40.0 && python bot.py"]',
    'CMD ["pip", "install", "playwright==1.40.0"]',
    'RUN python -m pip install playwright==1.40.0',
])
def test_exact_executable_sdk_pin_is_accepted(install: str) -> None:
    """Accept supported shell and exec-form installs with an exact matching pin."""
    assert_playwright_versions_match(
        'FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy\n' + install
    )


@pytest.mark.parametrize("frequency", [0, 440, 880])
def test_audio_check_requires_fixture_frequency(frequency: int) -> None:
    """Accept the fixture tone while rejecting silence and another steady tone."""
    samples = [int(8000 * math.sin(2 * math.pi * frequency * index / 16000))
               for index in range(32000)]
    audio = struct.pack(f"<{len(samples)}h", *samples)
    if frequency == 440:
        assert_fixture_tone(audio)
    else:
        with pytest.raises(AssertionError):
            assert_fixture_tone(audio)


@pytest.mark.parametrize("kind", ["spike", "brief_tone", "constant", "short_capture"])
def test_audio_check_rejects_transients_and_invalid_captures(kind: str) -> None:
    """Prevent startup noise or incomplete recordings from declaring audio healthy."""
    samples = [0] * 32000
    if kind == "spike":
        samples[0] = 32767
    elif kind == "brief_tone":
        samples[:4000] = [int(8000 * math.sin(2 * math.pi * 440 * index / 16000))
                         for index in range(4000)]
    elif kind == "constant":
        samples = [8000] * 32000
    else:
        samples = samples[:4000]
    with pytest.raises(AssertionError):
        assert_fixture_tone(struct.pack(f"<{len(samples)}h", *samples))


def test_rtsp_capture_recovers_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow initial reader must not fail the smoke before the publisher is ready."""
    clock = [0.0]
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout=b"decoded audio", stderr=b"")

    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    ))
    monkeypatch.setattr(smoke.subprocess, "run", run)
    assert smoke.capture_published_audio("rtsp://test/floor") == b"decoded audio"
    assert calls == [15, 14.5]


@pytest.mark.parametrize("failure", ["timeout", "timeout_with_stderr", "exit"])
def test_rtsp_capture_reports_latest_failure_at_deadline(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """Both timeout and exit diagnostics survive retries until the total deadline."""
    clock = [0.0]
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"old failure")
        if failure.startswith("timeout"):
            stderr = b"latest timeout detail" if failure == "timeout_with_stderr" else None
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=stderr)
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"latest exit detail")

    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    ))
    monkeypatch.setattr(smoke.subprocess, "run", run)
    with pytest.raises(AssertionError) as error:
        smoke.capture_published_audio("rtsp://test/floor")
    detail = str(error.value)
    assert "old failure" not in detail
    assert ("timed out" if failure.startswith("timeout") else "latest exit detail") in detail
    if failure == "timeout_with_stderr":
        assert "latest timeout detail" in detail
    assert calls == [15, 14.5]
    assert clock[0] == 30
