#!/usr/bin/env bash
set -euo pipefail

# Quiet restart-aware calibration for a local Qdrant recall sidecar.
#
# Healthy path: print nothing.
# Restart path: compare Docker .State.StartedAt with the last saved marker, then
# run the Qdrant recall health watchdog. If the watchdog passes, update the
# marker and stay quiet. If it fails, print a compact alert. Optionally set
# QDRANT_REPAIR_CMD to run a local repair command after a failed calibration.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-qdrant-hermes}"
STATE_DIR="${QDRANT_STATE_DIR:-${HERMES_HOME:-$HOME/.hermes}/qdrant/state}"
LOG_DIR="${HERMES_LOG_DIR:-${HERMES_HOME:-$HOME/.hermes}/logs}"
LOG_FILE="$LOG_DIR/qdrant_restart_calibration.log"
HEALTHCHECK_CMD="${QDRANT_HEALTHCHECK_CMD:-python3 "$SCRIPT_DIR/qdrant_recall_health_watchdog.py"}"
REPAIR_CMD="${QDRANT_REPAIR_CMD:-}"
MARKER="$STATE_DIR/${QDRANT_CONTAINER}.started_at"
VERBOSE="${QDRANT_RESTART_CALIBRATION_VERBOSE:-0}"

mkdir -p "$STATE_DIR" "$LOG_DIR"

container_started_at() {
  docker inspect -f '{{.State.StartedAt}}' "$QDRANT_CONTAINER" 2>/dev/null || true
}

run_healthcheck() {
  # shellcheck disable=SC2086
  bash -c "$HEALTHCHECK_CMD"
}

CURRENT_STARTED_AT="$(container_started_at)"
LAST_STARTED_AT=""
if [[ -f "$MARKER" ]]; then
  LAST_STARTED_AT="$(cat "$MARKER" 2>/dev/null || true)"
fi

# Docker may not be installed or the user may run Qdrant another way. In that
# case, fall back to the normal healthcheck without pretending restart detection
# is available.
if [[ -z "$CURRENT_STARTED_AT" ]]; then
  if run_healthcheck >/tmp/qdrant_restart_calibration.out 2>&1; then
    [[ "$VERBOSE" == "1" ]] && cat /tmp/qdrant_restart_calibration.out
    exit 0
  fi
  echo "⚠️ Qdrant recall calibration failed"
  echo "container=$QDRANT_CONTAINER"
  echo "started_at=unavailable"
  cat /tmp/qdrant_restart_calibration.out
  exit 1
fi

RESTART_DETECTED=0
if [[ -n "$LAST_STARTED_AT" && "$CURRENT_STARTED_AT" != "$LAST_STARTED_AT" ]]; then
  RESTART_DETECTED=1
fi

if run_healthcheck >/tmp/qdrant_restart_calibration.out 2>&1; then
  if [[ "$CURRENT_STARTED_AT" != "$LAST_STARTED_AT" ]]; then
    printf '%s\n' "$CURRENT_STARTED_AT" > "$MARKER"
    printf '[%s] qdrant restart calibration ok container=%s started_at=%s\n' \
      "$(date -Is)" "$QDRANT_CONTAINER" "$CURRENT_STARTED_AT" >> "$LOG_FILE"
  fi
  if [[ "$VERBOSE" == "1" ]]; then
    if [[ "$RESTART_DETECTED" == "1" ]]; then
      echo "Qdrant container restart observed; calibration OK"
      echo "container=$QDRANT_CONTAINER"
      echo "started_at=$CURRENT_STARTED_AT"
    fi
    cat /tmp/qdrant_restart_calibration.out
  fi
  exit 0
fi

STATUS=$?
printf '[%s] qdrant restart calibration failed container=%s current_started_at=%s last_started_at=%s status=%s\n' \
  "$(date -Is)" "$QDRANT_CONTAINER" "$CURRENT_STARTED_AT" "${LAST_STARTED_AT:-none}" "$STATUS" >> "$LOG_FILE"

if [[ -n "$REPAIR_CMD" ]]; then
  echo "⚠️ Qdrant recall calibration failed; running repair command"
  echo "container=$QDRANT_CONTAINER"
  echo "started_at=$CURRENT_STARTED_AT"
  echo "repair_cmd=$REPAIR_CMD"
  cat /tmp/qdrant_restart_calibration.out
  # shellcheck disable=SC2086
  bash -c "$REPAIR_CMD"
  exit $?
fi

echo "⚠️ Qdrant recall calibration failed"
echo "container=$QDRANT_CONTAINER"
echo "started_at=$CURRENT_STARTED_AT"
echo "last_started_at=${LAST_STARTED_AT:-none}"
echo "restart_detected=$RESTART_DETECTED"
cat /tmp/qdrant_restart_calibration.out
echo "Set QDRANT_REPAIR_CMD to chain a local repair script after failed calibration."
exit "$STATUS"
