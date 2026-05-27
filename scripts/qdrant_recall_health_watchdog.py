#!/usr/bin/env python3
"""Silent watchdog for Hermes Agent Qdrant multilingual recall collections.

Cron mode: print nothing when healthy; print a compact alert when a required
collection is missing/unhealthy so Hermes no-agent cron delivers only problems.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
REQUIRED = {
    "hermes_skills_multilingual_v1": {"size": 384, "distance": "Cosine", "min_points": 1},
    "hermes_sessions_recent_multilingual_v1": {"size": 384, "distance": "Cosine", "min_points": 1},
}


def fetch_json(path: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(f"{QDRANT_URL}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def vector_cfg(config: dict) -> tuple[int | None, str | None]:
    vectors = (((config or {}).get("params") or {}).get("vectors") or {})
    # Qdrant can expose either a single vector config or named vectors.
    if "size" in vectors:
        return vectors.get("size"), vectors.get("distance")
    if isinstance(vectors, dict) and vectors:
        first = next(iter(vectors.values()))
        if isinstance(first, dict):
            return first.get("size"), first.get("distance")
    return None, None


def main() -> int:
    verbose = os.environ.get("QDRANT_WATCHDOG_VERBOSE") == "1"
    problems: list[str] = []
    found: dict[str, dict] = {}
    try:
        collections = fetch_json("/collections")
        names = [c.get("name") for c in collections.get("result", {}).get("collections", [])]
    except Exception as exc:  # noqa: BLE001 — watchdog must report compactly
        print(
            "⚠️ Qdrant multilingual recall watchdog: cannot reach Qdrant.\n"
            f"url={QDRANT_URL}\n"
            f"error={type(exc).__name__}: {exc}\n"
            "Next check: verify Qdrant service/container and Hermes MCP/server env before assuming crontab state."
        )
        return 2

    for name, expected in REQUIRED.items():
        if name not in names:
            problems.append(f"MISSING {name}")
            continue
        try:
            info = fetch_json(f"/collections/{name}").get("result", {})
        except Exception as exc:  # noqa: BLE001
            problems.append(f"INFO_ERROR {name}: {type(exc).__name__}: {exc}")
            continue
        status = info.get("status")
        points = info.get("points_count")
        size, distance = vector_cfg(info.get("config") or {})
        found[name] = {"status": status, "points_count": points, "size": size, "distance": distance}
        if status != "green":
            problems.append(f"BAD_STATUS {name}: {status}")
        if isinstance(points, int) and points < expected["min_points"]:
            problems.append(f"LOW_POINTS {name}: {points}")
        if size != expected["size"]:
            problems.append(f"BAD_VECTOR_SIZE {name}: {size}")
        if distance != expected["distance"]:
            problems.append(f"BAD_DISTANCE {name}: {distance}")

    if problems:
        print("⚠️ Qdrant multilingual recall watchdog alert")
        print(f"time_utc={datetime.now(timezone.utc).isoformat()}")
        print(f"url={QDRANT_URL}")
        print("problems:")
        for p in problems:
            print(f"- {p}")
        print("observed:")
        for name in REQUIRED:
            print(f"- {name}: {found.get(name, 'not_found')}")
        print(
            "next_steps: check the Qdrant refresh/indexing script configuration, "
            "collection names, and embedding model before mixing legacy and multilingual recall collections."
        )
        return 1

    if verbose:
        print("Qdrant multilingual recall watchdog OK")
        print(f"url={QDRANT_URL}")
        for name in REQUIRED:
            item = found.get(name, {})
            print(f"- {name}: status={item.get('status')} points={item.get('points_count')} vector={item.get('size')}/{item.get('distance')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
