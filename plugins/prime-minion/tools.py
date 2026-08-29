"""Hermes tools for ephemeral and resumable Prime Agent minions.

The parent owns session state and one worker handle.  The worker in
``invocation_worker.py`` owns the relay, embedded Prime process, descendants,
private mount and terminal lifecycle verdict.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Optional, Sequence
from urllib.parse import urlparse

from .invocation_worker import (
    CAPABILITY_PROFILE,
    MAX_DIAGNOSTIC_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    UnsupportedLifecycleHost,
    check_capability_profile,
    encode_frame,
    open_provisioned_anchor,
    process_identity,
    provision_fixed_anchor,
    read_frame_stream,
)
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
_EMBEDDED_RPC = _PLUGIN_ROOT / "embedded_rpc.mjs"
_INVOCATION_LAUNCHER = _PLUGIN_ROOT / "invocation_launcher.py"
_INVOCATION_WORKER = _PLUGIN_ROOT / "invocation_worker.py"
_UNSHARE = shutil.which("unshare") or "unshare"
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
    "PASSWORD",
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
        "OPENAI_API_KEY",
    }
)
_CONTROL_WRITE_FD: int | None = None
_PARENT_CLEANUP_MARGIN_SECONDS = 5.0
_WORKER_CLEANUP_BUDGET_SECONDS = 25.0
_LAUNCHER_TERM_GRACE_SECONDS = 5.0
_EVIDENCE_PREFIX = b"PRIME_MINION_EVIDENCE "


class MinionError(RuntimeError):
    pass


class LifecycleCleanupError(MinionError):
    pass


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _synthetic_codex_bearer() -> str:
    """Build a fresh non-secret bearer; the worker relay replaces it."""

    header = _b64url(b'{"alg":"none","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {
                "https://api.openai.com/auth": {"chatgpt_account_id": "hermes-minion-placeholder"},
                "jti": secrets.token_hex(32),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{header}.{payload}."


def _child_environment(proxy_base_url: str, config_dir: Path, synthetic_bearer: str) -> dict[str, str]:
    """Retained compatibility helper for probes; worker applies the same policy."""

    env = _sanitized_environment()
    env.update(
        {
            "PRIME_AGENT_CODING_AGENT_DIR": str(config_dir),
            "HERMES_MINION_PROXY_BASE_URL": proxy_base_url,
            "HERMES_MINION_PROXY_API_KEY": synthetic_bearer,
        }
    )
    return env


def _sanitized_environment() -> dict[str, str]:
    allowed = {
        "HERMES_HOME",
        "HOME",
        "LANG",
        "PATH",
        "PYTHONIOENCODING",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
        "VIRTUAL_ENV",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_")
    }
    env.update({"DO_NOT_TRACK": "1", "PRIME_AGENT_TELEMETRY": "0"})
    return env


def _append_tail(tail: bytearray, value: bytes) -> None:
    tail.extend(value)
    if len(tail) > MAX_DIAGNOSTIC_BYTES:
        del tail[: len(tail) - MAX_DIAGNOSTIC_BYTES]


async def _bounded_stderr(
    stream: Optional[asyncio.StreamReader],
    evidence_sink: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    if stream is None:
        return "", [], "worker diagnostic stderr is unavailable"
    tail = bytearray()
    line_buffer = bytearray()
    evidence: list[dict[str, Any]] = []
    evidence_error: str | None = None
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        line_buffer.extend(chunk)
        while True:
            newline = line_buffer.find(b"\n")
            if newline < 0:
                if len(line_buffer) > MAX_DIAGNOSTIC_BYTES:
                    _append_tail(tail, bytes(line_buffer))
                    line_buffer.clear()
                break
            line = bytes(line_buffer[:newline])
            del line_buffer[: newline + 1]
            if not line.startswith(_EVIDENCE_PREFIX):
                _append_tail(tail, line + b"\n")
                continue
            payload = line[len(_EVIDENCE_PREFIX) :]
            if len(payload) > 64 << 10:
                evidence_error = evidence_error or "worker startup evidence exceeded 64 KiB"
                continue
            try:
                record = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                evidence_error = evidence_error or "worker startup evidence was malformed"
                continue
            if not isinstance(record, dict) or record.get("protocol_version") != PROTOCOL_VERSION or not isinstance(record.get("stage"), str):
                evidence_error = evidence_error or "worker startup evidence violated its contract"
                continue
            evidence.append(record)
            if evidence_sink is not None:
                try:
                    evidence_sink(dict(record))
                except Exception as exc:
                    evidence_error = evidence_error or f"worker evidence sink failed: {exc}"
    if line_buffer:
        _append_tail(tail, bytes(line_buffer))
    return tail.decode("utf-8", errors="replace"), evidence, evidence_error


async def _wait_direct_child(pid: int, timeout: float = 3.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child = _proc_child_pid(pid)
        if child is not None:
            return child
        await asyncio.sleep(0.01)
    return None


def _listener_is_closed(listener: str) -> bool:
    parsed = urlparse(listener)
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((parsed.hostname, parsed.port)) != 0


def _validate_success_evidence(
    records: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    required = ["handlers", "mounted", "relay_ready", "prime_running", "result_ready", "cleanup_complete"]
    stages = [str(record.get("stage")) for record in records]
    if stages != required:
        raise MinionError(f"worker startup evidence stage sequence mismatch: {stages}")
    by_stage = {str(record["stage"]): record for record in records}
    if by_stage["handlers"].get("worker_pid_namespace_pid") != 1:
        raise MinionError("worker startup evidence did not prove namespace PID1")
    for key in ("pid_namespace_inode", "mount_namespace_inode"):
        if not isinstance(by_stage["handlers"].get(key), int):
            raise MinionError(f"worker startup evidence omitted {key}")
    if not isinstance(by_stage["mounted"].get("mount_id"), int):
        raise MinionError("worker startup evidence omitted tmpfs mount ID")
    listener = by_stage["relay_ready"].get("listener")
    if not isinstance(listener, str) or listener != result.get("relay_listener"):
        raise MinionError("worker relay-listener evidence mismatch")
    cleanup = by_stage["cleanup_complete"]
    if any(cleanup.get(key) is not True for key in ("cleanup_verified", "mount_absent", "children_reaped")):
        raise LifecycleCleanupError("worker cleanup evidence did not prove terminal closure")
    return by_stage


async def _stop_process(process: Optional[asyncio.subprocess.Process], *, grace: float = 5.0) -> None:
    """Compatibility stop helper; no process-group or global kill is used."""

    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        try:
            process.kill()
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
    configured_runtime = os.environ.get("PRIME_MINION_RUNTIME_DIR", "").strip()
    if configured_runtime:
        raise MinionError("custom Prime runtime paths are test-only")
    configured_anchor = os.environ.get("PRIME_MINION_ANCHOR_PATH", "").strip()
    if configured_anchor:
        raise MinionError("custom invocation anchor paths are test-only")
    return _DEFAULT_RUNTIME


def _runtime_anchor_path(runtime: Path, *, allow_test_override: bool) -> Path:
    configured = os.environ.get("PRIME_MINION_ANCHOR_PATH", "").strip()
    if configured:
        if not allow_test_override:
            raise MinionError("custom invocation anchor paths are test-only")
        return Path(os.path.abspath(os.path.expanduser(configured)))
    return runtime.parent / "invocation-anchor"


def _runtime_commit(runtime: Path) -> str:
    launcher = runtime / "prime-agent.sh"
    if not launcher.is_file():
        raise MinionError(f"Pinned Prime Agent runtime is missing at {runtime}. Run this plugin's bootstrap script first.")
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
        raise MinionError(f"Prime Agent runtime drift: expected {_PINNED_PRIME_COMMIT}, observed {commit}.")
    return commit


async def _start_bridge() -> NoReturn:
    """The relay is intentionally not parent-owned in the 0.3.0 lifecycle."""

    raise MinionError("Prime relay is owned by the invocation worker")


def _build_worker_command(
    *,
    anchor: Path,
    control_read_fd: int,
    runtime: Path,
    workdir: Path,
    route: Mapping[str, str],
    session_mode: str,
    anchor_fd: int | None = None,
    fixture_commands: tuple[Sequence[str], Sequence[str]] | None = None,
) -> tuple[list[str], tuple[int, int]]:
    """Build the exact U→W command and its two-descriptor pass-fd allowlist."""

    del anchor, runtime, workdir, route, session_mode
    # A placeholder keeps this pure command builder inspectable in unit tests;
    # the real launcher passes its already-open anchor FD explicitly.
    passed_anchor_fd = 18 if anchor_fd is None else anchor_fd
    command = [
        sys.executable,
        str(_INVOCATION_LAUNCHER),
        "--expected-parent-pid",
        str(os.getpid()),
        "--",
        _UNSHARE,
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "--mount-proc",
        sys.executable,
        str(_INVOCATION_WORKER),
        "--control-fd",
        str(control_read_fd),
        "--anchor-fd",
        str(passed_anchor_fd),
    ]
    if fixture_commands is not None:
        relay_fixture, prime_fixture = fixture_commands
        fixture = json.dumps(
            {"relay": list(relay_fixture), "prime": list(prime_fixture)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        command.extend(["--test-fixture-b64", base64.urlsafe_b64encode(fixture).decode("ascii")])
    return command, (control_read_fd, passed_anchor_fd)


def _proc_child_pid(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").strip()
    except OSError:
        return None
    for item in raw.split():
        if item.isdigit():
            return int(item)
    return None


class _ParentInvocation:
    def __init__(self) -> None:
        self.control_write_fd: int | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.cancel_requested = False
        self.stop_sent = False

    def install_control_writer(self, fd: int) -> None:
        if self.control_write_fd is not None:
            raise RuntimeError("control writer was already installed")
        self.control_write_fd = fd
        if self.cancel_requested:
            self.send_stop()
            self.close_control()

    def send_stop(self) -> None:
        if self.stop_sent:
            return
        self.stop_sent = True
        if self.control_write_fd is None:
            return
        try:
            os.write(self.control_write_fd, b"S")
        except (BrokenPipeError, OSError):
            return

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.send_stop()
        self.close_control()

    def close_control(self) -> None:
        fd = self.control_write_fd
        self.control_write_fd = None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    async def terminate_exact_launcher(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_LAUNCHER_TERM_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def _spawn_worker(
    *,
    parent: _ParentInvocation,
    anchor_fd: int,
    control_read_fd: int,
    anchor: Path,
    runtime: Path,
    workdir: Path,
    route: Mapping[str, str],
    session_mode: str,
    relay_command: Sequence[str] | None,
    prime_command: Sequence[str] | None,
) -> asyncio.subprocess.Process:
    if (relay_command is None) != (prime_command is None):
        raise MinionError("test worker fixture requires both relay and Prime commands")
    fixture_commands = None if relay_command is None else (relay_command, prime_command or ())
    command, pass_fds = _build_worker_command(
        anchor=anchor,
        control_read_fd=control_read_fd,
        runtime=runtime,
        workdir=workdir,
        route=route,
        session_mode=session_mode,
        anchor_fd=anchor_fd,
        fixture_commands=fixture_commands,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_sanitized_environment(),
        cwd=str(workdir),
        start_new_session=True,
        close_fds=True,
        pass_fds=pass_fds,
        limit=MAX_RESULT_BYTES + 4,
    )
    parent.process = process
    return process


async def _read_worker_result(stream: asyncio.StreamReader | None) -> dict[str, Any]:
    if stream is None:
        raise ProtocolError("worker result stdout is unavailable")
    return await read_frame_stream(stream, MAX_RESULT_BYTES)


def _request_payload(
    *,
    task: str,
    workdir: Path,
    runtime: Path,
    route: Mapping[str, str],
    timeout_seconds: int,
    session_mode: str,
    session_directory: Path | None,
    resume_path: Path | None,
    anchor: Path,
    anchor_identity: Mapping[str, int],
    synthetic_bearer: str,
    operation: str,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
        "task": task,
        "workdir": str(workdir),
        "runtime": str(runtime),
        "route": dict(route),
        "timeout_seconds": timeout_seconds,
        "session_mode": session_mode,
        "session_directory": str(session_directory) if session_directory is not None else None,
        "resume_path": str(resume_path) if resume_path is not None else None,
        "relay_script": str(_BRIDGE_SCRIPT),
        "extension": str(_PLUGIN_ROOT / "prime_extension.mjs"),
        "synthetic_bearer": synthetic_bearer,
        "anchor_path": str(anchor),
        "anchor_identity": dict(anchor_identity),
    }


async def _drive_invocation(
    *,
    parent: _ParentInvocation,
    task: str,
    workdir: Path,
    runtime: Path,
    route: Mapping[str, str],
    timeout_seconds: int,
    session_mode: str,
    session_directory: Path | None,
    resume_path: Path | None,
    synthetic_bearer: str,
    relay_command: Sequence[str] | None = None,
    prime_command: Sequence[str] | None = None,
    preflight: bool = True,
    evidence_sink: Callable[[dict[str, Any]], None] | None = None,
    operation: str = "delegate",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    fixture_mode = relay_command is not None and prime_command is not None
    anchor = _runtime_anchor_path(runtime, allow_test_override=fixture_mode)
    try:
        if preflight:
            capability = check_capability_profile(anchor)
        else:
            capability = {"profile": CAPABILITY_PROFILE, "status": "skipped"}
        anchor_fd, anchor_identity = open_provisioned_anchor(anchor)
    except UnsupportedLifecycleHost as exc:
        raise MinionError(str(exc)) from exc
    try:
        control_read_fd, control_write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        os.set_blocking(control_write_fd, True)
        parent.install_control_writer(control_write_fd)
        request = _request_payload(
            task=task,
            workdir=workdir,
            runtime=runtime,
            route=route,
            timeout_seconds=timeout_seconds,
            session_mode=session_mode,
            session_directory=session_directory,
            resume_path=resume_path,
            anchor=anchor,
            anchor_identity=anchor_identity,
            synthetic_bearer=synthetic_bearer,
            operation=operation,
        )
        encoded_request = encode_frame(request, MAX_REQUEST_BYTES)
        process = await _spawn_worker(
            parent=parent,
            anchor_fd=anchor_fd,
            control_read_fd=control_read_fd,
            anchor=anchor,
            runtime=runtime,
            workdir=workdir,
            route=route,
            session_mode=session_mode,
            relay_command=relay_command,
            prime_command=prime_command,
        )
        launcher_identity = process_identity(process.pid)
        result_task = asyncio.create_task(_read_worker_result(process.stdout))
        diagnostic_task = asyncio.create_task(_bounded_stderr(process.stderr, evidence_sink))
        worker_host_pid = await _wait_direct_child(process.pid)
        if worker_host_pid is None:
            parent.request_cancel()
            await process.wait()
            raise MinionError("unshare launcher did not expose its namespace PID1 worker")
        worker_identity = process_identity(worker_host_pid)
        if launcher_identity is None or worker_identity is None:
            parent.request_cancel()
            await process.wait()
            raise MinionError("invocation process identity could not be captured")
        worker_pid_namespace = os.stat(f"/proc/{worker_host_pid}/ns/pid").st_ino
        worker_mount_namespace = os.stat(f"/proc/{worker_host_pid}/ns/mnt").st_ino
        control_info = os.fstat(control_read_fd)
        control_pipe_identity = (control_info.st_dev, control_info.st_ino)
        anchor_info = os.fstat(anchor_fd)
        anchor_fd_identity = (anchor_info.st_dev, anchor_info.st_ino)
        os.close(control_read_fd)
        control_read_fd = -1
        os.close(anchor_fd)
        anchor_fd = -1
        if process.stdin is None:
            raise MinionError("worker request stdin is unavailable")
        process.stdin.write(encoded_request)
        await process.stdin.drain()
        process.stdin.close()
        wait_task = asyncio.create_task(process.wait())
        result: dict[str, Any] | None = None
        result_error: Exception | None = None
        while not wait_task.done():
            done, _ = await asyncio.wait({wait_task, result_task}, return_when=asyncio.FIRST_COMPLETED)
            if result_task in done:
                try:
                    result = result_task.result()
                except Exception as exc:
                    result_error = exc
                    parent.send_stop()
                if wait_task.done():
                    break
                if result_error is not None:
                    await wait_task
                    break
        await wait_task
        if result is None:
            try:
                result = await result_task
            except Exception as exc:
                result_error = result_error or exc
        diagnostic, evidence_records, evidence_error = await diagnostic_task
        parent.close_control()
        evidence = {
            "capability": capability,
            "launcher_pid": process.pid,
            "launcher_identity": launcher_identity,
            "worker_host_pid": worker_host_pid,
            "worker_identity": worker_identity,
            "worker_pid_namespace": worker_pid_namespace,
            "worker_mount_namespace": worker_mount_namespace,
            "anchor_identity": anchor_identity,
            "control_pipe_identity": control_pipe_identity,
            "anchor_fd_identity": anchor_fd_identity,
            "startup_evidence": evidence_records,
        }
        if result_error is not None:
            raise MinionError(f"worker result protocol failure: {result_error}") from result_error
        if result is None:
            raise MinionError("worker returned no result")
        if evidence_error is not None:
            raise MinionError(evidence_error)
        if process.returncode != 0:
            detail = str(result.get("diagnostic_tail") or "").strip()
            message = str(result.get("error") or f"worker exited with code {process.returncode}")
            if detail:
                message = f"{message}; worker diagnostic: {detail[-2000:]}"
            if result.get("cleanup_verified") is not True:
                predicates = {
                    key: result.get(key)
                    for key in ("cleanup_verified", "mount_absent", "children_reaped")
                }
                raise LifecycleCleanupError(f"{message}; cleanup predicates={predicates}")
            raise MinionError(message)
        if result.get("status") != "completed":
            raise MinionError(str(result.get("error") or "worker did not produce a completed result"))
        for key in ("cleanup_verified", "mount_absent", "children_reaped"):
            if result.get(key) is not True:
                raise MinionError(f"worker success omitted cleanup predicate: {key}")
        stages = _validate_success_evidence(evidence_records, result)
        listener = str(stages["relay_ready"]["listener"])
        if process_identity(process.pid) == launcher_identity:
            raise LifecycleCleanupError("launcher identity remained after terminal worker result")
        if process_identity(worker_host_pid) == worker_identity:
            raise LifecycleCleanupError("worker identity remained after terminal worker result")
        if not _listener_is_closed(listener):
            raise LifecycleCleanupError("recorded relay listener remained reachable after worker exit")
        if parent.control_write_fd is not None or control_read_fd != -1 or anchor_fd != -1:
            raise LifecycleCleanupError("parent invocation protocol descriptors remained open")
        try:
            current = anchor.stat()
        except OSError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (anchor_identity["dev"], anchor_identity["ino"]):
            if not anchor.is_dir() or any(anchor.iterdir()):
                raise LifecycleCleanupError("fixed invocation anchor was not restored empty")
        return result, diagnostic, evidence
    finally:
        parent.close_control()
        try:
            os.close(locals().get("control_read_fd", -1))
        except OSError:
            pass
        try:
            os.close(anchor_fd)
        except OSError:
            pass


async def _run_invocation(
    *,
    task: str,
    workdir: Path,
    runtime: Path,
    route: Mapping[str, str],
    timeout_seconds: int,
    session_mode: str,
    session_directory: Path | None,
    resume_path: Path | None,
    synthetic_bearer: str | None = None,
    relay_command: Sequence[str] | None = None,
    prime_command: Sequence[str] | None = None,
    preflight: bool = True,
    evidence_sink: Callable[[dict[str, Any]], None] | None = None,
    operation: str = "delegate",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    parent = _ParentInvocation()
    drive_task = asyncio.create_task(
        _drive_invocation(
            parent=parent,
            task=task,
            workdir=workdir,
            runtime=runtime,
            route=route,
            timeout_seconds=timeout_seconds,
            session_mode=session_mode,
            session_directory=session_directory,
            resume_path=resume_path,
            synthetic_bearer=synthetic_bearer or _synthetic_codex_bearer(),
            relay_command=relay_command,
            prime_command=prime_command,
            preflight=preflight,
            evidence_sink=evidence_sink,
            operation=operation,
        )
    )
    cancelled = False
    drive_error: Exception | None = None
    deadline = time.monotonic() + timeout_seconds + _WORKER_CLEANUP_BUDGET_SECONDS + _PARENT_CLEANUP_MARGIN_SECONDS
    while True:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            output = await asyncio.wait_for(asyncio.shield(drive_task), timeout=remaining)
            break
        except asyncio.CancelledError:
            cancelled = True
            parent.request_cancel()
            deadline = min(
                deadline,
                time.monotonic() + _WORKER_CLEANUP_BUDGET_SECONDS + _PARENT_CLEANUP_MARGIN_SECONDS,
            )
            continue
        except asyncio.TimeoutError:
            parent.request_cancel()
            await parent.terminate_exact_launcher()
            try:
                await asyncio.wait_for(asyncio.shield(drive_task), timeout=_LAUNCHER_TERM_GRACE_SECONDS)
            except Exception:
                pass
            if not drive_task.done():
                drive_task.cancel()
                try:
                    await drive_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise LifecycleCleanupError("invocation worker exceeded its terminal cleanup budget")
        except Exception as exc:
            if not cancelled:
                raise
            drive_error = exc
            break
    parent.close_control()
    if cancelled:
        if isinstance(drive_error, LifecycleCleanupError):
            raise drive_error
        raise asyncio.CancelledError
    if drive_error is not None:
        raise drive_error
    return output


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
    del proxy_base_url
    result, diagnostic, _evidence = await _run_invocation(
        task=task,
        workdir=workdir,
        runtime=runtime,
        route=requested_route,
        timeout_seconds=timeout_seconds,
        session_mode="resumable" if session_directory is not None else "ephemeral",
        session_directory=session_directory,
        resume_path=resume_path,
        synthetic_bearer=synthetic_bearer,
        preflight=True,
    )
    result = dict(result)
    for key in ("status", "route", "cleanup_verified", "mount_absent", "children_reaped", "diagnostic_tail", "worker_pid_namespace_pid", "relay_listener"):
        result.pop(key, None)
    return result, diagnostic


def _effective_route(state: dict[str, Any]) -> dict[str, str]:
    model = state.get("model")
    if not isinstance(model, dict):
        raise MinionError("Prime Agent state did not expose an effective model.")
    provider = model.get("provider")
    model_id = model.get("id")
    effort = _PRIME_TO_EFFORT.get(str(state.get("thinkingLevel")))
    if not isinstance(provider, str) or not isinstance(model_id, str) or effort is None:
        raise MinionError("Prime Agent state exposed an invalid effective route.")
    return {"provider": provider, "model": model_id, "reasoning_effort": effort}


def _fake_relay_command(boundary_file: Path | None = None) -> list[str]:
    code = textwrap.dedent(
        """
        import json, os, pathlib, signal, socket, sys, time
        if len(sys.argv) > 1:
            descriptors = {}
            for name in os.listdir('/proc/self/fd'):
                if not name.isdigit() or int(name) <= 2:
                    continue
                try:
                    info = os.fstat(int(name))
                    target = os.readlink(f'/proc/self/fd/{name}')
                except OSError:
                    continue
                descriptors[name] = {'dev': info.st_dev, 'ino': info.st_ino, 'mode': info.st_mode, 'target': target}
            safe_paths = {key: os.environ[key] for key in ('HOME', 'HERMES_HOME', 'TMPDIR', 'PRIME_AGENT_CODING_AGENT_DIR') if key in os.environ}
            pathlib.Path(sys.argv[1]).write_text(json.dumps({'argv': sys.argv, 'env_keys': sorted(os.environ), 'env_paths': safe_paths, 'fds': descriptors}), encoding='utf-8')
        sys.stdin.readline()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(1)
        print(json.dumps({'ready': True, 'host': '127.0.0.1', 'port': sock.getsockname()[1]}), flush=True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        time.sleep(120)
        """
    )
    command = [sys.executable, "-c", code]
    if boundary_file is not None:
        command.append(str(boundary_file))
    return command


def _fake_prime_command(boundary_file: Path | None = None) -> list[str]:
    code = textwrap.dedent(
        """
        import json, os, pathlib, sys
        if len(sys.argv) > 1:
            descriptors = {}
            for name in os.listdir('/proc/self/fd'):
                if not name.isdigit() or int(name) <= 2:
                    continue
                try:
                    info = os.fstat(int(name))
                    target = os.readlink(f'/proc/self/fd/{name}')
                except OSError:
                    continue
                descriptors[name] = {'dev': info.st_dev, 'ino': info.st_ino, 'mode': info.st_mode, 'target': target}
            safe_paths = {key: os.environ[key] for key in ('HOME', 'HERMES_HOME', 'TMPDIR', 'PRIME_AGENT_CODING_AGENT_DIR') if key in os.environ}
            pathlib.Path(sys.argv[1]).write_text(json.dumps({'argv': sys.argv, 'env_keys': sorted(os.environ), 'env_paths': safe_paths, 'fds': descriptors}), encoding='utf-8')
        route = {'provider': 'openai-codex', 'id': 'gpt-5.6-luna'}
        effort = 'max'
        home = pathlib.Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR'])
        tmp = pathlib.Path(os.environ['TMPDIR'])
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / 'worker-proof').write_text('ok', encoding='utf-8')
        def state():
            return {'model': {**route, 'baseUrl': os.environ.get('HERMES_MINION_PROXY_BASE_URL')}, 'thinkingLevel': effort}
        for raw in sys.stdin:
            request = json.loads(raw)
            kind = request.get('type')
            rid = request.get('id')
            if kind == 'get_state':
                print(json.dumps({'type':'response','id':rid,'command':'get_state','success':True,'data':state()}), flush=True)
            elif kind == 'prompt':
                print(json.dumps({'type':'response','id':rid,'command':'prompt','success':True}), flush=True)
                print(json.dumps({'type':'agent_end','messages':[{'role':'assistant','content':[{'type':'text','text':'NO_PROVIDER_OK'}]}]}), flush=True)
            elif kind == 'abort':
                print(json.dumps({'type':'response','id':rid,'command':'abort','success':True}), flush=True)
            else:
                print(json.dumps({'type':'response','id':rid,'command':kind,'success':True}), flush=True)
        """
    )
    command = [sys.executable, "-c", code]
    if boundary_file is not None:
        command.append(str(boundary_file))
    return command


def _fake_blocking_prime_command() -> list[str]:
    code = textwrap.dedent(
        """
        import json, os, pathlib, sys, time
        route = {'provider': 'openai-codex', 'id': 'gpt-5.6-luna'}
        effort = 'max'
        pathlib.Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR']).mkdir(parents=True, exist_ok=True)
        pathlib.Path(os.environ['TMPDIR']).mkdir(parents=True, exist_ok=True)
        state = {'model': {**route, 'baseUrl': os.environ.get('HERMES_MINION_PROXY_BASE_URL')}, 'thinkingLevel': effort}
        for raw in sys.stdin:
            request = json.loads(raw)
            kind = request.get('type')
            rid = request.get('id')
            if kind == 'get_state':
                print(json.dumps({'type':'response','id':rid,'command':'get_state','success':True,'data':state}), flush=True)
            elif kind == 'prompt':
                print(json.dumps({'type':'response','id':rid,'command':'prompt','success':True}), flush=True)
                time.sleep(120)
            elif kind == 'abort':
                print(json.dumps({'type':'response','id':rid,'command':'abort','success':True}), flush=True)
        """
    )
    return [sys.executable, "-c", code]


def _fake_detached_prime_command(pid_file: Path) -> list[str]:
    code = textwrap.dedent(
        f"""
        import json, os, pathlib, signal, sys, time
        route = {{'provider': 'openai-codex', 'id': 'gpt-5.6-luna'}}
        effort = 'max'
        pathlib.Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR']).mkdir(parents=True, exist_ok=True)
        pathlib.Path(os.environ['TMPDIR']).mkdir(parents=True, exist_ok=True)
        state = {{'model': {{**route, 'baseUrl': os.environ.get('HERMES_MINION_PROXY_BASE_URL')}}, 'thinkingLevel': effort}}
        for raw in sys.stdin:
            request = json.loads(raw)
            kind = request.get('type')
            rid = request.get('id')
            if kind == 'get_state':
                print(json.dumps({{'type':'response','id':rid,'command':'get_state','success':True,'data':state}}), flush=True)
            elif kind == 'prompt':
                first = os.fork()
                if first == 0:
                    os.setsid()
                    second = os.fork()
                    if second != 0:
                        os._exit(0)
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    signal.signal(signal.SIGHUP, signal.SIG_IGN)
                    nspid = next(line for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('NSpid:'))
                    host_pid = nspid.split()[1]
                    pathlib.Path({str(pid_file)!r}).write_text(host_pid, encoding='ascii')
                    while True:
                        time.sleep(60)
                os.waitpid(first, 0)
                print(json.dumps({{'type':'response','id':rid,'command':'prompt','success':True}}), flush=True)
                print(json.dumps({{'type':'agent_end','messages':[{{'role':'assistant','content':[{{'type':'text','text':'DETACHED_OK'}}]}}]}}), flush=True)
            elif kind == 'abort':
                print(json.dumps({{'type':'response','id':rid,'command':'abort','success':True}}), flush=True)
        """
    )
    return [sys.executable, "-c", code]


def _fake_replacement_prime_command(ready_file: Path, continue_file: Path) -> list[str]:
    code = textwrap.dedent(
        f"""
        import json, os, pathlib, sys, time
        route = {{'provider': 'openai-codex', 'id': 'gpt-5.6-luna'}}
        effort = 'max'
        pathlib.Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR']).mkdir(parents=True, exist_ok=True)
        pathlib.Path(os.environ['TMPDIR']).mkdir(parents=True, exist_ok=True)
        state = {{'model': {{**route, 'baseUrl': os.environ.get('HERMES_MINION_PROXY_BASE_URL')}}, 'thinkingLevel': effort}}
        for raw in sys.stdin:
            request = json.loads(raw)
            kind = request.get('type')
            rid = request.get('id')
            if kind == 'get_state':
                print(json.dumps({{'type':'response','id':rid,'command':'get_state','success':True,'data':state}}), flush=True)
            elif kind == 'prompt':
                pathlib.Path({str(ready_file)!r}).write_text('ready', encoding='ascii')
                deadline = time.monotonic() + 20
                while not pathlib.Path({str(continue_file)!r}).exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit('replacement continuation timed out')
                    time.sleep(0.02)
                print(json.dumps({{'type':'response','id':rid,'command':'prompt','success':True}}), flush=True)
                print(json.dumps({{'type':'agent_end','messages':[{{'role':'assistant','content':[{{'type':'text','text':'REPLACEMENT_OK'}}]}}]}}), flush=True)
            elif kind == 'abort':
                print(json.dumps({{'type':'response','id':rid,'command':'abort','success':True}}), flush=True)
        """
    )
    return [sys.executable, "-c", code]


def run_no_provider_invocation_for_test(root: Path) -> dict[str, Any]:
    """T3 credential-free real namespace probe used by the focused product suite."""

    root = Path(root).resolve()
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_fixed_anchor(anchor)
    old_anchor_env = os.environ.get("PRIME_MINION_ANCHOR_PATH")
    os.environ["PRIME_MINION_ANCHOR_PATH"] = str(anchor)
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}
    try:
        result, _diagnostic, evidence = asyncio.run(
            _run_invocation(
                task="credential-free lifecycle probe",
                workdir=root,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=_fake_relay_command(),
                prime_command=_fake_prime_command(),
                preflight=True,
            )
        )
    finally:
        if old_anchor_env is None:
            os.environ.pop("PRIME_MINION_ANCHOR_PATH", None)
        else:
            os.environ["PRIME_MINION_ANCHOR_PATH"] = old_anchor_env
    return {
        "status": result["status"],
        "worker_pid_namespace_pid": result.get("worker_pid_namespace_pid"),
        "cleanup_verified": result.get("cleanup_verified"),
        "anchor_survives": anchor.is_dir() and not any(anchor.iterdir()),
        "provider_calls": 0,
        "real_provider_credential_transmitted": False,
        "evidence": evidence,
    }


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
        return json.dumps({"status": "error", "error": "session_id requires session_mode=resumable", "route": route})

    manifest: dict[str, Any] | None = None
    try:
        runtime = _runtime_root()
        prime_commit = _runtime_commit(runtime)
        if session_mode == "ephemeral":
            result, prime_stderr = await _run_rpc(
                runtime=runtime,
                task=task,
                workdir=workdir,
                requested_route=route,
                timeout_seconds=timeout_seconds,
                proxy_base_url="",
                synthetic_bearer=_synthetic_codex_bearer(),
            )
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
                result, prime_stderr = await _run_rpc(
                    runtime=runtime,
                    task=task,
                    workdir=workdir,
                    requested_route=route,
                    timeout_seconds=timeout_seconds,
                    proxy_base_url="",
                    synthetic_bearer=_synthetic_codex_bearer(),
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
    ctx.register_tool(name="delegate_minion", toolset="prime-minion", schema=DELEGATE_MINION, handler=delegate_minion, is_async=True)
    ctx.register_tool(name="minion_session_status", toolset="prime-minion", schema=MINION_SESSION_STATUS, handler=minion_session_status, is_async=True)
    ctx.register_tool(name="close_minion_session", toolset="prime-minion", schema=CLOSE_MINION_SESSION, handler=close_minion_session, is_async=True)


__all__ = [
    "close_minion_session",
    "delegate_minion",
    "minion_session_status",
    "register_tools",
]
