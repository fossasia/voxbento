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
  docker:*"config --environment"*) stage=other
    printf '%b' "$COMPOSE_ENV" ;;
  docker:*" up "*) stage=up ;;
  docker:*"image prune"*) stage=prune ;;
  docker:*build*) stage=build
    test -z "$EXPECT_FILE" || test -f "$EXPECT_FILE" || exit 93 ;;
  docker:*run*) stage=preflight
    test "$(cat release.txt)" = new || exit 91
    test -f .env || exit 92 ;;
  sudo:*"validate --config /etc/caddy/Caddyfile"*) stage=hostcaddy ;;
  sudo:*validate*) stage=caddy
    cp "$4" "$TMPDIR/rendered.caddy" ;;
  sudo:"grep -qxF"*) stage=other
    test -z "$LEGACY_MAIN" || exit 1 ;;
  sudo:"cat /etc/caddy/Caddyfile") stage=other
    printf '%b' "$LEGACY_MAIN" ;;
  sudo:"test -f"*) stage=other
    test "$PREVIOUS_SITE" != absent || exit 1 ;;
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
        "COMPOSE_ENV": "",
        "LEGACY_MAIN": "",
        "PREVIOUS_SITE": "",
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
    assert calls[rename].split("|")[1] == ("mv -f /etc/caddy/sites/voxbento.caddy.next /etc/caddy/sites/voxbento.caddy")
    assert not any("/etc/caddy/Caddyfile.next" in call for call in calls)
    assert "curl|-s -o /dev/null -w %{http_code} http://localhost:8000/healthz" in "\n".join(calls)
    prune_call = calls[next(i for i, call in enumerate(calls) if "image prune" in call)]
    assert "--filter label=com.docker.compose.project=voxbento" in prune_call
    reload = next(i for i, call in enumerate(calls) if call.startswith("sudo|systemctl reload caddy|"))
    up = next(i for i, call in enumerate(calls) if " up " in call)
    # Caddy switches only once the replacement portal is healthy, before cleanup.
    assert up < health < install < rename < reload < prune
    assert "sudo|cp " not in "\n".join(calls)
    assert not list(temporary.glob("voxbento-deploy.*"))


@pytest.mark.parametrize("stage", ["install", "rename", "reload"])
def test_failed_caddy_activation_fails_deploy_and_keeps_rollback_images(
    deployment: tuple[Path, Path, str, dict[str, str]],
    stage: str,
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, stage)
    assert result.returncode == 42, result.stdout + result.stderr
    calls = (temporary / "calls").read_text()
    health = calls.index("curl|")
    assert calls.index(" up ") < health < calls.index("sudo|install")
    assert "image prune" not in calls
    if stage == "install":
        # Nothing on disk changed yet: no restore, no reload.
        assert "systemctl reload" not in calls
        assert "Caddy activation failed" not in result.stdout
    else:
        assert "Caddy activation failed" in result.stdout
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
    # Caddy keeps routing to the configuration it had; the new site was never activated.
    changes = ("install", "mv", "rm", "tee", "systemctl")
    assert not any(call.startswith(changes) for call in sudo_calls(temporary))
    assert not list(temporary.glob("voxbento-deploy.*"))


def test_portal_uses_image_code_and_keeps_persistent_data() -> None:
    portal = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["portal"]
    assert portal["volumes"] == ["portal-data:/data", "supertonic-cache:/root/.cache"]
    assert portal["env_file"] == [{"path": ".env", "required": False}]
    assert ".env" in (ROOT / ".dockerignore").read_text().splitlines()


LEGACY_MAIN = (
    "# Caddyfile \u2014 voxbento.com production\\nvoxbento.com {\\n    reverse_proxy localhost:8000 {\\n    }\\n}\\n"
)


def sudo_calls(temporary: Path) -> list[str]:
    lines = (temporary / "calls").read_text().splitlines()
    return [line.split("|")[1] for line in lines if line.startswith("sudo|")]


def test_portal_host_port_sets_health_check_and_caddy_upstream(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    # Compose resolves the .env value (quotes, spaces, comments); the stub returns its result.
    result = run_deployment(env, "", COMPOSE_ENV="SECRET_KEY=test-only\\nPORTAL_HOST_PORT=8001\\n")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (temporary / "calls").read_text()
    assert "config --environment" in calls
    assert "http://localhost:8001/healthz" in calls
    assert "localhost:8000/healthz" not in calls
    rendered = (temporary / "rendered.caddy").read_text()
    assert "reverse_proxy localhost:8001 {" in rendered
    assert "localhost:8000" not in rendered


def test_compose_resolves_portal_host_port_like_published_port(tmp_path: Path) -> None:
    """The workflow trusts `docker compose config --environment`; check that it parses .env."""
    try:
        version = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
    except FileNotFoundError:
        pytest.skip("Docker is not installed")
    if version.returncode != 0:
        pytest.skip("Docker Compose is not installed")
    (tmp_path / "compose.yml").write_text(
        'services:\n  a:\n    image: busybox\n    ports: ["127.0.0.1:${PORTAL_HOST_PORT:-8000}:8000"]\n'
    )
    (tmp_path / ".env").write_text("PORTAL_HOST_PORT = '8001' # moved off 8000\n")
    environment = {k: v for k, v in os.environ.items() if k != "PORTAL_HOST_PORT"}
    result = subprocess.run(
        ["docker", "compose", "config", "--environment"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if "unknown flag" in result.stderr:
        pytest.skip("This Docker Compose version has no `config --environment`")
    assert result.returncode == 0, result.stderr
    assert "PORTAL_HOST_PORT=8001" in result.stdout.splitlines()


def test_invalid_portal_host_port_stops_before_build(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    live, temporary, _, env = deployment
    result = run_deployment(env, "", COMPOSE_ENV="PORTAL_HOST_PORT=80; reboot\\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "PORTAL_HOST_PORT must be a port number" in result.stdout
    calls = (temporary / "calls").read_text()
    assert " build" not in calls
    assert "sudo|" not in calls
    assert git(live, "status", "--porcelain") == ""
    assert not list(temporary.glob("voxbento-deploy.*"))


@pytest.mark.parametrize("override", ["docker-compose.local.yml", "ops/local.yml"])
def test_host_local_compose_override_reaches_candidate(
    deployment: tuple[Path, Path, str, dict[str, str]],
    override: str,
) -> None:
    live, _, _, env = deployment
    (live / ".env").write_text(f"SECRET_KEY=test-only\nCOMPOSE_FILE = docker-compose.yml:{override} # local\n")
    (live / override).parent.mkdir(parents=True, exist_ok=True)
    (live / override).write_text("services: {}\n")
    result = run_deployment(env, "", EXPECT_FILE=override)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Using host-local {override}" in result.stdout


def test_legacy_main_caddyfile_is_replaced_by_sites_import(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "", LEGACY_MAIN=LEGACY_MAIN)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = sudo_calls(temporary)
    backup = calls.index("install -m 644 /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sites")
    main = calls.index("mv -f /etc/caddy/Caddyfile.next /etc/caddy/Caddyfile")
    site = calls.index("mv -f /etc/caddy/sites/voxbento.caddy.next /etc/caddy/sites/voxbento.caddy")
    reload = calls.index("systemctl reload caddy")
    assert backup < main < site < reload


@pytest.mark.parametrize(
    ("main", "reported"),
    [
        ("other.example {\\n    respond 200\\n}\\n", "other.example {"),
        (LEGACY_MAIN + "app.example.org {\\n    reverse_proxy localhost:9000\\n}\\n", "app.example.org {"),
        ("{\\n    email ops@example.org\\n}\\n" + LEGACY_MAIN, "{"),
        # Braces, not indentation, delimit Caddy blocks.
        (LEGACY_MAIN + "    app.example.org {\\n        respond 200\\n    }\\n", "app.example.org {"),
        (LEGACY_MAIN + "\\tapp.example.org  {  # other app\\n\\t\\trespond 200\\n\\t}\\n", "app.example.org {"),
    ],
)
def test_main_caddyfile_with_other_entries_stops_before_any_change(
    deployment: tuple[Path, Path, str, dict[str, str]],
    main: str,
    reported: str,
) -> None:
    live, temporary, _, env = deployment
    old_commit = git(live, "rev-parse", "HEAD")
    result = run_deployment(env, "", LEGACY_MAIN=main)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot be replaced automatically" in result.stdout
    assert f"  {reported}" in result.stdout.splitlines()
    calls = sudo_calls(temporary)
    assert not any(call.startswith(("install", "mv", "rm", "systemctl")) for call in calls)
    assert git(live, "rev-parse", "HEAD") == old_commit
    assert " up " not in (temporary / "calls").read_text()


@pytest.mark.parametrize("previous", ["present", "absent"])
def test_invalid_host_caddy_config_is_restored(
    deployment: tuple[Path, Path, str, dict[str, str]],
    previous: str,
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "hostcaddy", LEGACY_MAIN=LEGACY_MAIN, PREVIOUS_SITE=previous)
    assert result.returncode == 42, result.stdout + result.stderr
    calls = sudo_calls(temporary)
    validate = calls.index("caddy validate --config /etc/caddy/Caddyfile")
    after = calls[validate + 1 :]
    restore_site = "mv -f /etc/caddy/sites/voxbento.caddy.previous /etc/caddy/sites/voxbento.caddy"
    remove_site = "rm -f /etc/caddy/sites/voxbento.caddy"
    if previous == "present":
        assert restore_site in after
        assert remove_site not in after
    else:
        assert "rm -f /etc/caddy/sites/voxbento.caddy.previous" in calls[:validate]
        assert remove_site in after
        assert restore_site not in after
    assert "mv -f /etc/caddy/Caddyfile.before-sites /etc/caddy/Caddyfile" in after
    # Caddy is reloaded only once, with the restored files.
    assert calls.count("systemctl reload caddy") == 1
    assert after.index("systemctl reload caddy") == len(after) - 1
    assert "image prune" not in (temporary / "calls").read_text()


def test_failed_container_replacement_leaves_caddy_untouched(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "up")
    assert result.returncode == 42, result.stdout + result.stderr
    changes = ("install", "mv", "rm", "tee", "systemctl")
    assert not any(call.startswith(changes) for call in sudo_calls(temporary))


def test_failure_after_caddy_activation_keeps_it(
    deployment: tuple[Path, Path, str, dict[str, str]],
) -> None:
    _, temporary, _, env = deployment
    result = run_deployment(env, "prune")
    assert result.returncode == 42, result.stdout + result.stderr
    calls = sudo_calls(temporary)
    # The active site matches the healthy portal; a cleanup failure must not roll it back.
    assert calls.count("systemctl reload caddy") == 1
    assert "mv -f /etc/caddy/sites/voxbento.caddy.previous /etc/caddy/sites/voxbento.caddy" not in calls
    assert "Caddy activation failed" not in result.stdout


def test_mediamtx_public_hosts_come_from_environment() -> None:
    mediamtx = yaml.safe_load((ROOT / "mediamtx.yml").read_text())
    assert mediamtx["webrtcAdditionalHosts"] == ["127.0.0.1"]
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["mediamtx"]
    hosts = service["environment"]["MTX_WEBRTCADDITIONALHOSTS"]
    assert hosts == "127.0.0.1,${MEDIAMTX_PUBLIC_HOSTS:-${JVB_ADVERTISE_IPS:-127.0.0.1}}"
