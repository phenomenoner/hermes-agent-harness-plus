#!/usr/bin/env python3
"""Credential-free route/readback probe through the 0.3 worker lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "prime-agent"
PACKAGE = "prime_minion_rpc_probe"
MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def load_tools():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = package
    for name in ("sessions", "invocation_worker", "tools"):
        path = ROOT / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.tools"]


async def probe(tools: Any, model: str, effort: str) -> dict[str, Any]:
    route = {"provider": "openai-codex", "model": model, "reasoning_effort": effort}
    result, _diagnostic, evidence = await tools._run_invocation(
        task="credential-free route/readback probe; do not prompt",
        workdir=ROOT,
        runtime=RUNTIME,
        route=route,
        timeout_seconds=30,
        session_mode="ephemeral",
        session_directory=None,
        resume_path=None,
        preflight=True,
        operation="route_probe",
    )
    expected = {
        "status": "completed",
        "operation": "route_probe",
        "route": route,
        "effective_route": route,
        "result": "ROUTE_PROBE_OK",
        "prompt_accepted": False,
        "provider_request_sent": False,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise SystemExit(f"route probe mismatch for {key}: expected {value!r}, observed {result.get(key)!r}")
    for key in ("cleanup_verified", "mount_absent", "children_reaped"):
        if result.get(key) is not True:
            raise SystemExit(f"route probe terminal result omitted {key}")
    stages = {record.get("stage"): record for record in evidence.get("startup_evidence", [])}
    mounted = stages.get("mounted") or {}
    if not isinstance(mounted.get("mount_id"), int):
        raise SystemExit("route probe evidence omitted exact mount ID")
    return {
        "route": route,
        "prime_session_id_absent": "prime_session_id" not in result,
        "worker_pid_namespace_pid": result.get("worker_pid_namespace_pid"),
        "worker_mount_namespace": evidence.get("worker_mount_namespace"),
        "mount_id": mounted["mount_id"],
        "cleanup_clean": result["cleanup_verified"],
    }


async def run(matrix: bool) -> list[dict[str, Any]]:
    if not (RUNTIME / ".git").is_dir():
        raise SystemExit(f"pinned Prime runtime is not installed at {RUNTIME}")
    tools = load_tools()
    routes = (
        [(model, effort) for model in MODELS for effort in EFFORTS]
        if matrix
        else [("gpt-5.6-luna", "max")]
    )
    return [await probe(tools, model, effort) for model, effort in routes]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()
    receipts = asyncio.run(run(args.matrix))
    print(
        json.dumps(
            {
                "status": "pass",
                "provider_requests_sent": 0,
                "route_count": len(receipts),
                "routes": receipts,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
