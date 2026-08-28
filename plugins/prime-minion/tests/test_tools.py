from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "prime_minion_testpkg"


def load_modules():
    for key in [name for name in sys.modules if name == PACKAGE or name.startswith(f"{PACKAGE}.")]:
        sys.modules.pop(key, None)
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


def load_tools():
    return load_modules()[1]


def test_route_validation_fails_closed_before_starting_bridge(tmp_path, monkeypatch) -> None:
    tools = load_tools()
    called = False

    async def forbidden_start():
        nonlocal called
        called = True
        raise AssertionError("bridge must not start")

    monkeypatch.setattr(tools, "_start_bridge", forbidden_start)
    result = asyncio.run(
        tools.delegate_minion(
            {
                "task": "noop",
                "workdir": str(tmp_path),
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "minimal",
            }
        )
    )
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "unsupported reasoning_effort" in payload["error"]
    assert called is False


def test_child_environment_strips_provider_secrets(monkeypatch, tmp_path) -> None:
    tools = load_tools()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("SOME_ACCESS_TOKEN", "must-not-cross")
    monkeypatch.setenv("SAFE_PROJECT_VALUE", "preserved")
    synthetic = tools._synthetic_codex_bearer()
    env = tools._child_environment(
        "http://127.0.0.1:32123/v1", tmp_path, synthetic
    )
    assert "OPENAI_API_KEY" not in env
    assert "SOME_ACCESS_TOKEN" not in env
    assert env["SAFE_PROJECT_VALUE"] == "preserved"
    assert env["HERMES_MINION_PROXY_BASE_URL"] == "http://127.0.0.1:32123/v1"
    assert env["HERMES_MINION_PROXY_API_KEY"] == synthetic
    assert synthetic.count(".") == 2
    assert "must-not-cross" not in synthetic
    assert tools._synthetic_codex_bearer() != synthetic
    assert env["PRIME_AGENT_TELEMETRY"] == "0"


def test_result_reduction_keeps_final_text_and_usage_small() -> None:
    tools = load_tools()
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "first"}],
            "usage": {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1},
        },
        {"role": "toolResult", "content": [{"type": "text", "text": "large tool output"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "final "},
                {"type": "text", "text": "answer"},
            ],
            "usage": {"input": 20, "output": 7, "cacheRead": 3, "cacheWrite": 0},
        },
    ]
    assert tools._assistant_text(messages) == "final answer"
    assert tools._usage(messages) == {
        "input": 30,
        "output": 12,
        "cache_read": 5,
        "cache_write": 1,
        "total": 48,
    }


def test_effort_mapping_matches_formal_contract() -> None:
    tools = load_tools()
    assert tools._EFFORT_TO_PRIME == {
        "none": "off",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }
    assert "minimal" not in tools._EFFORTS


def _install_fake_runtime(monkeypatch, tools, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "prime-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(tools, "_runtime_commit", lambda _runtime: tools._PINNED_PRIME_COMMIT)

    async def empty_stderr() -> str:
        return ""

    async def start_bridge():
        return (
            None,
            "http://127.0.0.1:32123/v1",
            asyncio.create_task(empty_stderr()),
            "test-synthetic-bearer",
        )

    monkeypatch.setattr(tools, "_start_bridge", start_bridge)


def test_resumable_session_create_then_resume_uses_same_transcript(tmp_path, monkeypatch) -> None:
    sessions, tools = load_modules()
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    _install_fake_runtime(monkeypatch, tools, tmp_path)
    seen_resume_paths: list[Path | None] = []

    async def fake_run_rpc(**kwargs):
        session_directory = kwargs["session_directory"]
        resume_path = kwargs["resume_path"]
        assert isinstance(session_directory, Path)
        seen_resume_paths.append(resume_path)
        transcript = session_directory / "prime-session.jsonl"
        if resume_path is None:
            transcript.write_text('{"type":"session","id":"prime-1"}\n', encoding="utf-8")
        else:
            assert resume_path == transcript
            transcript.write_text(transcript.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
        return (
            {
                "prompt_accepted": True,
                "event_count": 2,
                "tool_calls": [],
                "result": "ok",
                "usage": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0, "total": 2},
                "duration_seconds": 0.01,
                "effective_route": kwargs["requested_route"],
                "prime_session_id": "prime-1",
                "session_file": str(transcript),
            },
            "",
        )

    monkeypatch.setattr(tools, "_run_rpc", fake_run_rpc)
    request = {
        "task": "phase one",
        "workdir": str(tmp_path),
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "session_mode": "resumable",
    }
    first = json.loads(asyncio.run(tools.delegate_minion(request)))
    assert first["status"] == "completed"
    assert first["generation"] == 1
    assert first["session_state"] == "IDLE"
    assert first["session_id"].startswith("minion_")
    assert "session_file" not in first

    second_request = {**request, "task": "phase two", "session_id": first["session_id"]}
    second = json.loads(asyncio.run(tools.delegate_minion(second_request)))
    assert second["status"] == "completed"
    assert second["session_id"] == first["session_id"]
    assert second["generation"] == 2
    assert seen_resume_paths[0] is None
    assert seen_resume_paths[1] is not None

    manifest = sessions.load_manifest(first["session_id"])
    assert manifest["state"] == "IDLE"
    assert [turn["status"] for turn in manifest["turns"]] == ["COMPLETED", "COMPLETED"]
    assert manifest["session_file"] == "transcript/prime-session.jsonl"


def test_resume_rejects_workdir_drift_before_bridge(tmp_path, monkeypatch) -> None:
    _, tools = load_modules()
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    _install_fake_runtime(monkeypatch, tools, tmp_path)

    async def fake_run_rpc(**kwargs):
        transcript = kwargs["session_directory"] / "session.jsonl"
        transcript.write_text('{}\n', encoding="utf-8")
        return (
            {
                "prompt_accepted": True,
                "event_count": 1,
                "tool_calls": [],
                "result": "ok",
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
                "duration_seconds": 0.01,
                "effective_route": kwargs["requested_route"],
                "prime_session_id": "prime-1",
                "session_file": str(transcript),
            },
            "",
        )

    monkeypatch.setattr(tools, "_run_rpc", fake_run_rpc)
    first = json.loads(
        asyncio.run(
            tools.delegate_minion(
                {
                    "task": "phase one",
                    "workdir": str(tmp_path),
                    "session_mode": "resumable",
                }
            )
        )
    )
    other = tmp_path / "other"
    other.mkdir()
    bridge_calls = 0

    async def forbidden_bridge():
        nonlocal bridge_calls
        bridge_calls += 1
        raise AssertionError("bridge must not start")

    monkeypatch.setattr(tools, "_start_bridge", forbidden_bridge)
    second = json.loads(
        asyncio.run(
            tools.delegate_minion(
                {
                    "task": "phase two",
                    "workdir": str(other),
                    "session_id": first["session_id"],
                }
            )
        )
    )
    assert second["status"] == "error"
    assert "workdir mismatch" in second["error"]
    assert bridge_calls == 0


def test_failed_turn_is_interrupted_and_never_replayed_automatically(tmp_path, monkeypatch) -> None:
    sessions, tools = load_modules()
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    _install_fake_runtime(monkeypatch, tools, tmp_path)
    calls = 0

    async def fail_once(**_kwargs):
        nonlocal calls
        calls += 1
        raise tools.MinionError("simulated process loss")

    monkeypatch.setattr(tools, "_run_rpc", fail_once)
    payload = json.loads(
        asyncio.run(
            tools.delegate_minion(
                {
                    "task": "unsafe mutation",
                    "workdir": str(tmp_path),
                    "session_mode": "resumable",
                }
            )
        )
    )
    assert payload["status"] == "error"
    assert payload["session_state"] == "INTERRUPTED"
    assert calls == 1
    manifest = sessions.load_manifest(payload["session_id"])
    assert manifest["turns"][-1]["status"] == "INTERRUPTED"
    assert "simulated process loss" in manifest["turns"][-1]["error"]


def test_session_lease_rejects_a_second_live_owner(tmp_path, monkeypatch) -> None:
    sessions, _ = load_modules()
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="a" * 40)
    with sessions.session_lease(manifest["session_id"]):
        with pytest.raises(sessions.SessionBusyError, match="already active"):
            with sessions.session_lease(manifest["session_id"]):
                pass


def test_status_and_close_do_not_expose_transcript_path(tmp_path, monkeypatch) -> None:
    sessions, tools = load_modules()
    monkeypatch.setenv("PRIME_MINION_STATE_DIR", str(tmp_path / "state"))
    manifest = sessions.create_manifest(workdir=tmp_path, prime_commit="b" * 40)
    sessions.begin_turn(
        manifest,
        {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
    )
    sessions.record_interrupted(manifest, "/private/internal/transcript/path")
    status = json.loads(
        asyncio.run(tools.minion_session_status({"session_id": manifest["session_id"]}))
    )
    assert status["status"] == "ok"
    assert "session_file" not in status["session"]
    assert "error" not in status["session"]["last_turn"]
    assert "/private/internal" not in json.dumps(status)

    closed = json.loads(
        asyncio.run(tools.close_minion_session({"session_id": manifest["session_id"]}))
    )
    assert closed["status"] == "closed"
    assert closed["session"]["state"] == "CLOSED"
    assert sessions.load_manifest(manifest["session_id"])["state"] == "CLOSED"


def test_registers_delegate_status_and_close_tools_as_async() -> None:
    tools = load_tools()
    registered: list[dict] = []

    class Context:
        def register_tool(self, **kwargs):
            registered.append(kwargs)

    tools.register_tools(Context())
    assert [item["name"] for item in registered] == [
        "delegate_minion",
        "minion_session_status",
        "close_minion_session",
    ]
    assert all(item.get("is_async") is True for item in registered)
