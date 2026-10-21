"""Exercise production deployment failures without contacting a server or Docker."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Deploy Test", "-c", "user.email=deploy@example.test", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[Path, Path, str, dict[str, str]]:
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    (remote / "release.txt").write_text("old")
    (remote / "Caddyfile").write_text("candidate config")
    (remote / ".gitignore").write_text(".env\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "Old release")
    live = tmp_path / "live"
    git(tmp_path, "clone", str(remote), str(live))
    (live / ".env").write_text("SECRET_KEY=test-only\n")
    (remote / "release.txt").write_text("new")
    git(remote, "commit", "-am", "Candidate release")
    candidate = git(remote, "rev-parse", "HEAD")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = """#!/bin/sh
name=$(basename "$0")
printf '%s|%s|%s|%s\\n' "$name" "$*" "$PWD" "$(cat "$LIVE/release.txt")" >> "$CALLS"
case "$name:$*" in
  docker:*build*) stage=build ;;
  docker:*run*) stage=preflight
    test "$(cat release.txt)" = new || exit 91
    test -f .env || exit 92 ;;
  sudo:*validate*) stage=caddy ;;
  docker:*"volume inspect"*) exit 1 ;;
  curl:*)
    if [ "$FAIL_STAGE" = health ]; then printf 503; else printf 200; fi
    exit 0 ;;
  *) stage=other ;;
esac
if [ "$FAIL_STAGE" = "$stage" ]; then exit 42; fi
"""
    for command in ("docker", "sudo", "curl", "sleep"):
        executable = binaries / command
        executable.write_text(stub)
        executable.chmod(0o755)
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = workflow["jobs"]["deploy"]["steps"][1]["with"]["script"]
    assert "cd /home/ubuntu/voxbento" in script
    script = script.replace("cd /home/ubuntu/voxbento", f"cd {shlex.quote(str(live))}")
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "LIVE": str(live),
        "CALLS": str(tmp_path / "calls"),
        "TMPDIR": str(tmp_path),
        "FAIL_STAGE": "",
    }
    return live, tmp_path, candidate, {**env, "DEPLOY_SCRIPT": script}


def run_deployment(env: dict[str, str], stage: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", env["DEPLOY_SCRIPT"]],
        env={**env, "FAIL_STAGE": stage},
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.parametrize("stage", ["build", "preflight", "caddy"])
def test_failed_preflight_leaves_live_release_untouched(
    deployment: tuple[Path, Path, str, dict[str, str]],
    stage: str,
) -> None:
    live, temporary, _, env = deployment
    old_commit = git(live, "rev-parse", "HEAD")
    result = run_deployment(env, stage)
    assert result.returncode == 42, result.stdout + result.stderr
    assert git(live, "rev-parse", "HEAD") == old_commit
    assert (live / "release.txt").read_text() == "old"
    calls = (temporary / "calls").read_text()
    assert " up " not in calls
    assert "sudo|cp " not in calls
    assert "image prune" not in calls
    assert not list(temporary.glob("voxbento-deploy.*"))
    assert len(git(live, "worktree", "list").splitlines()) == 1


def test_successful_deploy_uses_candidate_then_preserves_project_identity(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    live, temporary, candidate, env = deployment
    result = run_deployment(env, "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(live, "rev-parse", "HEAD") == candidate
    calls = (temporary / "calls").read_text().splitlines()
    compose_calls = [call.split("|") for call in calls if "docker|compose " in call]
    assert compose_calls
    assert all(call[1].startswith("compose --project-name voxbento ") for call in compose_calls)
    preflight = next(call for call in compose_calls if " run " in call[1])
    assert preflight[2] != str(live)
    assert preflight[3] == "old"
    replace = next(call for call in compose_calls if " up " in call[1])
    assert "up -d --no-build --remove-orphans" in replace[1]
    assert replace[2:] == [str(live), "new"]
    health = next(i for i, call in enumerate(calls) if call.startswith("curl|"))
    prune = next(i for i, call in enumerate(calls) if "image prune" in call)
    assert health < prune
    assert not list(temporary.glob("voxbento-deploy.*"))


def test_unhealthy_replacement_retains_rollback_images(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "health")
    assert result.returncode == 1, result.stdout + result.stderr
    calls = (temporary / "calls").read_text()
    assert "logs portal --tail=50" in calls
    assert "image prune" not in calls
    assert not list(temporary.glob("voxbento-deploy.*"))


def test_portal_uses_image_code_and_keeps_persistent_data() -> None:
    portal = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["portal"]
    assert portal["volumes"] == ["portal-data:/data", "supertonic-cache:/root/.cache"]
    assert portal["env_file"] == [{"path": ".env", "required": False}]
    assert ".env" in (ROOT / ".dockerignore").read_text().splitlines()
