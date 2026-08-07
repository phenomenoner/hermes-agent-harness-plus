import base64
import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
TOOL_ROOT = ROOT / "packages" / "context-canvas"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore  # type: ignore[import-not-found]


def load_plugin():
    name = "context_canvas_autopilot_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def configure(plugin, tmp_path, monkeypatch, **overrides):
    canvas_root = tmp_path / "canvas"
    cache_root = tmp_path / "cache"
    metrics_root = tmp_path / "metrics"
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(canvas_root))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    plugin.reset_state_for_tests()
    config = {
        "mode": "v2_active_legacy_shadow",
        "revision": "test-r1",
        "cache_root": str(cache_root),
        "metrics_root": str(metrics_root),
        "retention_class": "test",
        "retention_days": 1,
        "max_semantic_refs": 12,
        "legacy_tool_threshold": 3,
        "legacy_large_result_chars": 1000,
        "legacy_max_ref_chars": 50000,
        "metrics_enabled": True,
        "require_hermes_redactor": False,
        "async_writes": False,
        "queue_maxsize": 64,
        "flush_timeout_seconds": 10,
    }
    config.update(overrides)
    plugin.set_test_config(config)
    return canvas_root, cache_root, metrics_root


def manifests(cache_root):
    return sorted((cache_root / "sessions").glob("*/snapshots/sr_*.json"))


def test_v2_caches_full_result_from_first_call_without_noisy_canvas(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "a.py"},
        result="full point-in-time result",
        session_id="s1",
        tool_call_id="call-1",
        status="ok",
    )

    paths = manifests(cache_root)
    assert len(paths) == 1
    store = SnapshotStore(cache_root)
    checked = store.validate_manifest(paths[0])
    assert checked["envelope"]["result"] == "full point-in-time result"
    assert checked["manifest"]["full_snapshot_is_sanitized"] is True
    assert not list(canvas_root.glob("auto-v2-*/canvas.json"))
    rows = PrivateJsonlLedger(metrics_root).read()
    assert rows[0]["active_capture_ok"] is True
    assert rows[0]["legacy_shadow_capture"] is False


def test_content_addressing_deduplicates_identical_envelopes(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    for index in range(2):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": "same.py"},
            result="same result",
            session_id="dedupe",
            tool_call_id=f"call-{index}",
            status="ok",
        )

    paths = manifests(cache_root)
    assert len(paths) == 2
    payloads = [json.loads(path.read_text()) for path in paths]
    assert payloads[0]["object_sha256"] == payloads[1]["object_sha256"]
    assert payloads[0]["object_reused"] is False
    assert payloads[1]["object_reused"] is True
    objects = list((cache_root / "objects" / "text" / "sha256").glob("*/*.json.zlib"))
    assert len(objects) == 1
    rows = PrivateJsonlLedger(metrics_root).read()
    assert rows[1]["active_object_reused"] is True


def test_duplicate_tool_call_id_is_idempotent_for_manifest_and_semantic_projection(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)
    event = {
        "tool_name": "patch",
        "args": {"path": "same.py"},
        "result": {"success": True},
        "session_id": "duplicate-event",
        "tool_call_id": "stable-call-id",
        "status": "ok",
    }

    plugin.on_post_tool_call(**event)
    plugin.on_post_tool_call(**event)

    assert len(manifests(cache_root)) == 1
    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == 2
    assert rows[0]["event_id"] == rows[1]["event_id"]
    assert rows[0]["active_event_duplicate"] is False
    assert rows[1]["active_event_duplicate"] is True
    canvas = json.loads(next(canvas_root.glob("auto-v2-duplicate-event-*/canvas.json")).read_text())
    assert len(canvas["nodes"]) == 1
    assert len(canvas["nodes"][0]["refs"]) == 1


def test_data_url_is_externalized_as_binary_object(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    raw = b"\x89PNG\r\n\x1a\n" + b"Z" * 512
    encoded = base64.b64encode(raw).decode("ascii")

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": f"data:image/png;base64,{encoded}"},
        result={"ok": True, "description": "fixture"},
        session_id="image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert ";base64," not in envelope_text
    embedded = checked["manifest"]["embedded_objects"]
    assert len(embedded) == 1
    assert SnapshotStore(cache_root).read_binary(embedded[0]["sha256"]) == raw


def test_short_data_url_is_externalized_as_binary_object(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": "data:image/gif;base64,YWJj"},
        result={"ok": True},
        session_id="short-image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert ";base64,YWJj" not in envelope_text
    embedded = checked["manifest"]["embedded_objects"]
    assert len(embedded) == 1
    assert checked["manifest"]["externalization_errors"] == 0
    assert SnapshotStore(cache_root).read_binary(embedded[0]["sha256"]) == b"abc"
    assert PrivateJsonlLedger(metrics_root).read()[0]["active_externalization_errors"] == 0


def test_embedded_binary_metrics_count_raw_bytes_and_first_write_storage_only(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=1)
    raw = bytes((index * 73 + index // 11 * 19 + 17) % 256 for index in range(4096))
    encoded = base64.b64encode(raw).decode("ascii")
    url = f"data:application/octet-stream;base64,{encoded}"

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"first": url, "second": url},
        result={"ok": True},
        session_id="binary-ledger",
        tool_call_id="binary-1",
        status="ok",
    )
    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"first": url},
        result={"ok": True},
        session_id="binary-ledger",
        tool_call_id="binary-2",
        status="ok",
    )

    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == 2
    assert rows[0]["active_embedded_raw_bytes"] == len(raw) * 2
    assert rows[0]["active_embedded_stored_bytes"] == len(raw)
    assert rows[1]["active_embedded_raw_bytes"] == len(raw)
    assert rows[1]["active_embedded_stored_bytes"] == 0

    checked = [SnapshotStore(cache_root).validate_manifest(path) for path in manifests(cache_root)]
    assert [[item["reused"] for item in entry["manifest"]["embedded_objects"]] for entry in checked] == [
        [False, True],
        [True],
    ]
    assert all(entry["manifest"]["embedded_objects"][0]["raw_bytes"] == len(raw) for entry in checked)


def test_malformed_data_url_is_removed_and_externalization_error_is_recorded(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)
    malformed = "data:image/gif;base64,%%%not-valid%%%"

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": malformed},
        result={"ok": True},
        session_id="invalid-image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert malformed not in envelope_text
    assert ";base64," not in envelope_text
    assert checked["manifest"]["embedded_objects"] == []
    assert checked["manifest"]["externalization_errors"] == 1
    assert PrivateJsonlLedger(metrics_root).read()[0]["active_externalization_errors"] == 1


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"exit_code": 1, "output": "failed"}, "nonzero_exit"),
        ({"success": False}, "reported_failure"),
        ({"ok": False}, "reported_failure"),
        ({"error": "tool failed"}, "error_field"),
    ],
)
def test_observer_ok_cannot_hide_payload_failure(result, reason):
    plugin = load_plugin()
    assert plugin._status_from_result(result, json.dumps(result), observer_status="ok") == ("error", reason)


def test_observer_error_type_remains_failure_even_with_ok_status():
    plugin = load_plugin()
    assert plugin._status_from_result(
        {"ok": True},
        '{"ok":true}',
        observer_status="ok",
        observer_error_type="tool_error",
    ) == ("error", "tool_error")


def test_observer_ok_nonzero_exit_promotes_failure_not_verification(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="terminal",
        args={"command": "python -m pytest -q"},
        result={"exit_code": 1, "output": "failed"},
        session_id="observer-ok-failure",
        tool_call_id="failed-call",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["status"] == "error"
    assert checked["manifest"]["status_reason"] == "nonzero_exit"
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_semantic_class"] == "failure"
    assert row["legacy_shadow_capture"] is False
    canvas = json.loads(next(canvas_root.glob("auto-v2-observer-ok-failure-*/canvas.json")).read_text())
    assert {node["id"] for node in canvas["nodes"]} == {"AUTO_V2_FAILURES"}


def test_registered_context_canvas_tool_is_active_excluded_but_legacy_bug_is_measured(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        legacy_tool_threshold=1,
    )

    plugin.on_post_tool_call(
        tool_name="mcp__context_canvas__canvas_add_ref",
        args={"session_id": "x"},
        result="self capture",
        session_id="self",
        status="ok",
    )

    assert manifests(cache_root) == []
    assert not canvas_root.exists()
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_excluded"] is True
    assert row["self_capture_excluded"] is True
    assert row["active_capture_attempted"] is False
    assert row["legacy_shadow_capture"] is True


def test_verification_failure_and_action_promote_three_bounded_nodes(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    events = [
        ("terminal", {"command": "python -m pytest -q"}, {"exit_code": 0, "output": "3 passed"}, "ok"),
        ("terminal", {"command": "python fail.py"}, {"exit_code": 1, "output": "failed"}, "error"),
        ("patch", {"path": "a.py"}, {"success": True}, "ok"),
    ]
    for index, (tool, args, result, status) in enumerate(events):
        plugin.on_post_tool_call(
            tool_name=tool,
            args=args,
            result=result,
            session_id="semantic",
            tool_call_id=f"call-{index}",
            status=status,
            error_type="fixture" if status == "error" else "",
        )

    assert len(manifests(cache_root)) == 3
    canvas_path = next(canvas_root.glob("auto-v2-semantic-*/canvas.json"))
    canvas = json.loads(canvas_path.read_text())
    assert {node["id"] for node in canvas["nodes"]} == {
        "AUTO_V2_VERIFICATIONS",
        "AUTO_V2_FAILURES",
        "AUTO_V2_ACTIONS",
    }
    assert {node["kind"] for node in canvas["nodes"]} == {"verification", "blocked", "action"}
    assert all(node["refs"] for node in canvas["nodes"])
    assert len(list((canvas_path.parent / "refs").glob("tc_*.md"))) == 3


def test_legacy_shadow_threshold_never_materializes_legacy_canvas(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=3)

    for index in range(4):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"{index}.py"},
            result="small",
            session_id="threshold",
            status="ok",
        )

    rows = PrivateJsonlLedger(metrics_root).read()
    assert [row["legacy_shadow_capture"] for row in rows] == [False, False, True, True]
    assert len(manifests(cache_root)) == 4
    assert not (canvas_root / "auto-threshold").exists()
    assert not list(canvas_root.glob("auto-v2-*/canvas.json"))


def test_v2_active_does_not_run_or_emit_legacy_evaluator(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        mode="v2_active",
        legacy_tool_threshold=1,
    )

    def forbidden_evaluator(**kwargs):
        raise AssertionError("v2_active must not run the stateful legacy evaluator")

    monkeypatch.setattr(plugin, "evaluate_legacy_event", forbidden_evaluator)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "active.py"},
        result="active v2",
        session_id="v2-only",
        tool_call_id="v2-call",
        status="ok",
    )

    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["mode"] == "v2_active"
    assert row["event_sequence"] == 1
    assert row["legacy_shadow_capture"] is False
    assert row["legacy_shadow_reason"] == "disabled"
    assert row["legacy_shadow_estimated_bytes"] == 0
    assert row["legacy_shadow_estimated_nodes"] == 0
    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["legacy_shadow_capture"] is False
    assert checked["manifest"]["legacy_shadow_reason"] == "disabled"


@pytest.mark.parametrize("mode", ["v2_active_legacy_shadow", "legacy_active_safe"])
def test_legacy_modes_keep_stateful_decision_contract(tmp_path, monkeypatch, mode):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        mode=mode,
        legacy_tool_threshold=1,
    )

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "legacy.py"},
        result="legacy contract",
        session_id="legacy-contract",
        tool_call_id="legacy-call",
        status="ok",
    )

    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["mode"] == mode
    assert row["event_sequence"] == 1
    assert row["legacy_shadow_capture"] is True
    assert row["legacy_shadow_reason"] == "tool_threshold:1"
    assert row["legacy_shadow_estimated_nodes"] == 1
    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["legacy_shadow_capture"] is True
    if mode == "legacy_active_safe":
        assert row["active_semantic_class"] == "action"
        assert canvas_root.exists()
    else:
        assert row["active_semantic_class"] == "none"
        assert not canvas_root.exists()


def _ordered_decisions(plugin, tmp_path, monkeypatch, *, async_writes):
    _, _, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        async_writes=async_writes,
        worker_count=8,
        legacy_tool_threshold=3,
    )
    for index in range(4):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"ordered-{index}.py"},
            result="small",
            session_id="ordered",
            tool_call_id=f"ordered-{index}",
            status="ok",
        )
    if async_writes:
        plugin.on_session_finalize(session_id="ordered")
    rows = PrivateJsonlLedger(metrics_root).read()
    return [
        (row["event_sequence"], row["legacy_shadow_capture"], row["legacy_shadow_reason"])
        for row in sorted(rows, key=lambda row: row["event_sequence"])
    ]


def test_ordered_legacy_stream_matches_sync_and_async_receive_order(tmp_path, monkeypatch):
    sync_plugin = load_plugin()
    sync = _ordered_decisions(sync_plugin, tmp_path / "sync", monkeypatch, async_writes=False)
    async_plugin = load_plugin()
    asynchronous = _ordered_decisions(async_plugin, tmp_path / "async", monkeypatch, async_writes=True)

    expected = [
        (1, False, "below_threshold:1"),
        (2, False, "below_threshold:2"),
        (3, True, "tool_threshold:3"),
        (4, True, "tool_threshold:4"),
    ]
    assert sync == expected
    assert asynchronous == expected


def test_concurrent_capture_keeps_unique_manifests_and_valid_metrics(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=99)
    workers = 20

    def capture(index):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"{index}.txt"},
            result=f"payload-{index}",
            session_id="parallel",
            tool_call_id=f"call-{index}",
            status="ok",
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(capture, range(workers)))

    paths = manifests(cache_root)
    assert len(paths) == workers
    assert len({path.name for path in paths}) == workers
    store = SnapshotStore(cache_root)
    assert all(store.validate_manifest(path)["ok"] for path in paths)
    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == workers
    assert all(row["active_capture_ok"] for row in rows)


def test_required_redactor_failure_is_fail_open_and_metricized(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        require_hermes_redactor=True,
    )

    def fail_redactor(*args, **kwargs):
        raise RuntimeError("fixture redactor unavailable")

    monkeypatch.setattr(plugin, "_force_redact_text", fail_redactor)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "secret.env"},
        result="secret fixture",
        session_id="redactor-fail",
        status="ok",
    )

    assert manifests(cache_root) == []
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_capture_attempted"] is True
    assert row["active_capture_ok"] is False
    assert row["active_error_type"] == "RuntimeError"


def test_redacted_full_snapshot_never_persists_raw_canary(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        require_hermes_redactor=True,
    )
    canary = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"

    def fixture_redactor(text, *, tool_name, required):
        redacted = text.replace(canary, "«redacted:sk-…»")
        return redacted, redacted != text, "hermes_force"

    monkeypatch.setattr(plugin, "_force_redact_text", fixture_redactor)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "secret.env"},
        result=f"API_KEY={canary}",
        session_id="secret",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    combined = json.dumps(checked["envelope"])
    assert canary not in combined
    assert "redacted" in combined
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_redaction_applied"] is True
    assert row["active_redactor_backend"] == "hermes_force"


def test_lifecycle_updates_only_captured_session(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "a.py"},
        result="data",
        session_id="life",
        status="ok",
    )
    plugin.on_session_end(session_id="life", completed=True, interrupted=False)
    lifecycle_path = next((cache_root / "sessions").glob("life-*/lifecycle.json"))
    assert json.loads(lifecycle_path.read_text())["lifecycle"] == "turn-ended"
    plugin.on_session_finalize(session_id="life")
    assert json.loads(lifecycle_path.read_text())["lifecycle"] == "closed"
    plugin.on_session_finalize(session_id="never-captured")
    assert not list((cache_root / "sessions").glob("never-captured-*"))


def test_async_hook_flushes_on_finalize_and_records_queue_latency(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        async_writes=True,
        queue_maxsize=16,
        flush_timeout_seconds=10,
    )

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "async.py"},
        result="async snapshot",
        session_id="async",
        status="ok",
    )
    plugin.on_session_finalize(session_id="async")

    assert len(manifests(cache_root)) == 1
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["async_write"] is True
    assert row["active_capture_ok"] is True
    assert row["hook_ms"] >= 0
    assert row["persist_ms"] > 0
    assert row["queue_wait_ms"] >= 0


def test_registers_all_required_hooks():
    plugin = load_plugin()

    class FakeContext:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    ctx = FakeContext()
    plugin.register(ctx)
    assert [name for name, _ in ctx.hooks] == ["post_tool_call", "on_session_end", "on_session_finalize"]
