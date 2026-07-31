from __future__ import annotations

import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
TOOL_ROOT = ROOT / "packages" / "context-canvas"


def load_plugin():
    spec = importlib.util.spec_from_file_location("context_canvas_autopilot_under_test", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reset_state_for_tests()
    return module


def test_autopilot_starts_canvas_after_tool_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", "3")
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", "10000")

    plugin = load_plugin()

    plugin.on_post_tool_call(tool_name="read_file", args={"path": "a.py"}, result="short", session_id="s1")
    plugin.on_post_tool_call(tool_name="search_files", args={"pattern": "x"}, result="short", session_id="s1")
    assert not (tmp_path / "canvas" / "auto-s1" / "canvas.json").exists()

    plugin.on_post_tool_call(tool_name="terminal", args={"command": "pytest"}, result="short", session_id="s1")

    canvas_path = tmp_path / "canvas" / "auto-s1" / "canvas.json"
    assert canvas_path.exists()
    canvas = json.loads(canvas_path.read_text())
    assert canvas["session_id"] == "auto-s1"
    assert canvas["nodes"][0]["kind"] == "action"
    assert "terminal" in canvas["nodes"][0]["summary"]
    assert (tmp_path / "canvas" / "auto-s1" / "refs" / "tc_001.md").exists()


def test_autopilot_captures_large_result_immediately_and_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", "99")
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", "20")
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_MAX_REF_CHARS", "40")

    plugin = load_plugin()
    plugin.on_post_tool_call(tool_name="terminal", args={"command": "python noisy.py"}, result="x" * 100, session_id="s2")

    ref = tmp_path / "canvas" / "auto-s2" / "refs" / "tc_001.md"
    assert ref.exists()
    text = ref.read_text()
    assert "[truncated to 40 chars" in text
    assert len(text) < 900


def test_autopilot_ignores_context_canvas_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", "1")

    plugin = load_plugin()
    plugin.on_post_tool_call(
        tool_name="mcp_context_canvas_canvas_add_ref",
        args={"session_id": "x", "content": "y"},
        result="large" * 1000,
        session_id="s3",
    )

    assert not (tmp_path / "canvas").exists()


def test_skill_view_large_result_does_not_start_canvas(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", "99")
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", "20")

    plugin = load_plugin()
    plugin.on_post_tool_call(
        tool_name="skill_view",
        args={"name": "hermes-agent"},
        result={
            "success": True,
            "name": "hermes-agent",
            "description": "Configure, extend, or contribute to Hermes Agent.",
            "content": "x" * 1000,
        },
        session_id="skills-no-start",
    )

    assert not (tmp_path / "canvas" / "auto-skills-no-start" / "canvas.json").exists()


def test_skill_view_on_active_canvas_stores_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", "1")
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", "20")

    plugin = load_plugin()
    plugin.on_post_tool_call(tool_name="terminal", args={"command": "pytest"}, result="short", session_id="skills-active")
    plugin.on_post_tool_call(
        tool_name="skill_view",
        args={"name": "context-canvas-memory"},
        result={
            "success": True,
            "name": "context-canvas-memory",
            "description": "Task Canvas rules.",
            "tags": ["context-management"],
            "related_skills": ["hermes-agent"],
            "content": "VERY LARGE SKILL BODY " * 200,
        },
        session_id="skills-active",
    )

    canvas_path = tmp_path / "canvas" / "auto-skills-active" / "canvas.json"
    canvas = json.loads(canvas_path.read_text())
    assert canvas["nodes"][-1]["summary"] == "Loaded relevant skill: context-canvas-memory"

    ref = tmp_path / "canvas" / "auto-skills-active" / "refs" / "tc_002.md"
    text = ref.read_text()
    assert "captured_mode" in text
    assert "metadata_only" in text
    assert "context-canvas-memory" in text
    assert "VERY LARGE SKILL BODY" not in text


def test_canvas_initialization_is_serialized_for_parallel_hooks(monkeypatch):
    plugin = load_plugin()

    class FakeStore:
        def __init__(self):
            self.exists = False
            self.start_calls = 0
            self.guard = threading.Lock()

        def read(self, _canvas_id):
            with self.guard:
                if not self.exists:
                    raise FileNotFoundError
                return {"ok": True}

        def start(self, **_kwargs):
            time.sleep(0.02)
            with self.guard:
                self.start_calls += 1
                self.exists = True
            return {"ok": True}

    store = FakeStore()
    monkeypatch.setattr(plugin, "_store", lambda: store)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda _: plugin._ensure_canvas("auto-parallel", session_id="parallel"),
                range(12),
            )
        )

    assert store.start_calls == 1
    assert "auto-parallel" in plugin._active_canvases
