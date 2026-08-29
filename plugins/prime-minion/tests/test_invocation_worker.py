from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "prime_minion_invocation_testpkg"


def proc_children(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").strip()
    except OSError:
        return []
    return [int(item) for item in raw.split() if item.isdigit()]


def proc_tree(root_pid: int) -> list[int]:
    result: list[int] = []
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        for child in proc_children(pid):
            if child not in result:
                result.append(child)
                pending.append(child)
    return result


def load_module(name: str):
    for key in [key for key in sys.modules if key == PACKAGE or key.startswith(f"{PACKAGE}.")]:
        sys.modules.pop(key, None)
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    for module_name in ("schemas", "sessions", "invocation_worker", "tools"):
        module_path = ROOT / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module_name}", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PACKAGE}.{module_name}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.{name}"]


def load_bootstrap_module():
    module_path = ROOT / "scripts" / "bootstrap_runtime.py"
    spec = importlib.util.spec_from_file_location("prime_minion_bootstrap_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def provision_test_anchor(tools, runtime: Path, anchor: Path) -> None:
    runtime.chmod(0o700)
    tools.provision_fixed_anchor(anchor)


def test_invocation_worker_exports_bounded_protocol_contract() -> None:
    worker_path = ROOT / "invocation_worker.py"
    assert worker_path.is_file(), "invocation_worker.py is the required lifecycle owner"
    worker = load_module("invocation_worker")
    assert worker.MAX_REQUEST_BYTES == 1 << 20
    assert worker.MAX_RESULT_BYTES == 2 << 20
    assert worker.MAX_RPC_LINE_BYTES == 4 << 20
    assert worker.MAX_DIAGNOSTIC_BYTES == 64 << 10
    assert worker.PROTOCOL_VERSION == 1


def test_frame_codec_accepts_one_object_and_rejects_protocol_variants() -> None:
    worker = load_module("invocation_worker")
    encoded = worker.encode_frame({"type": "request", "task": "ok"}, worker.MAX_REQUEST_BYTES)
    assert int.from_bytes(encoded[:4], "big") == len(encoded) - 4
    assert worker.decode_frame(encoded, worker.MAX_REQUEST_BYTES) == {
        "type": "request",
        "task": "ok",
    }
    with pytest.raises(worker.ProtocolError, match="object"):
        worker.decode_frame(worker.encode_frame(["not", "an", "object"], worker.MAX_REQUEST_BYTES), worker.MAX_REQUEST_BYTES)
    with pytest.raises(worker.ProtocolError, match="multiple|trailing"):
        worker.decode_frame(encoded + encoded, worker.MAX_REQUEST_BYTES)
    with pytest.raises(worker.ProtocolError, match="exceeds|maximum"):
        worker.encode_frame({"payload": "x" * worker.MAX_REQUEST_BYTES}, worker.MAX_REQUEST_BYTES)


def test_fixed_anchor_uses_fd_identity_and_survives_cleanup(tmp_path: Path) -> None:
    worker = load_module("invocation_worker")
    anchor = tmp_path / "anchor"
    expected = worker.provision_fixed_anchor(anchor)
    assert worker.verify_fixed_anchor(anchor) == expected
    receipt = worker.anchor_receipt_path(anchor)
    assert receipt.is_file()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    fd = worker.open_anchor(anchor)
    try:
        identity = worker.anchor_identity(fd)
        assert identity["uid"] == os.getuid()
        assert identity["mode"] == 0o700
        worker.verify_anchor_path(anchor, fd, identity)
        assert worker.is_persistent_anchor(anchor)
        worker.assert_no_recursive_anchor_cleanup()
    finally:
        os.close(fd)
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


def test_provision_failure_never_cleans_replacement_pathnames(
    tmp_path: Path, monkeypatch
) -> None:
    worker = load_module("invocation_worker")
    anchor = tmp_path / "anchor"
    receipt = tmp_path / ".anchor.identity.json"
    saved_anchor = tmp_path / "saved-anchor"
    saved_receipt = tmp_path / "saved-receipt"

    def replace_then_fail(_fd: int, _payload) -> int:
        anchor.rename(saved_anchor)
        anchor.mkdir(mode=0o700)
        receipt.rename(saved_receipt)
        receipt.write_text("FOREIGN", encoding="ascii")
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(worker.os, "write", replace_then_fail)
    with pytest.raises(OSError, match="injected receipt write failure"):
        worker.provision_fixed_anchor(anchor)

    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []
    assert receipt.read_text(encoding="ascii") == "FOREIGN"
    assert saved_anchor.is_dir()
    assert saved_receipt.is_file()


@pytest.mark.parametrize("foreign_mode", [0o700, 0o755])
def test_pre_admission_foreign_anchor_is_untouched_and_never_spawns(
    tmp_path: Path, monkeypatch, foreign_mode: int
) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor_parent = tmp_path / "anchor-parent"
    anchor_parent.mkdir(mode=0o700)
    anchor = anchor_parent / "invocation-anchor"
    tools.provision_fixed_anchor(anchor)
    saved_anchor = anchor_parent / "saved-anchor"
    anchor.rename(saved_anchor)
    anchor.mkdir(mode=foreign_mode)
    os.chmod(anchor, foreign_mode)
    sentinel = anchor / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    spawn_called = False

    async def forbidden_spawn(**_kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("worker spawn must not be reached for an unbound anchor")

    monkeypatch.setattr(tools, "_spawn_worker", forbidden_spawn)
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}
    with pytest.raises(tools.MinionError, match="anchor|identity|provision"):
        asyncio.run(
            tools._run_invocation(
                task="foreign anchor admission probe",
                workdir=tmp_path,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=tools._fake_relay_command(),
                prime_command=tools._fake_prime_command(),
                preflight=False,
            )
        )

    assert not spawn_called
    assert stat.S_IMODE(anchor.stat().st_mode) == foreign_mode
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert sorted(path.name for path in anchor.iterdir()) == ["foreign-sentinel"]
    assert saved_anchor.is_dir()
    assert list(saved_anchor.iterdir()) == []


def test_pre_admission_foreign_parent_is_untouched_and_child_is_not_created(
    tmp_path: Path, monkeypatch
) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    foreign_parent = tmp_path / "anchor-parent"
    foreign_parent.mkdir(mode=0o700)
    anchor = foreign_parent / "invocation-anchor"
    tools.provision_fixed_anchor(anchor)
    saved_parent = tmp_path / "saved-anchor-parent"
    foreign_parent.rename(saved_parent)
    foreign_parent.mkdir(mode=0o755)
    os.chmod(foreign_parent, 0o755)
    sentinel = foreign_parent / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    spawn_called = False

    async def forbidden_spawn(**_kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("worker spawn must not be reached for an unbound anchor route")

    monkeypatch.setattr(tools, "_spawn_worker", forbidden_spawn)
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}
    with pytest.raises(tools.MinionError, match="anchor|identity|provision"):
        asyncio.run(
            tools._run_invocation(
                task="foreign parent admission probe",
                workdir=tmp_path,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=tools._fake_relay_command(),
                prime_command=tools._fake_prime_command(),
                preflight=False,
            )
        )

    assert not spawn_called
    assert stat.S_IMODE(foreign_parent.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert not anchor.exists()
    assert sorted(path.name for path in foreign_parent.iterdir()) == ["foreign-sentinel"]
    assert (saved_parent / "invocation-anchor").is_dir()
    assert (saved_parent / ".invocation-anchor.identity.json").is_file()


def test_production_rejects_anchor_environment_override_before_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir(mode=0o755)
    os.chmod(foreign_parent, 0o755)
    sentinel = foreign_parent / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    anchor = foreign_parent / "invocation-anchor"
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))

    with pytest.raises(tools.MinionError, match="test-only"):
        asyncio.run(
            tools._run_invocation(
                task="production anchor override probe",
                workdir=tmp_path,
                runtime=runtime,
                route={"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                preflight=False,
            )
        )

    assert stat.S_IMODE(foreign_parent.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert not anchor.exists()


def test_public_handler_rejects_direct_anchor_override_before_commit_or_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    tools = load_module("tools")
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir(mode=0o700)
    anchor = foreign_parent / "invocation-anchor"
    tools.provision_fixed_anchor(anchor)
    sentinel = anchor / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    receipt = foreign_parent / ".invocation-anchor.identity.json"
    receipt_before = receipt.read_bytes()
    parent_mode_before = stat.S_IMODE(foreign_parent.stat().st_mode)
    anchor_mode_before = stat.S_IMODE(anchor.stat().st_mode)
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    commit_checked = False
    spawn_called = False

    def forbidden_commit_check(_observed_runtime: Path) -> str:
        nonlocal commit_checked
        commit_checked = True
        return tools._PINNED_PRIME_COMMIT

    async def forbidden_rpc(**_kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("production anchor override reached worker spawn")

    monkeypatch.setattr(tools, "_runtime_commit", forbidden_commit_check)
    monkeypatch.setattr(tools, "_run_rpc", forbidden_rpc)
    payload = json.loads(
        asyncio.run(
            tools.delegate_minion(
                {
                    "task": "production direct anchor override probe",
                    "workdir": str(tmp_path),
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "timeout_seconds": 30,
                    "session_mode": "ephemeral",
                }
            )
        )
    )

    assert payload["status"] == "error"
    assert "test-only" in payload["error"]
    assert not commit_checked
    assert not spawn_called
    assert stat.S_IMODE(foreign_parent.stat().st_mode) == parent_mode_before
    assert stat.S_IMODE(anchor.stat().st_mode) == anchor_mode_before
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert receipt.read_bytes() == receipt_before


def test_production_rejects_indirect_runtime_anchor_override_before_spawn_or_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    tools = load_module("tools")
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir(mode=0o700)
    runtime = foreign_parent / "prime-agent"
    runtime.mkdir(mode=0o700)
    anchor = foreign_parent / "invocation-anchor"
    provision_test_anchor(tools, foreign_parent, anchor)
    sentinel = anchor / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    receipt = foreign_parent / ".invocation-anchor.identity.json"
    receipt_before = receipt.read_bytes()
    parent_mode_before = stat.S_IMODE(foreign_parent.stat().st_mode)
    anchor_mode_before = stat.S_IMODE(anchor.stat().st_mode)
    monkeypatch.setenv("PRIME_MINION_RUNTIME_DIR", str(runtime))
    commit_checked = False
    spawn_called = False

    def forbidden_commit_check(observed_runtime: Path) -> str:
        nonlocal commit_checked
        commit_checked = True
        assert observed_runtime == runtime
        return tools._PINNED_PRIME_COMMIT

    async def forbidden_rpc(**_kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("production runtime override reached worker spawn")

    monkeypatch.setattr(tools, "_runtime_commit", forbidden_commit_check)
    monkeypatch.setattr(tools, "_run_rpc", forbidden_rpc)
    payload = json.loads(
        asyncio.run(
            tools.delegate_minion(
                {
                    "task": "production runtime override probe",
                    "workdir": str(tmp_path),
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "timeout_seconds": 30,
                    "session_mode": "ephemeral",
                }
            )
        )
    )

    assert payload["status"] == "error"
    assert "test-only" in payload["error"]
    assert not commit_checked
    assert not spawn_called
    assert stat.S_IMODE(foreign_parent.stat().st_mode) == parent_mode_before
    assert stat.S_IMODE(anchor.stat().st_mode) == anchor_mode_before
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert receipt.read_bytes() == receipt_before


def test_bootstrap_creates_only_missing_fixed_runtime_parent(tmp_path: Path, monkeypatch) -> None:
    bootstrap = load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "RUNTIME", tmp_path / ".runtime" / "prime-agent")

    parent = bootstrap.ensure_runtime_parent()

    assert parent == tmp_path / ".runtime"
    assert parent.is_dir()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert list(parent.iterdir()) == []


def test_bootstrap_rejects_existing_foreign_parent_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    bootstrap = load_bootstrap_module()
    parent = tmp_path / ".runtime"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    sentinel = parent / "foreign-sentinel"
    sentinel.write_text("KEEP", encoding="ascii")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "RUNTIME", parent / "prime-agent")

    with pytest.raises(SystemExit, match="will not be chmod-repaired"):
        bootstrap.ensure_runtime_parent()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert sorted(path.name for path in parent.iterdir()) == ["foreign-sentinel"]


def test_stale_lease_requires_pid_start_time_and_boot_id(tmp_path: Path, monkeypatch) -> None:
    sessions = load_module("sessions")
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="a" * 40)
    root = sessions.session_root(manifest["session_id"])
    lease = root / ".lease"
    lease.mkdir(mode=0o700)
    (lease / "owner.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "proc_start_time": "not-the-live-start-time",
                "boot_id": "not-the-live-boot-id",
            }
        ),
        encoding="utf-8",
    )
    with sessions.session_lease(manifest["session_id"]):
        owner = json.loads((root / ".lease" / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["proc_start_time"] != "not-the-live-start-time"
        assert owner["boot_id"] != "not-the-live-boot-id"


def test_live_exact_lease_is_busy_and_release_preserves_replacement(tmp_path: Path, monkeypatch) -> None:
    sessions = load_module("sessions")
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="a" * 40)
    root = sessions.session_root(manifest["session_id"])
    with sessions.session_lease(manifest["session_id"]):
        with pytest.raises(sessions.SessionBusyError, match="already active"):
            with sessions.session_lease(manifest["session_id"]):
                pytest.fail("live exact lease was stolen")
        lease = root / ".lease"
        saved = root / ".lease.saved"
        lease.rename(saved)
        lease.mkdir(mode=0o700)
        (lease / "foreign-sentinel").write_text("KEEP", encoding="ascii")
    assert (root / ".lease" / "foreign-sentinel").read_text(encoding="ascii") == "KEEP"
    assert (root / ".lease.saved" / "owner.json").is_file()


def test_stale_running_turn_repairs_before_resume_and_close(tmp_path: Path, monkeypatch) -> None:
    sessions = load_module("sessions")
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    resume_manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="a" * 40)
    with sessions.session_lease(resume_manifest["session_id"]):
        sessions.begin_turn(resume_manifest, route)
    with sessions.session_lease(resume_manifest["session_id"]):
        loaded = sessions.load_manifest(resume_manifest["session_id"])
        with pytest.raises(sessions.SessionBusyError, match="live exact owner"):
            sessions.begin_turn(loaded, route)
    resume_manifest["turns"][-1]["lease_owner"]["proc_start_time"] = "stale-start-time"
    sessions.write_manifest(resume_manifest)
    with sessions.session_lease(resume_manifest["session_id"]):
        loaded = sessions.load_manifest(resume_manifest["session_id"])
        generation = sessions.begin_turn(loaded, route)
    repaired = sessions.load_manifest(resume_manifest["session_id"])
    assert generation == 2
    assert repaired["schema_version"] == sessions.SESSION_SCHEMA_VERSION == 1
    assert repaired["turns"][0]["status"] == "INTERRUPTED"
    assert repaired["turns"][1]["status"] == "RUNNING"

    close_manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="a" * 40)
    with sessions.session_lease(close_manifest["session_id"]):
        sessions.begin_turn(close_manifest, route)
    close_manifest["turns"][-1]["lease_owner"]["proc_start_time"] = "stale-start-time"
    sessions.write_manifest(close_manifest)
    with sessions.session_lease(close_manifest["session_id"]):
        loaded = sessions.load_manifest(close_manifest["session_id"])
        sessions.close_manifest(loaded)
    closed = sessions.load_manifest(close_manifest["session_id"])
    assert closed["state"] == "CLOSED"
    assert closed["turns"][-1]["status"] == "INTERRUPTED"


def test_worker_command_passes_only_control_read_and_anchor(tmp_path: Path) -> None:
    tools = load_module("tools")
    command, pass_fds = tools._build_worker_command(
        anchor=tmp_path / "anchor",
        control_read_fd=17,
        runtime=tmp_path / "runtime",
        workdir=tmp_path,
        route={
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
        },
        session_mode="ephemeral",
    )
    assert command[:2] == [sys.executable, str(tools._INVOCATION_LAUNCHER)]
    assert command[command.index("--") + 1 : command.index("--") + 3] == [tools._UNSHARE, "--user"]
    assert "--mount-proc" in command
    assert "--control-fd" in command
    assert "--anchor-fd" in command
    assert pass_fds == (17, int(command[command.index("--anchor-fd") + 1]))
    assert tools._CONTROL_WRITE_FD not in pass_fds
    assert "--daemon-socket" not in command


def test_child_runtime_environment_uses_the_duplicated_runtime_fd() -> None:
    worker = load_module("invocation_worker")
    remapped = worker.remap_runtime_environment(
        {
            "PRIME_AGENT_CODING_AGENT_DIR": "/proc/self/fd/7/agent-home",
            "TMPDIR": "/proc/self/fd/7/tmp",
            "UNCHANGED": "/proc/self/fd/70/not-the-worker-root",
        },
        worker_runtime_fd=7,
        child_runtime_fd=11,
    )
    assert remapped == {
        "PRIME_AGENT_CODING_AGENT_DIR": "/proc/self/fd/11/agent-home",
        "TMPDIR": "/proc/self/fd/11/tmp",
        "UNCHANGED": "/proc/self/fd/70/not-the-worker-root",
    }


def test_worker_relay_and_prime_environments_are_role_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = load_module("invocation_worker")
    tools = load_module("tools")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("HERMES_HOME", "/host/hermes")
    monkeypatch.setenv("UNEXPECTED_TOKEN", "must-not-pass")
    monkeypatch.setenv("SOME_KEY", "must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")

    launcher_env = tools._sanitized_environment()
    relay_env = worker.InvocationWorker._allowlisted_environment(relay=True)
    prime_env = worker.InvocationWorker._allowlisted_environment(relay=False)

    assert launcher_env["PATH"] == "/usr/bin"
    assert launcher_env["HOME"] == "/host/home"
    assert launcher_env["HERMES_HOME"] == "/host/hermes"
    assert relay_env["HOME"] == "/host/home"
    assert relay_env["HERMES_HOME"] == "/host/hermes"
    assert "HOME" not in prime_env and "HERMES_HOME" not in prime_env
    for env in (launcher_env, relay_env, prime_env):
        assert "UNEXPECTED_TOKEN" not in env
        assert "SOME_KEY" not in env
        assert "OPENAI_API_KEY" not in env


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_actual_nested_children_receive_only_role_env_and_runtime_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    relay_boundary = tmp_path / "relay-boundary.json"
    prime_boundary = tmp_path / "prime-boundary.json"
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("HERMES_HOME", "/host/hermes")
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET_SENTINEL_MUST_NOT_PASS")
    monkeypatch.setenv("UNEXPECTED_TOKEN", "SECRET_SENTINEL_MUST_NOT_PASS")

    result, _diagnostic, evidence = asyncio.run(
        tools._run_invocation(
            task="credential-free boundary probe",
            workdir=tmp_path,
            runtime=runtime,
            route={"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
            timeout_seconds=30,
            session_mode="ephemeral",
            session_directory=None,
            resume_path=None,
            relay_command=tools._fake_relay_command(relay_boundary),
            prime_command=tools._fake_prime_command(prime_boundary),
            preflight=True,
        )
    )
    assert result["status"] == "completed"
    relay = json.loads(relay_boundary.read_text(encoding="utf-8"))
    prime = json.loads(prime_boundary.read_text(encoding="utf-8"))
    assert relay["env_paths"]["HOME"] == "/host/home"
    assert relay["env_paths"]["HERMES_HOME"] == "/host/hermes"
    assert "HERMES_HOME" not in prime["env_keys"]
    assert prime["env_paths"]["HOME"] == prime["env_paths"]["PRIME_AGENT_CODING_AGENT_DIR"]
    for observation in (relay, prime):
        assert "OPENAI_API_KEY" not in observation["env_keys"]
        assert "UNEXPECTED_TOKEN" not in observation["env_keys"]
        assert "SECRET_SENTINEL_MUST_NOT_PASS" not in " ".join(observation["argv"])
        descriptors = list(observation["fds"].values())
        assert descriptors
        assert all(not stat.S_ISFIFO(int(item["mode"])) for item in descriptors)
        assert all(
            (int(item["dev"]), int(item["ino"])) != tuple(evidence["anchor_fd_identity"])
            for item in descriptors
        )
        assert all(
            (int(item["dev"]), int(item["ino"])) != tuple(evidence["control_pipe_identity"])
            for item in descriptors
        )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_parallel_invocations_share_only_fixed_anchor_not_private_mount(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    async def scenario():
        return await asyncio.gather(
            *(
                tools._run_invocation(
                    task=f"parallel-{index}",
                    workdir=tmp_path,
                    runtime=runtime,
                    route=route,
                    timeout_seconds=30,
                    session_mode="ephemeral",
                    session_directory=None,
                    resume_path=None,
                    relay_command=tools._fake_relay_command(),
                    prime_command=tools._fake_prime_command(),
                    preflight=True,
                )
                for index in range(2)
            )
        )

    outputs = asyncio.run(scenario())
    assert [item[0]["status"] for item in outputs] == ["completed", "completed"]
    assert len({item[2]["worker_mount_namespace"] for item in outputs}) == 2
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


def test_prime_rpc_event_flood_fails_promptly_but_drains_to_eof() -> None:
    worker = load_module("invocation_worker")

    async def scenario() -> tuple[int, bool]:
        stream = asyncio.StreamReader()
        payload = b"".join(
            json.dumps({"type": "event", "index": index}, separators=(",", ":")).encode() + b"\n"
            for index in range(worker.MAX_RPC_EVENTS + 32)
        )
        stream.feed_data(payload)
        stream.feed_eof()
        process = types.SimpleNamespace(stdout=stream, stdin=None)
        rpc = worker._PrimeRPC(process)
        await rpc.reader_task
        with pytest.raises(worker._RPCStreamFailure, match="256 records"):
            await rpc.next_event(timeout=0.01)
        return rpc.events.qsize(), stream.at_eof()

    queue_size, at_eof = asyncio.run(scenario())
    assert queue_size <= 1
    assert at_eof is True


def test_relay_readiness_reader_rejects_at_64_kib_not_stream_limit() -> None:
    worker = load_module("invocation_worker")

    async def scenario() -> None:
        stream = asyncio.StreamReader(limit=worker.MAX_RPC_LINE_BYTES + 1)
        stream.feed_data(b"x" * (worker.MAX_READY_BYTES + 1) + b"\n")
        stream.feed_eof()
        with pytest.raises(worker.ProtocolError, match="exceeds its maximum"):
            await worker.read_bounded_line(stream, maximum=worker.MAX_READY_BYTES, timeout=0.1)

    asyncio.run(scenario())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_no_provider_worker_is_pid1_and_restores_invocation_baseline(tmp_path: Path) -> None:
    tools = load_module("tools")
    result = tools.run_no_provider_invocation_for_test(tmp_path)
    assert result["status"] == "completed"
    assert result["worker_pid_namespace_pid"] == 1
    assert result["cleanup_verified"] is True
    assert result["anchor_survives"] is True
    assert result["provider_calls"] == 0
    assert result["real_provider_credential_transmitted"] is False


def test_parent_loss_is_failure_and_never_provisional_success() -> None:
    worker = load_module("invocation_worker")
    assert worker.classify_control_bytes(b"") == worker.ControlState.PARENT_LOST
    assert worker.classify_control_bytes(b"S") == worker.ControlState.INTENTIONAL_STOP
    with pytest.raises(worker.ProtocolError):
        worker.classify_control_bytes(b"X")
    with pytest.raises(worker.ProtocolError):
        worker.classify_control_bytes(b"SS")
    assert worker.accept_success_after_cleanup(
        provisional_result={"status": "completed"},
        cleanup=worker.CleanupVerdict(clean=True, mount_absent=True, children_reaped=True),
        parent_lost=True,
    ) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_repeated_parent_cancellation_settles_worker_and_anchor(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    async def scenario() -> float:
        invocation = asyncio.create_task(
            tools._run_invocation(
                task="cancel lifecycle probe",
                workdir=tmp_path,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=tools._fake_relay_command(),
                prime_command=tools._fake_blocking_prime_command(),
                preflight=True,
            )
        )
        await asyncio.sleep(0.5)
        started = time.monotonic()
        invocation.cancel()
        await asyncio.sleep(0.05)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 10
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


def test_cancellation_does_not_erase_cleanup_failure(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")

    async def failing_finalizer(**_kwargs):
        await asyncio.sleep(0.05)
        raise tools.LifecycleCleanupError("cleanup sentinel")

    monkeypatch.setattr(tools, "_drive_invocation", failing_finalizer)

    async def scenario() -> None:
        invocation = asyncio.create_task(
            tools._run_invocation(
                task="x",
                workdir=tmp_path,
                runtime=tmp_path,
                route={"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
            )
        )
        await asyncio.sleep(0)
        invocation.cancel()
        with pytest.raises(tools.LifecycleCleanupError, match="cleanup sentinel"):
            await invocation

    asyncio.run(scenario())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_malformed_result_and_unresponsive_worker_hit_parent_hard_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    anchor = runtime.parent / "invocation-anchor"
    provision_test_anchor(tools, runtime.parent, anchor)
    malformed_worker = tmp_path / "malformed-worker.py"
    malformed_worker.write_text(
        "import os, time\n"
        "os.write(1, b'\\x00\\x00\\x00\\x02{}X')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "_INVOCATION_WORKER", malformed_worker)
    monkeypatch.setattr(tools, "_WORKER_CLEANUP_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(tools, "_PARENT_CLEANUP_MARGIN_SECONDS", 0.1)
    monkeypatch.setattr(tools, "_LAUNCHER_TERM_GRACE_SECONDS", 0.1)

    async def deadline_scenario() -> float:
        started = time.monotonic()
        with pytest.raises(tools.LifecycleCleanupError, match="terminal cleanup budget"):
            await tools._run_invocation(
                task="malformed result probe",
                workdir=tmp_path,
                runtime=runtime,
                route={"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
                timeout_seconds=0,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                preflight=False,
            )
        return time.monotonic() - started

    assert asyncio.run(deadline_scenario()) < 2.0
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_pid1_reaps_detached_double_fork_term_ignoring_descendant(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    pid_file = tmp_path / "detached-host-pid"
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    result, _diagnostic, _evidence = asyncio.run(
        tools._run_invocation(
            task="detached descendant probe",
            workdir=tmp_path,
            runtime=runtime,
            route=route,
            timeout_seconds=30,
            session_mode="ephemeral",
            session_directory=None,
            resume_path=None,
            relay_command=tools._fake_relay_command(),
            prime_command=tools._fake_detached_prime_command(pid_file),
            preflight=True,
        )
    )
    detached_host_pid = int(pid_file.read_text(encoding="ascii"))
    assert result["status"] == "completed"
    assert not Path(f"/proc/{detached_host_pid}").exists()
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_post_mount_anchor_replacement_preserves_foreign_sentinel(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    saved_anchor = runtime / "saved-anchor"
    ready_file = tmp_path / "prime-ready"
    continue_file = tmp_path / "continue"
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    async def scenario():
        invocation = asyncio.create_task(
            tools._run_invocation(
                task="replacement probe",
                workdir=tmp_path,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=tools._fake_relay_command(),
                prime_command=tools._fake_replacement_prime_command(ready_file, continue_file),
                preflight=True,
            )
        )
        for _ in range(200):
            if ready_file.exists():
                break
            await asyncio.sleep(0.02)
        assert ready_file.exists()
        anchor.rename(saved_anchor)
        anchor.mkdir(mode=0o700)
        sentinel = anchor / "foreign-sentinel"
        sentinel.write_text("KEEP", encoding="ascii")
        continue_file.write_text("go", encoding="ascii")
        result, _diagnostic, _evidence = await invocation
        return result, sentinel

    result, sentinel = asyncio.run(scenario())
    assert result["status"] == "completed"
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert saved_anchor.is_dir()
    assert list(saved_anchor.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_pre_worker_anchor_replacement_fails_closed_and_preserves_sentinel(tmp_path: Path, monkeypatch) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    saved_anchor = runtime / "saved-anchor"
    ready_file = tmp_path / "wrapper-ready"
    continue_file = tmp_path / "wrapper-continue"
    wrapper = tmp_path / "worker-wrapper.py"
    real_worker = Path(tools._INVOCATION_WORKER)
    wrapper.write_text(
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(ready_file)!r}).write_text('ready', encoding='ascii')\n"
        "deadline = time.monotonic() + 20\n"
        f"while not pathlib.Path({str(continue_file)!r}).exists():\n"
        "    if time.monotonic() >= deadline: raise SystemExit('continuation timed out')\n"
        "    time.sleep(0.02)\n"
        f"os.execv(sys.executable, [sys.executable, {str(real_worker)!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "_INVOCATION_WORKER", wrapper)
    monkeypatch.setenv("PRIME_MINION_ANCHOR_PATH", str(anchor))
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"}

    async def scenario():
        invocation = asyncio.create_task(
            tools._run_invocation(
                task="pre-worker replacement probe",
                workdir=tmp_path,
                runtime=runtime,
                route=route,
                timeout_seconds=30,
                session_mode="ephemeral",
                session_directory=None,
                resume_path=None,
                relay_command=tools._fake_relay_command(),
                prime_command=tools._fake_prime_command(),
                preflight=True,
            )
        )
        for _ in range(200):
            if ready_file.exists():
                break
            await asyncio.sleep(0.02)
        assert ready_file.exists()
        anchor.rename(saved_anchor)
        anchor.mkdir(mode=0o700)
        sentinel = anchor / "foreign-sentinel"
        sentinel.write_text("KEEP", encoding="ascii")
        continue_file.write_text("go", encoding="ascii")
        with pytest.raises(tools.MinionError, match="anchor identity|anchor path|changed"):
            await invocation
        return sentinel

    sentinel = asyncio.run(scenario())
    assert sentinel.read_text(encoding="ascii") == "KEEP"
    assert saved_anchor.is_dir()
    assert list(saved_anchor.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
@pytest.mark.parametrize("kill_stage", ["handlers", "mounted", "relay_ready", "prime_running"])
def test_hard_parent_death_closes_control_and_tears_down_exact_tree(tmp_path: Path, kill_stage: str) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    evidence_file = tmp_path / "startup-evidence.jsonl"
    test_module_dir = Path(__file__).resolve().parent
    runner = tmp_path / "hard-parent-runner.py"
    runner.write_text(
        "import asyncio, json, os, sys\n"
        f"sys.path.insert(0, {str(test_module_dir)!r})\n"
        "from test_invocation_worker import load_module\n"
        "tools = load_module('tools')\n"
        f"root = tools.Path({str(tmp_path)!r})\n"
        f"runtime = tools.Path({str(runtime)!r})\n"
        f"evidence_file = tools.Path({str(evidence_file)!r})\n"
        "def sink(record):\n"
        "    with evidence_file.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(record, separators=(',', ':')) + '\\n')\n"
        "        stream.flush()\n"
        "        os.fsync(stream.fileno())\n"
        "asyncio.run(tools._run_invocation(\n"
        "    task='hard parent probe', workdir=root, runtime=runtime,\n"
        "    route={'provider':'openai-codex','model':'gpt-5.6-luna','reasoning_effort':'max'},\n"
        "    timeout_seconds=30, session_mode='ephemeral', session_directory=None, resume_path=None,\n"
        "    relay_command=tools._fake_relay_command(), prime_command=tools._fake_blocking_prime_command(),\n"
        "    preflight=True, evidence_sink=sink))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PRIME_MINION_ANCHOR_PATH"] = str(anchor)
    parent = subprocess.Popen(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    launcher_pid: int | None = None
    worker_pid: int | None = None
    exact_identities: dict[int, dict[str, str | int]] = {}
    try:
        stage_seen = False
        for _ in range(500):
            launcher_children = proc_children(parent.pid)
            if launcher_children:
                launcher_pid = launcher_children[0]
                worker_children = proc_children(launcher_pid)
                if worker_children:
                    worker_pid = worker_children[0]
            if evidence_file.exists():
                records = [
                    json.loads(line)
                    for line in evidence_file.read_text(encoding="utf-8").splitlines()
                    if line
                ]
                stage_seen = any(record.get("stage") == kill_stage for record in records)
            if launcher_pid is not None and worker_pid is not None and stage_seen:
                break
            time.sleep(0.02)
        assert launcher_pid is not None
        assert worker_pid is not None
        assert stage_seen
        observed = [launcher_pid, worker_pid, *proc_tree(worker_pid)]
        exact_identities = {
            pid: identity
            for pid in observed
            if (identity := tools.process_identity(pid)) is not None
        }
        assert worker_pid in exact_identities
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(tools.process_identity(pid) != identity for pid, identity in exact_identities.items()):
                break
            time.sleep(0.05)
        assert all(tools.process_identity(pid) != identity for pid, identity in exact_identities.items())
        assert anchor.is_dir()
        assert list(anchor.iterdir()) == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if launcher_pid is not None and tools.process_identity(launcher_pid) == exact_identities.get(launcher_pid):
            os.kill(launcher_pid, signal.SIGKILL)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace lifecycle is Linux/WSL-only")
def test_hard_parent_death_before_worker_handlers_uses_direct_launcher_guard(tmp_path: Path) -> None:
    tools = load_module("tools")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    anchor = runtime / "invocation-anchor"
    provision_test_anchor(tools, runtime, anchor)
    stalled_worker = tmp_path / "stalled-worker.py"
    stalled_worker.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    test_module_dir = Path(__file__).resolve().parent
    runner = tmp_path / "pre-handler-parent.py"
    runner.write_text(
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(test_module_dir)!r})\n"
        "from test_invocation_worker import load_module\n"
        "tools = load_module('tools')\n"
        f"tools._INVOCATION_WORKER = tools.Path({str(stalled_worker)!r})\n"
        f"root = tools.Path({str(tmp_path)!r})\n"
        f"runtime = tools.Path({str(runtime)!r})\n"
        "asyncio.run(tools._run_invocation(\n"
        "    task='pre-handler hard-death probe', workdir=root, runtime=runtime,\n"
        "    route={'provider':'openai-codex','model':'gpt-5.6-luna','reasoning_effort':'max'},\n"
        "    timeout_seconds=30, session_mode='ephemeral', session_directory=None, resume_path=None,\n"
        "    relay_command=tools._fake_relay_command(), prime_command=tools._fake_blocking_prime_command(),\n"
        "    preflight=True))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PRIME_MINION_ANCHOR_PATH"] = str(anchor)
    parent = subprocess.Popen([sys.executable, str(runner)], cwd=tmp_path, env=env)
    launcher_pid: int | None = None
    worker_pid: int | None = None
    identities: dict[int, dict[str, str | int]] = {}
    try:
        for _ in range(300):
            launcher = proc_children(parent.pid)
            if launcher:
                launcher_pid = launcher[0]
                worker = proc_children(launcher_pid)
                if worker:
                    worker_pid = worker[0]
                    break
            time.sleep(0.02)
        assert launcher_pid is not None and worker_pid is not None
        identities = {
            pid: identity
            for pid in (launcher_pid, worker_pid)
            if (identity := tools.process_identity(pid)) is not None
        }
        assert len(identities) == 2
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(tools.process_identity(pid) != identity for pid, identity in identities.items()):
                break
            time.sleep(0.05)
        assert all(tools.process_identity(pid) != identity for pid, identity in identities.items())
        assert anchor.is_dir()
        assert list(anchor.iterdir()) == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if launcher_pid is not None and tools.process_identity(launcher_pid) == identities.get(launcher_pid):
            os.kill(launcher_pid, signal.SIGKILL)
