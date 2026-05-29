#!/usr/bin/env bash
set -euo pipefail

# Quiet restart-aware calibration for a local Qdrant recall sidecar.
#
# Healthy path: run the HTTP watchdog first and print nothing when recall is OK.
# Docker metadata is optional and bounded so Docker Desktop / WSL stalls do not
# turn a healthy Qdrant endpoint into a scheduled-task timeout.
#
# Restart path: compare Docker .State.StartedAt with the last saved marker, then
# update the marker only after the Qdrant recall health watchdog passes. If the
# watchdog fails, print a compact alert. Optionally set QDRANT_REPAIR_CMD to run
# a local repair command after a failed calibration.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-qdrant-hermes}"
STATE_DIR="${QDRANT_STATE_DIR:-${HERMES_HOME:-$HOME/.hermes}/qdrant/state}"
LOG_DIR="${HERMES_LOG_DIR:-${HERMES_HOME:-$HOME/.hermes}/logs}"
LOG_FILE="$LOG_DIR/qdrant_restart_calibration.log"
HEALTHCHECK_CMD="${QDRANT_HEALTHCHECK_CMD:-python3 "$SCRIPT_DIR/qdrant_recall_health_watchdog.py"}"
REPAIR_CMD="${QDRANT_REPAIR_CMD:-}"
MARKER="$STATE_DIR/${QDRANT_CONTAINER}.started_at"
VERBOSE="${QDRANT_RESTART_CALIBRATION_VERBOSE:-0}"
DOCKER_INSPECT_TIMEOUT="${QDRANT_DOCKER_INSPECT_TIMEOUT:-5}"

mkdir -p "$STATE_DIR" "$LOG_DIR"
OUT_FILE="$(mktemp -t qdrant_restart_calibration.XXXXXX)"
trap 'rm -f "$OUT_FILE"' EXIT

container_started_at() {
  # Use Python's subprocess timeout instead of a shell-level `timeout` binary so
  # the helper works on more systems and never blocks forever on Docker CLI.
  python3 - "$QDRANT_CONTAINER" "$DOCKER_INSPECT_TIMEOUT" <<'PY' 2>/dev/null || true
import subprocess
import sys

container = sys.argv[1]
try:
    timeout = max(float(sys.argv[2]), 0.1)
except ValueError:
    timeout = 5.0

try:
    out = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
except Exception:
    sys.exit(0)

print(out.strip())
PY
}

run_healthcheck() {
  # shellcheck disable=SC2086
  bash -c "$HEALTHCHECK_CMD"
}

LAST_STARTED_AT=""
if [[ -f "$MARKER" ]]; then
  LAST_STARTED_AT="$(cat "$MARKER" 2>/dev/null || true)"
fi

# Check Qdrant over HTTP before touching Docker. This keeps the common healthy
# path fast and prevents Docker CLI stalls from causing false cron failures.
if run_healthcheck >"$OUT_FILE" 2>&1; then
  CURRENT_STARTED_AT="$(container_started_at)"
  RESTART_DETECTED=0

  if [[ -n "$CURRENT_STARTED_AT" && -n "$LAST_STARTED_AT" && "$CURRENT_STARTED_AT" != "$LAST_STARTED_AT" ]]; then
    RESTART_DETECTED=1
  fi

  if [[ -n "$CURRENT_STARTED_AT" && "$CURRENT_STARTED_AT" != "$LAST_STARTED_AT" ]]; then
    printf '%s\n' "$CURRENT_STARTED_AT" > "$MARKER"
    printf '[%s] qdrant restart calibration ok container=%s started_at=%s\n' \
      "$(date -Is)" "$QDRANT_CONTAINER" "$CURRENT_STARTED_AT" >> "$LOG_FILE"
  fi

  if [[ "$VERBOSE" == "1" ]]; then
    if [[ -n "$CURRENT_STARTED_AT" ]]; then
      if [[ "$RESTART_DETECTED" == "1" ]]; then
        echo "Qdrant container restart observed; calibration OK"
      else
        echo "Qdrant calibration OK"
      fi
      echo "container=$QDRANT_CONTAINER"
      echo "started_at=$CURRENT_STARTED_AT"
    else
      echo "Qdrant calibration OK"
      echo "container=$QDRANT_CONTAINER"
      echo "started_at=unavailable"
      echo "docker_inspect_timeout_seconds=$DOCKER_INSPECT_TIMEOUT"
    fi
    cat "$OUT_FILE"
  fi
  exit 0
fi

STATUS=$?
CURRENT_STARTED_AT="$(container_started_at)"
RESTART_DETECTED=0
if [[ -n "$CURRENT_STARTED_AT" && -n "$LAST_STARTED_AT" && "$CURRENT_STARTED_AT" != "$LAST_STARTED_AT" ]]; then
  RESTART_DETECTED=1
fi

printf '[%s] qdrant restart calibration failed container=%s current_started_at=%s last_started_at=%s status=%s\n' \
  "$(date -Is)" "$QDRANT_CONTAINER" "${CURRENT_STARTED_AT:-unavailable}" "${LAST_STARTED_AT:-none}" "$STATUS" >> "$LOG_FILE"

if [[ -n "$REPAIR_CMD" ]]; then
  echo "⚠️ Qdrant recall calibration failed; running repair command"
  echo "container=$QDRANT_CONTAINER"
  echo "started_at=${CURRENT_STARTED_AT:-unavailable}"
  echo "restart_detected=$RESTART_DETECTED"
  echo "repair_cmd=$REPAIR_CMD"
  cat "$OUT_FILE"
  # shellcheck disable=SC2086
  bash -c "$REPAIR_CMD"
  exit $?
fi

echo "⚠️ Qdrant recall calibration failed"
echo "container=$QDRANT_CONTAINER"
echo "started_at=${CURRENT_STARTED_AT:-unavailable}"
echo "last_started_at=${LAST_STARTED_AT:-none}"
echo "restart_detected=$RESTART_DETECTED"
cat "$OUT_FILE"
echo "Set QDRANT_REPAIR_CMD to chain a local repair script after failed calibration."
exit "$STATUS"
