#!/usr/bin/env bash
set -euo pipefail

# Bounded Docker/Qdrant recovery for local recall sidecars.
#
# Scheduler contract:
# - stay silent when Qdrant is already healthy or recovery succeeds;
# - write operational details to a local log;
# - print a compact diagnostic and exit non-zero only when bounded recovery fails.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-qdrant-hermes}"
QDRANT_HEALTH_WATCHDOG="${QDRANT_HEALTH_WATCHDOG:-$SCRIPT_DIR/qdrant_recall_health_watchdog.py}"
DOCKER_BIN="${QDRANT_DOCKER_BIN:-docker}"
WINDOWS_DOCKER_CLI="${QDRANT_WINDOWS_DOCKER_CLI:-/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe}"
WINDOWS_DOCKER_DESKTOP_EXE="${QDRANT_WINDOWS_DOCKER_DESKTOP_EXE:-/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe}"
STATE_DIR="${QDRANT_STATE_DIR:-${HERMES_HOME:-$HOME/.hermes}/qdrant/state}"
LOG_DIR="${QDRANT_LOG_DIR:-${HERMES_LOG_DIR:-${HERMES_HOME:-$HOME/.hermes}/logs}}"
LOG_FILE="${QDRANT_START_LOG:-$LOG_DIR/qdrant_bounded_start_restart.log}"
DOCKER_CLI_TIMEOUT="${QDRANT_DOCKER_CLI_TIMEOUT:-8}"
DOCKER_START_WAIT_ATTEMPTS="${QDRANT_DOCKER_START_WAIT_ATTEMPTS:-90}"
QDRANT_READY_WAIT_ATTEMPTS="${QDRANT_READY_WAIT_ATTEMPTS:-90}"
QDRANT_RESTART_WAIT_ATTEMPTS="${QDRANT_RESTART_WAIT_ATTEMPTS:-60}"
VERBOSE="${QDRANT_START_VERBOSE:-0}"
HEALTH_OUT="$(mktemp -t qdrant_bounded_start_health.XXXXXX)"
RESTART_MARKER="$STATE_DIR/${QDRANT_CONTAINER}.started_at"

mkdir -p "$LOG_DIR" "$STATE_DIR"
trap 'rm -f "$HEALTH_OUT"' EXIT

ts() { date -Is; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }
verbose() { [[ "$VERBOSE" == "1" ]] && printf '%s\n' "$*" || true; }

docker_call() {
  python3 - "$DOCKER_CLI_TIMEOUT" "$@" <<'PY'
import subprocess
import sys

try:
    timeout = max(float(sys.argv[1]), 0.1)
except ValueError:
    timeout = 8.0
cmd = sys.argv[2:]
try:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
except Exception as exc:  # noqa: BLE001 - shell helper needs compact failure
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(124)

sys.stdout.write(proc.stdout)
sys.exit(proc.returncode)
PY
}

rest_ready() {
  curl -fsS --max-time 2 "$QDRANT_URL/collections" >/dev/null 2>&1
}

health_ready() {
  : > "$HEALTH_OUT"
  if [[ -f "$QDRANT_HEALTH_WATCHDOG" ]]; then
    QDRANT_URL="$QDRANT_URL" python3 "$QDRANT_HEALTH_WATCHDOG" >"$HEALTH_OUT" 2>&1
  else
    rest_ready >"$HEALTH_OUT" 2>&1
  fi
}

docker_cmd_available() {
  local bin="$1"
  docker_call "$bin" version >/dev/null 2>&1
}

select_docker_bin() {
  if docker_cmd_available "$DOCKER_BIN"; then
    return 0
  fi
  if [[ -x "$WINDOWS_DOCKER_CLI" ]] && docker_cmd_available "$WINDOWS_DOCKER_CLI"; then
    DOCKER_BIN="$WINDOWS_DOCKER_CLI"
    return 0
  fi
  return 1
}

start_docker_desktop() {
  [[ -f "$WINDOWS_DOCKER_DESKTOP_EXE" ]] || return 1
  command -v powershell.exe >/dev/null 2>&1 || return 1
  local win_exe
  win_exe="$(wslpath -w "$WINDOWS_DOCKER_DESKTOP_EXE" 2>/dev/null || printf '%s' "$WINDOWS_DOCKER_DESKTOP_EXE")"
  WIN_DOCKER_DESKTOP_EXE="$win_exe" powershell.exe -NoProfile -NonInteractive -Command \
    'Start-Process -FilePath $env:WIN_DOCKER_DESKTOP_EXE -WindowStyle Minimized' >/dev/null 2>&1
}

wait_for_docker() {
  local i
  for i in $(seq 1 "$DOCKER_START_WAIT_ATTEMPTS"); do
    if select_docker_bin; then
      log "docker ready docker_bin=$DOCKER_BIN attempts=$i"
      return 0
    fi
    sleep 1
  done
  return 1
}

container_exists() {
  docker_call "$DOCKER_BIN" inspect "$QDRANT_CONTAINER" >/dev/null 2>&1
}

container_started_at() {
  docker_call "$DOCKER_BIN" inspect -f '{{.State.StartedAt}}' "$QDRANT_CONTAINER" 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*Z$' || true
}

wait_for_health() {
  local attempts="$1"
  local i
  for i in $(seq 1 "$attempts"); do
    if health_ready; then
      local started_at
      started_at="$(container_started_at || true)"
      if [[ -n "$started_at" ]]; then
        printf '%s\n' "$started_at" > "$RESTART_MARKER"
      fi
      log "qdrant healthy container=$QDRANT_CONTAINER docker_bin=$DOCKER_BIN attempts=$i started_at=${started_at:-unknown}"
      return 0
    fi
    sleep 1
  done
  return 1
}

emit_failure() {
  local reason="$1"
  printf '⚠️ Qdrant bounded start/restart failed\n'
  printf 'url=%s\n' "$QDRANT_URL"
  printf 'container=%s\n' "$QDRANT_CONTAINER"
  printf 'reason=%s\n' "$reason"
  printf 'log=%s\n' "$LOG_FILE"
  if [[ -s "$HEALTH_OUT" ]]; then
    printf 'last_health_output:\n'
    cat "$HEALTH_OUT" || true
  fi
}

main() {
  if health_ready; then
    verbose "Qdrant already healthy"
    return 0
  fi

  log "health failed; bounded start/restart begin url=$QDRANT_URL container=$QDRANT_CONTAINER"
  if ! select_docker_bin; then
    log "docker unavailable; attempting Docker Desktop start exe=$WINDOWS_DOCKER_DESKTOP_EXE"
    start_docker_desktop || true
    if ! wait_for_docker; then
      log "docker still unavailable after bounded wait"
      emit_failure "docker_unavailable_after_docker_desktop_start_attempt"
      return 1
    fi
  fi

  if ! container_exists; then
    log "container missing container=$QDRANT_CONTAINER docker_bin=$DOCKER_BIN"
    emit_failure "container_not_found"
    return 2
  fi

  log "docker start attempt container=$QDRANT_CONTAINER docker_bin=$DOCKER_BIN"
  docker_call "$DOCKER_BIN" start "$QDRANT_CONTAINER" >> "$LOG_FILE" 2>&1 || true
  if wait_for_health "$QDRANT_READY_WAIT_ATTEMPTS"; then
    verbose "Qdrant start attempt succeeded"
    return 0
  fi

  log "docker restart attempt container=$QDRANT_CONTAINER docker_bin=$DOCKER_BIN"
  docker_call "$DOCKER_BIN" restart "$QDRANT_CONTAINER" >> "$LOG_FILE" 2>&1 || true
  if wait_for_health "$QDRANT_RESTART_WAIT_ATTEMPTS"; then
    verbose "Qdrant restart attempt succeeded"
    return 0
  fi

  log "bounded start/restart failed"
  emit_failure "health_failed_after_start_and_one_restart"
  return 3
}

main "$@"
