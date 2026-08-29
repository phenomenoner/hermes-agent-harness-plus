#!/usr/bin/env python3
"""Credential-free two-process resume through the 0.3 worker lifecycle."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from probe_rpc import RUNTIME, load_tools

ROUTE = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}


async def run_state(
    tools: Any,
    *,
    workdir: Path,
    session_dir: Path,
    resume_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result, _diagnostic, evidence = await tools._run_invocation(
        task="credential-free resume route/readback probe; do not prompt",
        workdir=workdir,
        runtime=RUNTIME,
        route=ROUTE,
        timeout_seconds=30,
        session_mode="resumable",
        session_directory=session_dir,
        resume_path=resume_path,
        preflight=True,
        operation="route_probe",
    )
    expected = {
        "status": "completed",
        "operation": "route_probe",
        "effective_route": ROUTE,
        "prompt_accepted": False,
        "provider_request_sent": False,
        "cleanup_verified": True,
        "mount_absent": True,
        "children_reaped": True,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise SystemExit(f"resume probe mismatch for {key}: expected {value!r}, observed {result.get(key)!r}")
    return result, evidence


async def run() -> dict[str, Any]:
    tools = load_tools()
    with tempfile.TemporaryDirectory(prefix="prime-minion-resume-probe-") as temp:
        root = Path(temp)
        workdir = root / "workdir"
        session_dir = root / "sessions"
        workdir.mkdir(mode=0o700)
        session_dir.mkdir(mode=0o700)
        first, first_evidence = await run_state(
            tools,
            workdir=workdir,
            session_dir=session_dir,
            resume_path=None,
        )
        session_file = first.get("session_file")
        session_id = first.get("prime_session_id")
        if not isinstance(session_file, str) or not Path(session_file).is_file():
            raise SystemExit(
                "first worker did not persist a Prime session file; "
                f"result_keys={sorted(first)} session_file={session_file!r} "
                f"has_prime_session_id={isinstance(session_id, str) and bool(session_id)}"
            )
        if not isinstance(session_id, str) or not session_id:
            raise SystemExit("first worker did not return a Prime session identity")
        second, second_evidence = await run_state(
            tools,
            workdir=workdir,
            session_dir=session_dir,
            resume_path=Path(session_file),
        )
        if second.get("prime_session_id") != session_id:
            raise SystemExit("resumed Prime session identity changed")
        if Path(str(second.get("session_file"))).resolve() != Path(session_file).resolve():
            raise SystemExit("resumed Prime session file changed")
        return {
            "status": "pass",
            "provider_requests_sent": 0,
            "processes": 2,
            "same_prime_session_id": True,
            "same_session_file": True,
            "route": ROUTE,
            "cleanup_clean": [first["cleanup_verified"], second["cleanup_verified"]],
            "worker_pid_namespace_pid": [
                first["worker_pid_namespace_pid"],
                second["worker_pid_namespace_pid"],
            ],
            "worker_mount_namespace": [
                first_evidence["worker_mount_namespace"],
                second_evidence["worker_mount_namespace"],
            ],
        }


def main() -> None:
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
