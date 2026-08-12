#!/usr/bin/env python3
"""Deterministic source-only safety replay for retired Autopilot v2 code.

The replay never writes the live Canvas/cache/metrics roots. Historical refs are
sampled locally, passed through the real v2 hook, and summarized without
printing their content. A successful replay carries no product or rollout
authority.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
TOOL_ROOT = ROOT / "packages" / "context-canvas"
HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()
for candidate in (TOOL_ROOT, HERMES_HOME / "hermes-agent"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore  # type: ignore[import-not-found]  # noqa: E402

TOOL_RE = re.compile(r'"tool_name"\s*:\s*"([^"]+)"')
SECRET_CANARY = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
_PERSISTED_DATA_URL_RE = re.compile(
    r"data:[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+;base64,[^\s\"'<>]*",
    re.IGNORECASE,
)


def load_plugin() -> Any:
    spec = importlib.util.spec_from_file_location(
        "context_canvas_autopilot_replay",
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin: {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sampled_refs(root: Path, limit: int) -> list[tuple[str, str]]:
    groups: dict[str, list[Path]] = {}
    for path in sorted(root.glob("*/refs/tc_*.md")):
        try:
            prefix = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        match = TOOL_RE.search(prefix)
        tool = match.group(1) if match else "unknown"
        groups.setdefault(tool, []).append(path)
    if not groups or limit <= 0:
        return []
    chosen: list[tuple[str, str]] = []
    tools = sorted(groups)
    index = 0
    while len(chosen) < limit:
        made_progress = False
        for tool in tools:
            paths = groups[tool]
            if index >= len(paths):
                continue
            path = paths[index]
            try:
                chosen.append((tool, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
            made_progress = True
            if len(chosen) >= limit:
                break
        if not made_progress:
            break
        index += 1
    return chosen


def canary_violations(cache_root: Path) -> dict[str, int]:
    store = SnapshotStore(cache_root)
    secret_hits = 0
    data_url_hits = 0
    checked = 0
    for path in sorted((cache_root / "sessions").glob("*/snapshots/sr_*.json")):
        checked += 1
        validated = store.validate_manifest(path)
        envelope = validated["envelope"]
        combined = str(envelope.get("args", "")) + "\n" + str(envelope.get("result", ""))
        if SECRET_CANARY in combined:
            secret_hits += 1
        if _PERSISTED_DATA_URL_RE.search(combined) is not None:
            data_url_hits += 1
    return {"checked": checked, "secret_hits": secret_hits, "data_url_hits": data_url_hits}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root")
    parser.add_argument("--historical-root", default=str(HERMES_HOME / "context-canvas"))
    parser.add_argument("--historical-limit", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--sync-writes", action="store_true", help="Persist before returning from each hook")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_root = Path(args.output_root).expanduser() if args.output_root else Path("/tmp") / f"context-canvas-v2-replay-{timestamp}"
    canvas_root = output_root / "canvas"
    cache_root = output_root / "cache"
    metrics_root = output_root / "metrics"
    output_root.mkdir(parents=True, exist_ok=False)
    os.chmod(output_root, 0o700)
    os.environ["HERMES_CONTEXT_CANVAS_HOME"] = str(canvas_root)
    os.environ["HERMES_CONTEXT_CANVAS_TOOL"] = str(TOOL_ROOT)

    plugin = load_plugin()
    plugin.reset_state_for_tests()
    plugin.set_test_config(
        {
            "mode": "v2_active_legacy_shadow",
            "revision": "replay-r1",
            "cache_root": str(cache_root),
            "metrics_root": str(metrics_root),
            "retention_class": "synthetic-replay",
            "retention_days": 1,
            "max_semantic_refs": 12,
            "legacy_tool_threshold": 5,
            "legacy_large_result_chars": 6000,
            "legacy_max_ref_chars": 50000,
            "metrics_enabled": True,
            "require_hermes_redactor": True,
            "async_writes": not args.sync_writes,
            "queue_maxsize": 256,
            "flush_timeout_seconds": 120,
        }
    )

    events: list[dict[str, Any]] = []
    for index, (tool, content) in enumerate(sampled_refs(Path(args.historical_root).expanduser(), args.historical_limit)):
        events.append(
            {
                "tool_name": tool if tool != "unknown" else "read_file",
                "args": {"path": f"historical-sample-{index}.txt"},
                "result": content,
                "session_id": f"historical-{index // 8:03d}",
                "tool_call_id": f"hist-{index:04d}",
                "duration_ms": 5,
                "status": "ok",
            }
        )

    encoded_image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"A" * 512).decode("ascii")
    synthetic = [
        {
            "tool_name": "read_file",
            "args": {"path": "secret-fixture.env"},
            "result": f"OPENAI_API_KEY={SECRET_CANARY}",
            "session_id": "synthetic-security",
            "tool_call_id": "secret-1",
            "duration_ms": 2,
            "status": "ok",
        },
        {
            "tool_name": "vision_analyze",
            "args": {"image_url": f"data:image/png;base64,{encoded_image}"},
            "result": {"ok": True, "description": "fixture"},
            "session_id": "synthetic-security",
            "tool_call_id": "binary-1",
            "duration_ms": 3,
            "status": "ok",
        },
        {
            "tool_name": "terminal",
            "args": {"command": "python -m pytest -q"},
            "result": {"exit_code": 0, "output": "12 passed"},
            "session_id": "synthetic-semantic",
            "tool_call_id": "verify-1",
            "duration_ms": 10,
            "status": "ok",
        },
        {
            "tool_name": "terminal",
            "args": {"command": "python fail.py"},
            "result": {"exit_code": 1, "output": "failure fixture"},
            "session_id": "synthetic-semantic",
            "tool_call_id": "failure-1",
            "duration_ms": 10,
            "status": "error",
            "error_type": "fixture_failure",
        },
        {
            "tool_name": "patch",
            "args": {"path": "fixture.py"},
            "result": {"success": True},
            "session_id": "synthetic-semantic",
            "tool_call_id": "mutation-1",
            "duration_ms": 4,
            "status": "ok",
        },
        {
            "tool_name": "mcp__context_canvas__canvas_read",
            "args": {"session_id": "fixture"},
            "result": "self capture fixture",
            "session_id": "synthetic-semantic",
            "tool_call_id": "self-1",
            "duration_ms": 1,
            "status": "ok",
        },
    ]
    events.extend(synthetic)
    for event in events:
        plugin.on_post_tool_call(**event)

    def concurrent_capture(index: int) -> None:
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"parallel-{index}.txt"},
            result="duplicate deterministic payload" if index % 2 == 0 else f"unique payload {index}",
            session_id="synthetic-concurrency",
            tool_call_id=f"parallel-{index}",
            duration_ms=2,
            status="ok",
        )

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        list(pool.map(concurrent_capture, range(max(1, args.concurrency))))

    session_ids = {str(event["session_id"]) for event in events}
    session_ids.add("synthetic-concurrency")
    for session_id in session_ids:
        plugin.on_session_finalize(session_id=session_id)

    metrics = PrivateJsonlLedger(metrics_root).read()
    canaries = canary_violations(cache_root)
    failures = [row for row in metrics if row.get("active_capture_attempted") and not row.get("active_capture_ok")]
    self_rows = [row for row in metrics if row.get("self_capture_excluded")]
    result = {
        "ok": not failures and canaries["secret_hits"] == 0 and canaries["data_url_hits"] == 0,
        "product_authority": "none",
        "purpose": "historical_safety_evidence_only",
        "output_root": str(output_root),
        "historical_events": len(events) - len(synthetic),
        "synthetic_events": len(synthetic),
        "concurrent_events": max(1, args.concurrency),
        "metric_rows": len(metrics),
        "capture_failures": len(failures),
        "self_capture_excluded_rows": len(self_rows),
        "canary_check": canaries,
        "semantic_canvases": len(list(canvas_root.glob("auto-v2-*/canvas.json"))),
        "snapshot_manifests": len(list((cache_root / "sessions").glob("*/snapshots/sr_*.json"))),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
