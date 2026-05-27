#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${QDRANT_SESSIONS_SCRIPT:-$SCRIPT_DIR/qdrant_ingest_hermes_sessions.py}"
COLLECTION="${QDRANT_SESSIONS_COLLECTION:-hermes_sessions_recent_multilingual_v1}"
MODEL="${QDRANT_EMBED_MODEL:-sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2}"
DAYS="${QDRANT_SESSIONS_DAYS:-14}"
MAX_SESSIONS="${QDRANT_SESSIONS_MAX:-200}"
LOG_DIR="${HERMES_LOG_DIR:-${HERMES_HOME:-$HOME/.hermes}/logs}"
LOG_FILE="$LOG_DIR/qdrant_sessions_reindex.log"
VERBOSE="${QDRANT_REFRESH_VERBOSE:-0}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-qdrant-hermes}"
EXPECTED_COLLECTIONS="${QDRANT_EXPECTED_COLLECTIONS:-hermes_skills_multilingual_v1,hermes_sessions_recent_multilingual_v1}"
EXPECTED_VECTOR_SIZE="${QDRANT_EXPECTED_VECTOR_SIZE:-384}"
EXPECTED_DISTANCE="${QDRANT_EXPECTED_DISTANCE:-Cosine}"

mkdir -p "$LOG_DIR"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

qdrant_ready() {
  curl -fsS --max-time 2 "$QDRANT_URL/collections" >/dev/null 2>&1
}

ensure_qdrant() {
  if qdrant_ready; then
    return 0
  fi

  if command -v docker >/dev/null 2>&1 && docker inspect "$QDRANT_CONTAINER" >/dev/null 2>&1; then
    printf '[%s] qdrant not reachable at %s; starting docker container %s\n' "$(date -Is)" "$QDRANT_URL" "$QDRANT_CONTAINER" >> "$LOG_FILE"
    docker start "$QDRANT_CONTAINER" >> "$LOG_FILE" 2>&1 || true
    for _ in {1..45}; do
      if qdrant_ready; then
        return 0
      fi
      sleep 1
    done
  fi

  printf 'ERROR: Qdrant is not reachable at %s and auto-start failed for container %s\n' "$QDRANT_URL" "$QDRANT_CONTAINER"
  return 1
}

verify_expected_collections() {
  python3 - "$QDRANT_URL" "$EXPECTED_COLLECTIONS" "$EXPECTED_VECTOR_SIZE" "$EXPECTED_DISTANCE" <<'PY'
import json
import sys
import urllib.error
import urllib.request

base_url, raw_names, expected_size, expected_distance = sys.argv[1:5]
expected_size = int(expected_size)
names = [name.strip() for name in raw_names.split(",") if name.strip()]
failures = []

for name in names:
    url = f"{base_url.rstrip('/')}/collections/{name}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        failures.append(f"{name}: HTTP {exc.code}")
        continue
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        continue

    result = payload.get("result", {})
    status = result.get("status")
    points = int(result.get("points_count") or 0)
    vectors = result.get("config", {}).get("params", {}).get("vectors", {})
    size = vectors.get("size")
    distance = vectors.get("distance")
    if status != "green":
        failures.append(f"{name}: status={status!r}")
    if points <= 0:
        failures.append(f"{name}: points_count={points}")
    if size != expected_size:
        failures.append(f"{name}: vector_size={size}, expected={expected_size}")
    if distance != expected_distance:
        failures.append(f"{name}: distance={distance!r}, expected={expected_distance!r}")
    print(f"verified collection {name}: status={status} points={points} vectors={size}/{distance}")

if failures:
    print("ERROR: Qdrant expected collection verification failed:")
    for failure in failures:
        print(f"- {failure}")
    sys.exit(1)
PY
}

if ensure_qdrant && "$SCRIPT" --url "$QDRANT_URL" --collection "$COLLECTION" --model "$MODEL" --days "$DAYS" --max-sessions "$MAX_SESSIONS" --recreate >"$TMP_OUT" 2>&1 && verify_expected_collections >>"$TMP_OUT" 2>&1; then
  {
    printf '\n[%s] refresh ok collection=%s model=%s days=%s max_sessions=%s\n' "$(date -Is)" "$COLLECTION" "$MODEL" "$DAYS" "$MAX_SESSIONS"
    tail -n 80 "$TMP_OUT"
  } >> "$LOG_FILE"
  if [[ "$VERBOSE" == "1" ]]; then
    cat "$TMP_OUT"
  fi
else
  status=$?
  {
    printf '\n[%s] refresh FAILED status=%s collection=%s model=%s days=%s max_sessions=%s\n' "$(date -Is)" "$status" "$COLLECTION" "$MODEL" "$DAYS" "$MAX_SESSIONS"
    cat "$TMP_OUT"
  } >> "$LOG_FILE"
  cat "$TMP_OUT"
  exit "$status"
fi
