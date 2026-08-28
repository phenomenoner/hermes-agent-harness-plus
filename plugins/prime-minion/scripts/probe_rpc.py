#!/usr/bin/env python3
"""Credential-free Prime RPC/extension constructibility probes."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "prime-agent"
EXTENSION = ROOT / "prime_extension.mjs"
MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
PRIME_EFFORT = {"none": "off", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
BASE_URL = "http://127.0.0.1:32123/v1"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def synthetic_key() -> str:
    return ".".join(
        (
            b64url(b'{"alg":"none","typ":"JWT"}'),
            b64url(b'{"https://api.openai.com/auth":{"chatgpt_account_id":"probe"}}'),
            "",
        )
    )


def clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if any(marker in upper for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "REFRESH_TOKEN", "SECRET")):
            env.pop(key, None)
    env.update(
        {
            "DO_NOT_TRACK": "1",
            "PRIME_AGENT_TELEMETRY": "0",
            "PRIME_AGENT_CODING_AGENT_DIR": str(ROOT / ".runtime" / "probe-home"),
            "HERMES_MINION_PROXY_BASE_URL": BASE_URL,
            "HERMES_MINION_PROXY_API_KEY": synthetic_key(),
        }
    )
    return env


def probe(model_id: str, effort: str) -> dict[str, str]:
    launcher = RUNTIME / "prime-agent.sh"
    if not launcher.is_file():
        raise SystemExit(f"missing runtime: {launcher}")
    command = [
        str(launcher),
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(EXTENSION),
        "--provider",
        "openai-codex",
        "--model",
        model_id,
        "--thinking",
        PRIME_EFFORT[effort],
        "--cwd",
        str(ROOT),
    ]
    completed = subprocess.run(
        command,
        input='{"id":"probe","type":"get_state"}\n',
        text=True,
        capture_output=True,
        env=clean_environment(),
        cwd=RUNTIME,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"Prime RPC probe exited {completed.returncode}: {completed.stderr[-2000:]}")
    responses = []
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"non-JSON RPC stdout: {line[:200]}") from exc
        if value.get("type") == "response" and value.get("command") == "get_state":
            responses.append(value)
    if len(responses) != 1 or responses[0].get("success") is not True:
        raise SystemExit(f"missing successful get_state response: {completed.stdout[-2000:]}")
    state = responses[0]["data"]
    model = state.get("model") or {}
    observed = {key: model.get(key) for key in ("provider", "id", "baseUrl")}
    expected = {"provider": "openai-codex", "id": model_id, "baseUrl": BASE_URL}
    if observed != expected or state.get("thinkingLevel") != PRIME_EFFORT[effort]:
        raise SystemExit(
            f"route mismatch: model={observed}, thinking={state.get('thinkingLevel')}"
        )
    return {
        "provider": "openai-codex",
        "model": model_id,
        "reasoning_effort": effort,
        "prime_thinking_level": PRIME_EFFORT[effort],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", action="store_true", help="probe every supported model/effort pair")
    args = parser.parse_args()
    routes = (
        [probe(model, effort) for model in MODELS for effort in EFFORTS]
        if args.matrix
        else [probe("gpt-5.6-luna", "max")]
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "route_count": len(routes),
                "models": list(MODELS) if args.matrix else ["gpt-5.6-luna"],
                "reasoning_efforts": list(EFFORTS) if args.matrix else ["max"],
                "routes": routes,
            }
        )
    )


if __name__ == "__main__":
    main()
