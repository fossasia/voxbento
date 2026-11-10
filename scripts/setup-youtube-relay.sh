#!/usr/bin/env bash
# Turn on YouTube relay mode (#749) for this VoxBento deployment. See docs/program-relay.md.
#
#   scripts/setup-youtube-relay.sh [--ingest-only] [--host voxbento.com] [--cert-dir DIR --cert-name NAME] [--yes]
#
# Run from the repository root on the server. It is safe to run again: existing values are
# kept. It writes the settings into .env (never printing secrets), finds the TLS certificate
# Caddy already manages for the host, validates the Compose configuration, then (after
# confirmation, or with --yes) starts the services and checks that they answer.
# --ingest-only opens only the authenticated RTMPS listener (docker-compose.rtmps.yml) so a
# streaming tool can add VoxBento as an extra destination; no program-relay, YouTube stays independent.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=.env
RELAY_COMPOSE=docker-compose.relay.yml
RTMPS_COMPOSE=docker-compose.rtmps.yml
host="" cert_dir="" cert_name="" assume_yes=0 ingest_only=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --ingest-only) ingest_only=1; shift ;;
    --host) host=$2; shift 2 ;;
    --cert-dir) cert_dir=$2; shift 2 ;;
    --cert-name) cert_name=$2; shift 2 ;;
    --yes|-y) assume_yes=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed "s/^[\"']//; s/[\"']$//" || true; }
env_set() {
  # awk reads the value from the environment, so paths and secrets need no escaping.
  # The new file is written next to .env and renamed over it, so an interrupted run never
  # leaves a truncated .env. It keeps .env's mode (600, set below) and, as root, its owner.
  local tmp owner
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  if ! KEY=$1 VALUE=$2 awk 'BEGIN { done = 0 }
    index($0, ENVIRON["KEY"] "=") == 1 { if (!done) print ENVIRON["KEY"] "=" ENVIRON["VALUE"]; done = 1; next }
    { print }
    END { if (!done) print ENVIRON["KEY"] "=" ENVIRON["VALUE"] }' "$ENV_FILE" > "$tmp"; then
    rm -f "$tmp"
    fail "Could not update $ENV_FILE"
  fi
  chmod 600 "$tmp"
  if [ "$(id -u)" -eq 0 ]; then
    owner=$(stat -c %u:%g "$ENV_FILE" 2>/dev/null || stat -f %u:%g "$ENV_FILE")
    chown "$owner" "$tmp"
  fi
  mv -f "$tmp" "$ENV_FILE"
}
secret() { openssl rand -hex 32; }
sudo_if_needed() { if "$@" 2>/dev/null; then return 0; fi; command -v sudo >/dev/null && sudo "$@"; }

command -v docker >/dev/null || fail "docker is not installed."
command -v openssl >/dev/null || fail "openssl is not installed."
# Relay mode already opens the RTMPS listener, so the two override files are never combined.
if [ "$ingest_only" -eq 1 ]; then
  OVERRIDE=$RTMPS_COMPOSE OTHER=$RELAY_COMPOSE
else
  OVERRIDE=$RELAY_COMPOSE OTHER=$RTMPS_COMPOSE
fi
[ -f "$OVERRIDE" ] || fail "Run this from the VoxBento checkout ($OVERRIDE not found)."
[ -f "$ENV_FILE" ] || fail "$ENV_FILE not found. Create it first (see README.md)."
umask 077
# .env holds every production secret; make sure no other local user can read it.
if [ -n "$(find "$ENV_FILE" -perm /077 2>/dev/null || find "$ENV_FILE" -perm +077 2>/dev/null)" ]; then
  chmod 600 "$ENV_FILE"
  echo "Restricted $ENV_FILE to its owner (chmod 600)"
fi

step "Public hostname"
if [ -z "$host" ]; then
  base=$(env_get PUBLIC_BASE_URL)
  base=${base:-https://voxbento.com}
  host=${base#*://}; host=${host%%/*}; host=${host%%:*}
fi
[[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || fail "Could not work out a valid hostname; pass --host."
echo "Encoders will connect to rtmps://$host:1936"

step "Secrets and switches in $ENV_FILE"
if [ -z "$(env_get MEDIAMTX_AUTH_HOOK_SECRET)" ]; then
  env_set MEDIAMTX_AUTH_HOOK_SECRET "$(secret)"
  echo "Generated MEDIAMTX_AUTH_HOOK_SECRET"
fi
if [ "$ingest_only" -ne 1 ]; then
  control=$(env_get PROGRAM_RELAY_CONTROL_SECRET)
  if [ "${#control}" -lt 32 ]; then
    env_set PROGRAM_RELAY_CONTROL_SECRET "$(secret)"
    echo "Generated PROGRAM_RELAY_CONTROL_SECRET"
  fi
fi
env_set PROGRAM_INGEST_ENABLED true
[ -n "$(env_get PROGRAM_RTMPS_PUBLIC_BASE)" ] || env_set PROGRAM_RTMPS_PUBLIC_BASE "rtmps://$host:1936"
echo "PROGRAM_RTMPS_PUBLIC_BASE=$(env_get PROGRAM_RTMPS_PUBLIC_BASE)"

step "TLS certificate for $host"
if [ -z "$cert_dir" ]; then
  cert_dir=$(env_get PROGRAM_RTMPS_CERT_DIR)
  cert_name=${cert_name:-$(env_get PROGRAM_RTMPS_CERT_NAME)}
fi
if [ -z "$cert_dir" ]; then
  # Caddy (systemd package, or run as the current user) keeps one directory per hostname.
  for root in /var/lib/caddy/.local/share/caddy "$HOME/.local/share/caddy" /root/.local/share/caddy; do
    found=$(sudo_if_needed sh -c "ls -d '$root'/certificates/*/'$host' 2>/dev/null | head -n 1" || true)
    if [ -n "$found" ]; then cert_dir=$found; cert_name=$host; break; fi
  done
fi
[ -n "$cert_dir" ] || fail "No certificate found for $host. Pass --cert-dir DIR --cert-name NAME (DIR/NAME.crt and DIR/NAME.key)."
cert_name=${cert_name:-rtmps}
sudo_if_needed test -f "$cert_dir/$cert_name.crt" || fail "Missing $cert_dir/$cert_name.crt"
sudo_if_needed test -f "$cert_dir/$cert_name.key" || fail "Missing $cert_dir/$cert_name.key"
env_set PROGRAM_RTMPS_CERT_DIR "$cert_dir"
env_set PROGRAM_RTMPS_CERT_NAME "$cert_name"
echo "Using $cert_dir/$cert_name.crt (MediaMTX reloads it when Caddy renews)"

step "Include $OVERRIDE in every docker compose command and deploy"
files=$(env_get COMPOSE_FILE)
case ":${files}:" in
  *":$OVERRIDE:"*) ;;
  *":$RELAY_COMPOSE:"*)
    # Only reachable with --ingest-only: relay mode implies RTMPS ingest.
    echo "$RELAY_COMPOSE is already included and accepts RTMPS ingest; ingest-only is already covered." ;;
  *)
    # Switching from ingest-only to relay replaces the RTMPS-only override.
    files=$(printf '%s\n' "${files:-docker-compose.yml}" | tr ':' '\n' | { grep -vxF "$OTHER" || true; } | paste -sd: -)
    env_set COMPOSE_FILE "${files:-docker-compose.yml}:$OVERRIDE" ;;
esac
echo "COMPOSE_FILE=$(env_get COMPOSE_FILE)"

step "Validate the Compose configuration"
docker compose config --quiet
echo "OK"

if [ "$assume_yes" -ne 1 ]; then
  if [ "$ingest_only" -eq 1 ]; then
    printf '\nRecreate portal and MediaMTX (a few seconds of interruption for live booths)? [y/N] '
    not_started="docker compose up -d --build mediamtx portal"
  else
    printf '\nBuild and start program-relay, then recreate portal and MediaMTX (a few seconds of\ninterruption for live booths)? [y/N] '
    not_started="docker compose up -d --build"
  fi
  read -r answer
  case "$answer" in y|Y|yes) ;; *) echo "Not started. Run: $not_started"; exit 0 ;; esac
fi

ok=1
if [ "$ingest_only" -eq 1 ]; then
  step "Start RTMPS ingest"
  # Build the portal from this checkout: an older image would not offer the RTMPS mode.
  docker compose up -d --build mediamtx portal

  step "Check"
else
  step "Start the relay"
  docker compose up -d --build program-relay
  docker compose up -d mediamtx portal

  step "Check"
  relay_answers() {
    docker compose exec -T portal python -c \
      "import urllib.request; urllib.request.urlopen('http://program-relay:8090/healthz', timeout=3)" 2>/dev/null
  }
  for _ in $(seq 1 20); do relay_answers && break; sleep 3; done
  if relay_answers; then
    echo "Portal reaches the relay service: OK"
  else
    echo "Portal cannot reach program-relay. Check: docker compose logs program-relay"; ok=0
  fi
fi
# `up -d` returns before the services are ready; wait (bounded) for both before reporting.
portal_answers() {
  docker compose exec -T portal python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" 2>/dev/null
}
rtmps_answers() {
  echo | openssl s_client -connect 127.0.0.1:1936 -servername "$host" -verify_hostname "$host" \
    -verify_return_error >/dev/null 2>&1
}
for _ in $(seq 1 20); do portal_answers && break; sleep 3; done
if portal_answers; then
  echo "Portal is healthy (it authorizes every RTMPS publish): OK"
else
  echo "Portal is not healthy, so no publish can be authorized. Check: docker compose logs portal"; ok=0
fi
for _ in $(seq 1 20); do rtmps_answers && break; sleep 3; done
if rtmps_answers; then
  echo "RTMPS listener on 1936 presents a valid certificate for $host: OK"
else
  echo "RTMPS on 1936 is not answering with a valid certificate. Check: docker compose logs mediamtx"; ok=0
fi

if [ "$ingest_only" -eq 1 ]; then
  cat <<EOF

Next:
  1. Open TCP port 1936 in the firewall / cloud security group (for ufw: sudo ufw allow 1936/tcp).
  2. Organizers: room page -> Program stream ingest -> "Add VoxBento as an extra destination".
EOF
else
  cat <<EOF

Next:
  1. Open TCP port 1936 in the firewall / cloud security group (for ufw: sudo ufw allow 1936/tcp).
  2. Organizers: room page -> Program stream ingest -> "VoxBento forwards to YouTube".
  3. Before the first real event, do the unlisted YouTube test run in docs/program-relay-rehearsal.md.
EOF
fi
[ "$ok" -eq 1 ] || exit 1
