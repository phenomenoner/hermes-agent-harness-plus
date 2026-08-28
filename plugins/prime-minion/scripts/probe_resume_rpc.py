#!/usr/bin/env python3
"""Credential-free create, process-exit, and resume probe for Prime RPC."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from probe_rpc import BASE_URL, EXTENSION, RUNTIME, clean_environment

ROOT = Path(__file__).resolve().parents[1]
PROBE_ROOT = ROOT / ".runtime" / "resume-probe"
SESSION_DIR = PROBE_ROOT / "sessions"


def run_state(*, resume: Path | None) -> dict[str, Any]:
    command = [
        str(RUNTIME / "prime-agent.sh"),
        "--mode",
        "rpc",
        "--session-dir",
        str(SESSION_DIR),
        "--no-extensions",
        "--extension",
        str(EXTENSION),
        "--provider",
        "openai-codex",
        "--model",
        "gpt-5.6-luna",
        "--thinking",
        "max",
        "--cwd",
        str(PROBE_ROOT),
    ]
    if resume is not None:
        command.extend(["--resume", str(resume)])
    completed = subprocess.run(
        command,
        input='{"id":"state","type":"get_state"}\n',
        text=True,
        capture_output=True,
        env=clean_environment(),
        cwd=RUNTIME,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"Prime resume probe exited {completed.returncode}: {completed.stderr[-2000:]}")
    states = []
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"non-JSON RPC stdout: {line[:200]}") from exc
        if value.get("type") == "response" and value.get("command") == "get_state":
            states.append(value)
    if len(states) != 1 or states[0].get("success") is not True:
        raise SystemExit(f"missing successful get_state response: {completed.stdout[-2000:]}")
    return states[0]["data"]


def main() -> None:
    if PROBE_ROOT.exists():
        shutil.rmtree(PROBE_ROOT)
    SESSION_DIR.mkdir(parents=True, mode=0o700)
    first = run_state(resume=None)
    session_file = first.get("sessionFile")
    session_id = first.get("sessionId")
    if not isinstance(session_file, str) or not Path(session_file).is_file():
        raise SystemExit(f"first process did not persist a session file: {session_file!r}")
    if not isinstance(session_id, str) or not session_id:
        raise SystemExit("first process did not return a session id")

    second = run_state(resume=Path(session_file))
    if second.get("sessionId") != session_id:
        raise SystemExit(
            f"resume identity mismatch: first={session_id!r}, second={second.get('sessionId')!r}"
        )
    if Path(str(second.get("sessionFile"))).resolve() != Path(session_file).resolve():
        raise SystemExit("resume file mismatch")
    model = second.get("model") if isinstance(second.get("model"), dict) else {}
    if model.get("provider") != "openai-codex" or model.get("id") != "gpt-5.6-luna":
        raise SystemExit(f"resumed route mismatch: {model}")
    if second.get("thinkingLevel") != "max":
        raise SystemExit(f"resumed effort mismatch: {second.get('thinkingLevel')!r}")
    print(
        json.dumps(
            {
                "status": "pass",
                "processes": 2,
                "same_prime_session_id": True,
                "same_session_file": True,
                "route": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                },
                "base_url": BASE_URL,
            }
        )
    )


if __name__ == "__main__":
    main()
