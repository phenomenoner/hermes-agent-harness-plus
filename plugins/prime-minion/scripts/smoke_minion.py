#!/usr/bin/env python3
"""Run one real Luna/max Prime minion through Hermes-managed Codex."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "prime_minion_smoke_pkg"


def load_tools():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    for name in ("schemas", "tools"):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PACKAGE}.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.tools"]


async def main() -> None:
    tools = load_tools()
    workdir = ROOT / ".runtime" / "smoke-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    proof = workdir / "minion-proof.txt"
    if proof.exists():
        proof.unlink()
    task = (
        "Use the ipython tool to create a file named minion-proof.txt in the current working "
        "directory containing exactly PRIME_MINION_OK followed by one newline. Then reply exactly "
        "PRIME_MINION_DONE and nothing else."
    )
    raw = await tools.delegate_minion(
        {
            "task": task,
            "workdir": str(workdir),
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "timeout_seconds": 900,
        }
    )
    payload = json.loads(raw)
    if payload.get("status") != "completed":
        raise SystemExit(json.dumps(payload, ensure_ascii=False))
    if not proof.is_file() or proof.read_text(encoding="utf-8") != "PRIME_MINION_OK\n":
        raise SystemExit("minion proof file was not created with the exact expected content")
    if payload.get("result") != "PRIME_MINION_DONE":
        raise SystemExit(f"unexpected final result: {payload.get('result')!r}")
    print(
        json.dumps(
            {
                "status": "pass",
                "route": payload["route"],
                "result": payload["result"],
                "proof_file": str(proof),
                "proof_content": "PRIME_MINION_OK\\n",
                "tool_calls": payload.get("tool_calls"),
                "usage": payload.get("usage"),
                "event_count": payload.get("event_count"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
