"""Exercise production deployment failures without contacting a server or Docker."""

from __future__ import annotations

import os
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
    (remote / "Caddyfile").write_text("voxbento.com {\n    reverse_proxy localhost:8000 {\n    }\n}\n")
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
  docker:*build*) stage=build
    test -z "$EXPECT_FILE" || test -f "$EXPECT_FILE" || exit 93 ;;
  docker:*run*) stage=preflight
    test "$(cat release.txt)" = new || exit 91
    test -f .env || exit 92 ;;
  sudo:*"validate --config /etc/caddy/Caddyfile"*) stage=hostcaddy ;;
  sudo:*validate*) stage=caddy
    cp "$4" "$TMPDIR/rendered.caddy" ;;
  sudo:"grep -qxF"*) stage=other
    case "$CADDY_MAIN" in legacy|other) exit 1 ;; esac ;;
  sudo:"grep -qF"*) stage=other
    if [ "$CADDY_MAIN" = other ]; then exit 1; fi ;;
  sudo:install*) stage=install ;;
  sudo:mv*) stage=rename ;;
  sudo:*reload*) stage=reload ;;
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
    assert 'cd "${VOXBENTO_DIR:-$HOME/voxbento}"' in script
    env = {
        "VOXBENTO_DIR": str(live),
        "HOME": str(tmp_path),
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "LIVE": str(live),
        "CALLS": str(tmp_path / "calls"),
        "TMPDIR": str(tmp_path),
        "FAIL_STAGE": "",
        "CADDY_MAIN": "",
        "EXPECT_FILE": "",
    }
    return live, tmp_path, candidate, {**env, "DEPLOY_SCRIPT": script}


def run_deployment(env: dict[str, str], stage: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", env["DEPLOY_SCRIPT"]],
        env={**env, "FAIL_STAGE": stage, **extra},
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
    install = next(i for i, call in enumerate(calls) if call.startswith("sudo|install -m 644 "))
    rename = next(i for i, call in enumerate(calls) if call.startswith("sudo|mv -f "))
    assert "/etc/caddy/sites/voxbento.caddy.next" in calls[install]
    assert calls[rename].split("|")[1] == (
        "mv -f /etc/caddy/sites/voxbento.caddy.next /etc/caddy/sites/voxbento.caddy"
    )
    assert not any("/etc/caddy/Caddyfile.next" in call for call in calls)
    assert "curl|-s -o /dev/null -w %{http_code} http://localhost:8000/healthz" in "\n".join(calls)
    prune_call = calls[next(i for i, call in enumerate(calls) if "image prune" in call)]
    assert "--filter label=com.docker.compose.project=voxbento" in prune_call
    reload = next(i for i, call in enumerate(calls) if call.startswith("sudo|systemctl reload caddy|"))
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert install < rename < reload < up
    assert "sudo|cp " not in "\n".join(calls)
    assert not list(temporary.glob("voxbento-deploy.*"))


@pytest.mark.parametrize("stage", ["install", "rename", "reload"])
def test_failed_caddy_install_stops_container_replacement(
    deployment: tuple[Path, Path, str, dict[str, str]],
    stage: str,
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, stage)
    assert result.returncode == 42, result.stdout + result.stderr
    calls = (temporary / "calls").read_text()
    assert " up " not in calls
    assert "image prune" not in calls
    if stage in {"install", "rename"}:
        assert "systemctl reload" not in calls
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


def test_portal_host_port_sets_health_check_and_caddy_upstream(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    live, temporary, _, env = deployment
    (live / ".env").write_text("SECRET_KEY=test-only\nPORTAL_HOST_PORT='8001'\n")
    result = run_deployment(env, "")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (temporary / "calls").read_text()
    assert "http://localhost:8001/healthz" in calls
    assert "localhost:8000/healthz" not in calls
    rendered = (temporary / "rendered.caddy").read_text()
    assert "reverse_proxy localhost:8001 {" in rendered
    assert "localhost:8000" not in rendered


def test_invalid_portal_host_port_stops_before_build(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    live, temporary, _, env = deployment
    (live / ".env").write_text("SECRET_KEY=test-only\nPORTAL_HOST_PORT=80; reboot\n")
    result = run_deployment(env, "")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "PORTAL_HOST_PORT must be a port number" in result.stdout
    assert not (temporary / "calls").exists()


def test_host_local_compose_override_reaches_candidate(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    live, _, _, env = deployment
    (live / ".env").write_text(
        "SECRET_KEY=test-only\nCOMPOSE_FILE=docker-compose.yml:docker-compose.local.yml\n"
    )
    (live / "docker-compose.local.yml").write_text("services: {}\n")
    result = run_deployment(env, "", EXPECT_FILE="docker-compose.local.yml")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Using host-local docker-compose.local.yml" in result.stdout


def test_legacy_main_caddyfile_is_replaced_by_sites_import(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "", CADDY_MAIN="legacy")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [call.split("|")[1] for call in (temporary / "calls").read_text().splitlines()]
    backup = calls.index("install -m 644 /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sites")
    main = calls.index("mv -f /etc/caddy/Caddyfile.next /etc/caddy/Caddyfile")
    site = calls.index("mv -f /etc/caddy/sites/voxbento.caddy.next /etc/caddy/sites/voxbento.caddy")
    reload = calls.index("systemctl reload caddy")
    assert backup < main < site < reload


def test_unmanaged_main_caddyfile_stops_deploy(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "", CADDY_MAIN="other")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is not managed by VoxBento" in result.stdout
    calls = (temporary / "calls").read_text()
    assert "/etc/caddy/Caddyfile.next" not in calls
    assert "systemctl reload" not in calls
    assert " up " not in calls


def test_invalid_host_caddy_config_is_restored(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "hostcaddy", CADDY_MAIN="legacy")
    assert result.returncode == 1, result.stdout + result.stderr
    calls = [call.split("|")[1] for call in (temporary / "calls").read_text().splitlines()]
    assert "mv -f /etc/caddy/sites/voxbento.caddy.previous /etc/caddy/sites/voxbento.caddy" in calls
    assert "mv -f /etc/caddy/Caddyfile.before-sites /etc/caddy/Caddyfile" in calls
    assert "systemctl reload caddy" not in calls
    assert not any(" up " in call for call in calls)


def test_mediamtx_public_hosts_come_from_environment() -> None:
    mediamtx = yaml.safe_load((ROOT / "mediamtx.yml").read_text())
    assert mediamtx["webrtcAdditionalHosts"] == ["127.0.0.1"]
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["mediamtx"]
    hosts = service["environment"]["MTX_WEBRTCADDITIONALHOSTS"]
    assert hosts == "127.0.0.1,${MEDIAMTX_PUBLIC_HOSTS:-${JVB_ADVERTISE_IPS:-127.0.0.1}}"
