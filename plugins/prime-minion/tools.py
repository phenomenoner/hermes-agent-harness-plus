"""Hermes tools for ephemeral and resumable Prime Agent minions."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .schemas import CLOSE_MINION_SESSION, DELEGATE_MINION, MINION_SESSION_STATUS
from .sessions import (
    SessionError,
    begin_turn,
    close_manifest,
    create_manifest,
    load_manifest,
    public_status,
    record_completed,
    record_interrupted,
    resume_file,
    session_lease,
    transcript_dir,
    validate_resume_binding,
)

_PLUGIN_ROOT = Path(__file__).resolve().parent
_DEFAULT_RUNTIME = _PLUGIN_ROOT / ".runtime" / "prime-agent"
_BRIDGE_SCRIPT = _PLUGIN_ROOT / "bridge_server.py"
_EXTENSION = _PLUGIN_ROOT / "prime_extension.mjs"
_PINNED_PRIME_COMMIT = "bc0fa7606abb3b7af0f765319518d255e6ae553d"
_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"})
_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
_EFFORT_TO_PRIME = {
    "none": "off",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
_PRIME_TO_EFFORT = {value: key for key, value in _EFFORT_TO_PRIME.items()}
_SECRET_ENV_MARKERS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "BEARER_TOKEN",
    "REFRESH_TOKEN",
    "SECRET_KEY",
    "CLIENT_SECRET",
)
_EXPLICIT_SECRET_ENV = frozenset(
    {
        "ANTHROPIC_OAUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HERMES_API_KEY",
        "OPENAI_ACCESS_TOKEN",
    }
)
_MAX_DIAGNOSTIC_BYTES = 64_000


class MinionError(RuntimeError):
    pass


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _synthetic_codex_bearer() -> str:
    """Build a fresh parseable bearer; the relay authenticates then replaces it."""
    header = _b64url(b'{"alg":"none","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "hermes-minion-placeholder"
                },
                "jti": secrets.token_hex(32),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{header}.{payload}."


def _child_environment(
    proxy_base_url: str,
    config_dir: Path,
    synthetic_bearer: str,
) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper in _EXPLICIT_SECRET_ENV or any(marker in upper for marker in _SECRET_ENV_MARKERS):
            env.pop(key, None)
    env.update(
        {
            "DO_NOT_TRACK": "1",
            "PRIME_AGENT_TELEMETRY": "0",
            "PRIME_AGENT_CODING_AGENT_DIR": str(config_dir),
            "HERMES_MINION_PROXY_BASE_URL": proxy_base_url,
            "HERMES_MINION_PROXY_API_KEY": synthetic_bearer,
        }
    )
    return env


async def _bounded_stderr(stream: Optional[asyncio.StreamReader]) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        while total > _MAX_DIAGNOSTIC_BYTES and chunks:
            total -= len(chunks.pop(0))
    return b"".join(chunks).decode("utf-8", errors="replace")


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


async def _stop_process(process: Optional[asyncio.subprocess.Process], *, grace: float = 5.0) -> None:
    if process is None or process.returncode is not None:
        return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except Exception:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        _kill_process_group(process)
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def _assistant_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        return "".join(parts).strip()
    return ""


def _usage(messages: list[Any]) -> dict[str, int]:
    result = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        result["input"] += int(usage.get("input") or 0)
        result["output"] += int(usage.get("output") or 0)
        result["cache_read"] += int(usage.get("cacheRead") or 0)
        result["cache_write"] += int(usage.get("cacheWrite") or 0)
    result["total"] = sum(result[key] for key in ("input", "output", "cache_read", "cache_write"))
    return result


def _runtime_root() -> Path:
    configured = os.environ.get("PRIME_MINION_RUNTIME_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else _DEFAULT_RUNTIME


def _runtime_commit(runtime: Path) -> str:
    launcher = runtime / "prime-agent.sh"
    if not launcher.is_file():
        raise MinionError(
            f"Pinned Prime Agent runtime is missing at {runtime}. Run this plugin's bootstrap script first."
        )
    completed = subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise MinionError("Prime Agent runtime commit could not be verified.")
    if commit != _PINNED_PRIME_COMMIT:
        raise MinionError(
            f"Prime Agent runtime drift: expected {_PINNED_PRIME_COMMIT}, observed {commit}."
        )
    return commit


async def _start_bridge() -> tuple[asyncio.subprocess.Process, str, asyncio.Task[str], str]:
    synthetic_bearer = _synthetic_codex_bearer()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_BRIDGE_SCRIPT),
        "--parent-pid",
        str(os.getpid()),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stderr_task = asyncio.create_task(_bounded_stderr(process.stderr))
    assert process.stdin is not None
    process.stdin.write((synthetic_bearer + "\n").encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    assert process.stdout is not None
    try:
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=15.0)
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        raise MinionError("Hermes Codex relay did not become ready within 15 seconds.") from exc
    if not raw:
        await process.wait()
        diagnostic = await stderr_task
        raise MinionError(f"Hermes Codex relay exited before readiness: {diagnostic[-2000:]}")
    try:
        ready = json.loads(raw)
    except json.JSONDecodeError as exc:
        await _stop_process(process)
        raise MinionError("Hermes Codex relay emitted malformed readiness data.") from exc
    if ready.get("ready") is not True:
        await _stop_process(process)
        raise MinionError(str(ready.get("error") or "Hermes Codex relay is unavailable."))
    host = ready.get("host")
    port = ready.get("port")
    if host != "127.0.0.1" or not isinstance(port, int):
        await _stop_process(process)
        raise MinionError("Hermes Codex relay violated its loopback binding contract.")
    return process, f"http://127.0.0.1:{port}/v1", stderr_task, synthetic_bearer


def _effective_route(state: dict[str, Any]) -> dict[str, str]:
    model = state.get("model")
    if not isinstance(model, dict):
        raise MinionError("Prime Agent state did not expose an effective model.")
    provider = model.get("provider")
    model_id = model.get("id")
    thinking = state.get("thinkingLevel")
    effort = _PRIME_TO_EFFORT.get(str(thinking))
    if not isinstance(provider, str) or not isinstance(model_id, str) or effort is None:
        raise MinionError("Prime Agent state exposed an invalid effective route.")
    return {"provider": provider, "model": model_id, "reasoning_effort": effort}


async def _write_rpc(process: asyncio.subprocess.Process, value: dict[str, Any]) -> None:
    if process.stdin is None:
        raise MinionError("Prime Agent RPC stdin is unavailable.")
    process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
    await process.stdin.drain()


async def _read_rpc_event(process: asyncio.subprocess.Process) -> dict[str, Any]:
    if process.stdout is None:
        raise MinionError("Prime Agent RPC stdout is unavailable.")
    raw = await process.stdout.readline()
    if not raw:
        code = await process.wait()
        raise MinionError(f"Prime Agent exited with code {code} before completing the RPC command.")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MinionError("Prime Agent RPC emitted a non-JSON stdout line.") from exc
    if not isinstance(event, dict):
        raise MinionError("Prime Agent RPC emitted a non-object record.")
    return event


async def _rpc_command(
    process: asyncio.subprocess.Process,
    *,
    request_id: str,
    command: dict[str, Any],
    expected_command: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    await _write_rpc(process, {"id": request_id, **command})

    async def wait_response() -> dict[str, Any]:
        while True:
            event = await _read_rpc_event(process)
            if event.get("type") != "response" or event.get("id") != request_id:
                continue
            if event.get("command") != expected_command:
                raise MinionError("Prime Agent RPC returned a mismatched command response.")
            if event.get("success") is not True:
                raise MinionError(str(event.get("error") or f"Prime Agent rejected {expected_command}."))
            return event

    try:
        return await asyncio.wait_for(wait_response(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MinionError(f"Prime Agent did not answer {expected_command} within {timeout:g} seconds.") from exc


async def _get_state(process: asyncio.subprocess.Process, request_id: str) -> dict[str, Any]:
    response = await _rpc_command(
        process,
        request_id=request_id,
        command={"type": "get_state"},
        expected_command="get_state",
    )
    state = response.get("data")
    if not isinstance(state, dict):
        raise MinionError("Prime Agent returned malformed session state.")
    return state


async def _ensure_route(
    process: asyncio.subprocess.Process,
    requested_route: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    state = await _get_state(process, "hermes-minion-state-initial")
    current = _effective_route(state)
    if current["provider"] != requested_route["provider"] or current["model"] != requested_route["model"]:
        await _rpc_command(
            process,
            request_id="hermes-minion-set-model",
            command={
                "type": "set_model",
                "provider": requested_route["provider"],
                "modelId": requested_route["model"],
            },
            expected_command="set_model",
        )
    if current["reasoning_effort"] != requested_route["reasoning_effort"]:
        await _rpc_command(
            process,
            request_id="hermes-minion-set-thinking",
            command={
                "type": "set_thinking_level",
                "level": _EFFORT_TO_PRIME[requested_route["reasoning_effort"]],
            },
            expected_command="set_thinking_level",
        )
    state = await _get_state(process, "hermes-minion-state-effective")
    effective = _effective_route(state)
    if effective != requested_route:
        raise MinionError(
            f"Prime Agent effective route mismatch: requested {requested_route}, observed {effective}."
        )
    return state, effective


async def _abort_rpc(process: asyncio.subprocess.Process) -> None:
    try:
        await _write_rpc(process, {"id": "hermes-minion-abort", "type": "abort"})
    except Exception:
        pass


async def _run_rpc(
    *,
    runtime: Path,
    task: str,
    workdir: Path,
    requested_route: dict[str, str],
    timeout_seconds: int,
    proxy_base_url: str,
    synthetic_bearer: str,
    session_directory: Path | None = None,
    resume_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    launcher = runtime / "prime-agent.sh"
    config_dir = _PLUGIN_ROOT / ".runtime" / "minion-home"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps({"transport": "sse", "telemetry": {"enabled": False}}),
        encoding="utf-8",
    )
    env = _child_environment(proxy_base_url, config_dir, synthetic_bearer)
    command = [
        str(launcher),
        "--mode",
        "rpc",
        "--no-extensions",
        "--extension",
        str(_EXTENSION),
        "--provider",
        requested_route["provider"],
        "--model",
        requested_route["model"],
        "--thinking",
        _EFFORT_TO_PRIME[requested_route["reasoning_effort"]],
        "--cwd",
        str(workdir),
    ]
    if session_directory is None:
        command.append("--no-session")
    else:
        command.extend(["--session-dir", str(session_directory)])
        if resume_path is not None:
            command.extend(["--resume", str(resume_path)])

    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(runtime),
        start_new_session=True,
    )
    stderr_task = asyncio.create_task(_bounded_stderr(process.stderr))
    started = time.monotonic()
    diagnostic = ""
    result: dict[str, Any]
    try:
        initial_state, effective = await _ensure_route(process, requested_route)
        await _write_rpc(
            process,
            {"id": "hermes-minion-task", "type": "prompt", "message": task},
        )

        async def consume() -> dict[str, Any]:
            event_count = 0
            tool_calls: list[dict[str, Any]] = []
            prompt_accepted = False
            final_messages: list[Any] = []
            while True:
                event = await _read_rpc_event(process)
                event_count += 1
                event_type = event.get("type")
                if event_type == "response" and event.get("command") == "prompt":
                    if event.get("success") is not True:
                        raise MinionError(str(event.get("error") or "Prime Agent rejected the prompt."))
                    prompt_accepted = True
                elif event_type == "tool_execution_end":
                    tool_calls.append(
                        {
                            "name": event.get("toolName"),
                            "is_error": bool(event.get("isError", False)),
                        }
                    )
                elif event_type == "agent_end":
                    messages = event.get("messages")
                    final_messages = messages if isinstance(messages, list) else []
                    return {
                        "prompt_accepted": prompt_accepted,
                        "event_count": event_count,
                        "tool_calls": tool_calls,
                        "result": _assistant_text(final_messages),
                        "usage": _usage(final_messages),
                    }

        try:
            result = await asyncio.wait_for(consume(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError as exc:
            await _abort_rpc(process)
            raise MinionError(f"Prime Agent minion timed out after {timeout_seconds} seconds.") from exc
        except asyncio.CancelledError:
            await _abort_rpc(process)
            raise

        final_state = await _get_state(process, "hermes-minion-state-final")
        final_effective = _effective_route(final_state)
        if final_effective != effective:
            raise MinionError(
                f"Prime Agent route changed during the task: started {effective}, ended {final_effective}."
            )
        result.update(
            {
                "effective_route": final_effective,
                "duration_seconds": round(time.monotonic() - started, 3),
                "prime_session_id": final_state.get("sessionId") or initial_state.get("sessionId"),
                "session_file": final_state.get("sessionFile") or initial_state.get("sessionFile"),
            }
        )
    finally:
        await _stop_process(process)
        try:
            diagnostic = await stderr_task
        except Exception:
            diagnostic = ""
    return result, diagnostic


async def delegate_minion(args: dict[str, Any], **_: Any) -> str:
    task = str(args.get("task") or "").strip()
    provider = str(args.get("provider") or "openai-codex").strip()
    model = str(args.get("model") or "gpt-5.6-terra").strip()
    effort = str(args.get("reasoning_effort") or "high").strip()
    timeout_seconds = int(args.get("timeout_seconds") or os.environ.get("PRIME_MINION_TIMEOUT_SECONDS", 1800))
    workdir = Path(str(args.get("workdir") or "")).expanduser().resolve()
    supplied_session_id = str(args.get("session_id") or "").strip()
    explicit_mode = args.get("session_mode")
    session_mode = str(explicit_mode or ("resumable" if supplied_session_id else "ephemeral")).strip()

    route = {"provider": provider, "model": model, "reasoning_effort": effort}
    if not task:
        return json.dumps({"status": "error", "error": "task is required", "route": route})
    if provider != "openai-codex":
        return json.dumps({"status": "error", "error": f"unsupported provider: {provider}", "route": route})
    if model not in _MODELS:
        return json.dumps({"status": "error", "error": f"unsupported model: {model}", "route": route})
    if effort not in _EFFORTS:
        return json.dumps({"status": "error", "error": f"unsupported reasoning_effort: {effort}", "route": route})
    if timeout_seconds < 30 or timeout_seconds > 7200:
        return json.dumps({"status": "error", "error": "timeout_seconds must be between 30 and 7200", "route": route})
    if not workdir.is_dir():
        return json.dumps({"status": "error", "error": f"workdir is not an existing directory: {workdir}", "route": route})
    if session_mode not in {"ephemeral", "resumable"}:
        return json.dumps({"status": "error", "error": f"unsupported session_mode: {session_mode}", "route": route})
    if supplied_session_id and session_mode != "resumable":
        return json.dumps(
            {"status": "error", "error": "session_id requires session_mode=resumable", "route": route}
        )

    bridge: Optional[asyncio.subprocess.Process] = None
    bridge_stderr_task: Optional[asyncio.Task[str]] = None
    runtime = _runtime_root()
    manifest: dict[str, Any] | None = None
    try:
        prime_commit = _runtime_commit(runtime)
        if session_mode == "ephemeral":
            bridge, proxy_base_url, bridge_stderr_task, synthetic_bearer = await _start_bridge()
            result, prime_stderr = await _run_rpc(
                runtime=runtime,
                task=task,
                workdir=workdir,
                requested_route=route,
                timeout_seconds=timeout_seconds,
                proxy_base_url=proxy_base_url,
                synthetic_bearer=synthetic_bearer,
            )
            result.pop("session_file", None)
            result.pop("prime_session_id", None)
            payload: dict[str, Any] = {
                "status": "completed",
                "route": route,
                "effective_route": result.pop("effective_route"),
                "workdir": str(workdir),
                "session_mode": "ephemeral",
                **result,
            }
            if prime_stderr.strip():
                payload["diagnostic_tail"] = prime_stderr[-2000:]
            return json.dumps(payload, ensure_ascii=False)

        if supplied_session_id:
            manifest = load_manifest(supplied_session_id)
        else:
            manifest = create_manifest(workdir=workdir, prime_commit=prime_commit)
        session_id = str(manifest["session_id"])
        with session_lease(session_id):
            manifest = load_manifest(session_id)
            validate_resume_binding(manifest, workdir=workdir, prime_commit=prime_commit)
            existing_resume = resume_file(manifest)
            if int(manifest.get("generation") or 0) > 0 and existing_resume is None:
                raise SessionError("minion session has no durable transcript to resume")
            generation = begin_turn(manifest, route)
            try:
                bridge, proxy_base_url, bridge_stderr_task, synthetic_bearer = await _start_bridge()
                result, prime_stderr = await _run_rpc(
                    runtime=runtime,
                    task=task,
                    workdir=workdir,
                    requested_route=route,
                    timeout_seconds=timeout_seconds,
                    proxy_base_url=proxy_base_url,
                    synthetic_bearer=synthetic_bearer,
                    session_directory=transcript_dir(session_id),
                    resume_path=existing_resume,
                )
                effective_route = result.pop("effective_route")
                prime_session_id = result.pop("prime_session_id")
                persisted_file = result.pop("session_file")
                if not isinstance(prime_session_id, str) or not isinstance(persisted_file, str):
                    raise SessionError("Prime Agent did not return durable session identity")
                record_completed(
                    manifest,
                    effective_route=effective_route,
                    prime_session_id=prime_session_id,
                    session_file=Path(persisted_file),
                )
                payload = {
                    "status": "completed",
                    "route": route,
                    "effective_route": effective_route,
                    "workdir": str(workdir),
                    "session_mode": "resumable",
                    "session_id": session_id,
                    "generation": generation,
                    "session_state": manifest["state"],
                    **result,
                }
                if prime_stderr.strip():
                    payload["diagnostic_tail"] = prime_stderr[-2000:]
                return json.dumps(payload, ensure_ascii=False)
            except asyncio.CancelledError:
                record_interrupted(manifest, "Hermes cancelled the active minion turn")
                raise
            except Exception as exc:
                record_interrupted(manifest, str(exc))
                raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        payload = {"status": "error", "route": route, "workdir": str(workdir), "error": str(exc)}
        if manifest is not None:
            payload.update(
                {
                    "session_mode": "resumable",
                    "session_id": manifest.get("session_id"),
                    "generation": manifest.get("generation"),
                    "session_state": manifest.get("state"),
                }
            )
        return json.dumps(payload, ensure_ascii=False)
    finally:
        await _stop_process(bridge)
        if bridge_stderr_task is not None:
            try:
                await bridge_stderr_task
            except Exception:
                pass


async def minion_session_status(args: dict[str, Any], **_: Any) -> str:
    session_id = str(args.get("session_id") or "").strip()
    try:
        manifest = load_manifest(session_id)
        return json.dumps({"status": "ok", "session": public_status(manifest)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


async def close_minion_session(args: dict[str, Any], **_: Any) -> str:
    session_id = str(args.get("session_id") or "").strip()
    try:
        with session_lease(session_id):
            manifest = load_manifest(session_id)
            close_manifest(manifest)
            return json.dumps({"status": "closed", "session": public_status(manifest)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def register_tools(ctx: Any) -> None:
    ctx.register_tool(
        name="delegate_minion",
        toolset="prime-minion",
        schema=DELEGATE_MINION,
        handler=delegate_minion,
        is_async=True,
    )
    ctx.register_tool(
        name="minion_session_status",
        toolset="prime-minion",
        schema=MINION_SESSION_STATUS,
        handler=minion_session_status,
        is_async=True,
    )
    ctx.register_tool(
        name="close_minion_session",
        toolset="prime-minion",
        schema=CLOSE_MINION_SESSION,
        handler=close_minion_session,
        is_async=True,
    )


__all__ = [
    "close_minion_session",
    "delegate_minion",
    "minion_session_status",
    "register_tools",
]
