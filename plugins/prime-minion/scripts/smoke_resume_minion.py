#!/usr/bin/env python3
"""Run a real create, process-exit, resume, and close minion lifecycle."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "prime_minion_resume_smoke_pkg"
RESUME_CODE = "RESUME-CODE-7F4A9C"


def load_modules():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    for name in ("schemas", "sessions", "tools"):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PACKAGE}.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.sessions"], sys.modules[f"{PACKAGE}.tools"]


async def main() -> None:
    state_root = ROOT / ".runtime" / "resume-smoke-state"
    workdir = ROOT / ".runtime" / "resume-smoke-workdir"
    for path in (state_root, workdir):
        if path.exists():
            shutil.rmtree(path)
    workdir.mkdir(parents=True)
    os.environ["PRIME_MINION_STATE_DIR"] = str(state_root)
    sessions, tools = load_modules()

    first_task = (
        f"Remember the exact code {RESUME_CODE} for a future turn. Use the ipython tool to create "
        "phase-one.txt in the current working directory containing exactly PHASE_ONE_OK followed by "
        "one newline. Then reply exactly PHASE_ONE_DONE and nothing else."
    )
    first = json.loads(
        await tools.delegate_minion(
            {
                "task": first_task,
                "workdir": str(workdir),
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "session_mode": "resumable",
                "timeout_seconds": 900,
            }
        )
    )
    if first.get("status") != "completed":
        raise SystemExit(json.dumps(first, ensure_ascii=False))
    proof = workdir / "phase-one.txt"
    if not proof.is_file() or proof.read_text(encoding="utf-8") != "PHASE_ONE_OK\n":
        raise SystemExit("phase-one proof file was not created with exact content")
    if first.get("result") != "PHASE_ONE_DONE":
        raise SystemExit(f"unexpected first result: {first.get('result')!r}")
    session_id = first.get("session_id")
    if not isinstance(session_id, str):
        raise SystemExit("first turn did not return session_id")

    second = json.loads(
        await tools.delegate_minion(
            {
                "task": (
                    "This is a resumed session after the previous Prime process exited. Without "
                    "using tools or reading files, reply with only the exact resume code from my "
                    "previous message."
                ),
                "workdir": str(workdir),
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "session_id": session_id,
                "timeout_seconds": 900,
            }
        )
    )
    if second.get("status") != "completed":
        raise SystemExit(json.dumps(second, ensure_ascii=False))
    if second.get("result") != RESUME_CODE:
        raise SystemExit(f"resume context mismatch: {second.get('result')!r}")
    if second.get("generation") != 2:
        raise SystemExit(f"unexpected resumed generation: {second.get('generation')!r}")

    status = json.loads(await tools.minion_session_status({"session_id": session_id}))
    if status.get("status") != "ok" or status["session"].get("state") != "IDLE":
        raise SystemExit(f"unexpected idle status: {status}")
    manifest = sessions.load_manifest(session_id)
    if [turn.get("status") for turn in manifest["turns"]] != ["COMPLETED", "COMPLETED"]:
        raise SystemExit("durable turn history did not record two completed generations")

    closed = json.loads(await tools.close_minion_session({"session_id": session_id}))
    if closed.get("status") != "closed" or closed["session"].get("state") != "CLOSED":
        raise SystemExit(f"unexpected close result: {closed}")
    print(
        json.dumps(
            {
                "status": "pass",
                "route": second["effective_route"],
                "same_session_id": first["session_id"] == second["session_id"],
                "generations": [first["generation"], second["generation"]],
                "first_result": first["result"],
                "resumed_result": second["result"],
                "workspace_proof": "PHASE_ONE_OK\\n",
                "turn_statuses": [turn["status"] for turn in manifest["turns"]],
                "final_state": closed["session"]["state"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
