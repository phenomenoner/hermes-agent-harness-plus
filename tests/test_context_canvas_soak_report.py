import base64
import hashlib
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = ROOT / "packages" / "context-canvas"
PLUGIN_PATH = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
REPORT_PATH = ROOT / "scripts" / "context_canvas_v2_soak_report.py"
REPLAY_PATH = ROOT / "scripts" / "context_canvas_v2_replay.py"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from context_canvas.snapshot import (  # type: ignore[import-not-found]  # noqa: E402
    PrivateJsonlLedger,
    SnapshotStore,
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def configure_plugin(plugin, tmp_path, monkeypatch, **overrides):
    canvas_root = tmp_path / "canvas"
    cache_root = tmp_path / "cache"
    metrics_root = tmp_path / "metrics"
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(canvas_root))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(TOOL_ROOT))
    plugin.reset_state_for_tests()
    config = {
        "mode": "v2_active_legacy_shadow",
        "revision": "report-test-r1",
        "cache_root": str(cache_root),
        "metrics_root": str(metrics_root),
        "retention_class": "test",
        "retention_days": 1,
        "max_semantic_refs": 12,
        "legacy_tool_threshold": 99,
        "legacy_large_result_chars": 1000,
        "legacy_max_ref_chars": 50000,
        "metrics_enabled": True,
        "require_hermes_redactor": True,
        "async_writes": False,
        "queue_maxsize": 64,
        "flush_timeout_seconds": 10,
    }
    config.update(overrides)
    plugin.set_test_config(config)
    monkeypatch.setattr(
        plugin,
        "_force_redact_text",
        lambda text, *, tool_name, required: (text, False, "hermes_force"),
    )
    return cache_root, metrics_root


def report_args(reporter, metrics_root, cache_root):
    return reporter.parser().parse_args(
        [
            "--metrics-root",
            str(metrics_root),
            "--cache-root",
            str(cache_root),
            "--min-hours",
            "0",
            "--min-events",
            "0",
            "--min-sessions",
            "0",
            "--max-p95-ms",
            "100000",
            "--max-p99-ms",
            "100000",
            "--max-persist-p95-ms",
            "100000",
            "--max-persist-p99-ms",
            "100000",
            "--max-queue-wait-p95-ms",
            "100000",
        ]
    )


def test_conflicting_duplicate_runtime_error_is_a_reporter_hard_failure(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_regression")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    event = {
        "tool_name": "read_file",
        "args": {"path": "same.py"},
        "session_id": "report-conflict",
        "tool_call_id": "stable-event",
        "status": "ok",
    }
    plugin.on_post_tool_call(result={"ok": True, "payload": "first"}, **event)
    plugin.on_post_tool_call(result={"ok": True, "payload": "second"}, **event)

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_regression")
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))

    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == 2
    assert rows[0]["active_capture_ok"] is True
    assert rows[1]["active_capture_ok"] is False
    assert rows[1]["active_error_type"] == "RuntimeError"
    assert report["verdict"] == "FAIL"
    assert report["sample"]["metric_rows"] == 2
    assert report["sample"]["unique_events"] == 1
    assert report["sample"]["duplicate_callbacks"] == 1
    assert report["sample"]["capture_attempts"] == 1
    assert report["sample"]["capture_successes"] == 1
    assert report["hard_gates"]["capture_failures"] == 1
    assert "capture_failures" in report["hard_gates"]["failures"]


def test_reporter_metric_schema_accepts_ordered_event_sequence(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_schema")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    for index in range(2):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"{index}.py"},
            result=f"result-{index}",
            session_id="report-sequence",
            tool_call_id=f"call-{index}",
            status="ok",
        )

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_schema")
    rows = PrivateJsonlLedger(metrics_root).read()
    assert [row["event_sequence"] for row in rows] == [1, 2]
    assert all(reporter.validate_metric(row) is None for row in rows)


def test_reporter_never_grants_product_or_rollout_authority(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_no_authority")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "fixture.txt"},
        result="historical safety replay",
        session_id="report-no-authority",
        tool_call_id="no-authority",
        status="ok",
    )
    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_no_authority")
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))

    assert report["product_authority"] == "none"
    assert report["decision"] == "historical_safety_evidence_only"


def test_raw_data_url_detector_requires_one_contiguous_data_url():
    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_data_url_detector")

    assert reporter.contains_persisted_data_url("data:image/gif;base64,YWJj") is True
    assert reporter.contains_persisted_data_url("data:image/gif;base64,%%%not-valid%%") is True
    assert (
        reporter.contains_persisted_data_url(
            "documentation mentions `data:` here and `;base64,` somewhere else"
        )
        is False
    )


def test_replay_data_url_detector_requires_one_contiguous_data_url(tmp_path):
    replay = load_module(REPLAY_PATH, "context_canvas_v2_replay_data_url_detector")
    cache_root = tmp_path / "cache"
    store = SnapshotStore(cache_root)

    separated = store.put_envelope(
        {
            "args": "documentation mentions `data:` here",
            "result": "and `;base64,` somewhere else",
        }
    )
    store.record_manifest(
        "separated-terms",
        {
            "event_id": "a" * 64,
            "object_sha256": separated["sha256"],
            "embedded_objects": [],
        },
    )
    assert replay.canary_violations(cache_root)["data_url_hits"] == 0

    contiguous = store.put_envelope(
        {"args": {}, "result": "data:image/gif;base64,YWJj"}
    )
    store.record_manifest(
        "contiguous-data-url",
        {
            "event_id": "b" * 64,
            "object_sha256": contiguous["sha256"],
            "embedded_objects": [],
        },
    )
    assert replay.canary_violations(cache_root)["data_url_hits"] == 1


def test_reporter_does_not_flag_separated_data_url_terms_in_snapshot_text(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_data_url_terms")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    plugin.on_post_tool_call(
        tool_name="session_search",
        args={"query": "data URL docs"},
        result="documentation mentions `data:` here and `;base64,` somewhere else",
        session_id="report-data-url-terms",
        tool_call_id="data-url-terms",
        status="ok",
    )

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_data_url_terms")
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))

    assert report["hard_gates"]["raw_data_url_text_objects"] == 0
    assert "raw_data_url_persisted" not in report["hard_gates"]["failures"]


def test_reporter_surfaces_removed_invalid_data_url_as_fidelity_loss(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_fidelity_loss")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    malformed = "data:image/gif;base64,%%%not-valid%%%"
    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": malformed},
        result={"ok": True},
        session_id="report-fidelity-loss",
        tool_call_id="fidelity-loss",
        status="ok",
    )

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_fidelity_loss")
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))

    assert report["hard_gates"]["externalization_errors"] == 0
    assert "externalization_errors" not in report["hard_gates"]["failures"]
    assert report["quality"]["invalid_data_urls_removed"] == 1
    assert "snapshot_fidelity_loss" in report["quality"]["holds"]


def test_binary_store_failure_remains_a_reporter_hard_failure(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_report_binary_store_failure")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch)
    _, snapshot_store_type, _, _ = plugin._components()

    def fail_binary_store(self, _raw):
        raise OSError("synthetic binary object-store failure")

    monkeypatch.setattr(snapshot_store_type, "put_binary", fail_binary_store)
    raw_url = "data:image/gif;base64,YWJj"
    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": raw_url},
        result={"ok": True},
        session_id="report-binary-store-failure",
        tool_call_id="binary-store-failure",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(
        next((cache_root / "sessions").glob("*/snapshots/sr_*.json"))
    )
    envelope_text = str(checked["envelope"])
    assert raw_url not in envelope_text
    assert ";base64," not in envelope_text
    assert checked["manifest"]["externalization_errors"] == 1

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_binary_store_failure")
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))

    assert report["verdict"] == "FAIL"
    assert report["hard_gates"]["externalization_errors"] == 1
    assert "externalization_errors" in report["hard_gates"]["failures"]
    assert report["hard_gates"]["raw_data_url_text_objects"] == 0


def test_externalized_binary_bytes_cross_threshold_and_legacy_cohort_accounting(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_binary_threshold")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch, legacy_tool_threshold=1)
    raw = b"".join(hashlib.sha256(f"context-canvas-binary-{index}".encode()).digest() for index in range(2048))
    url = "data:application/octet-stream;base64," + base64.b64encode(raw).decode("ascii")
    result = {"ok": True, "description": "x" * 8192}

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": url},
        result=result,
        session_id="binary-threshold",
        tool_call_id="binary-0",
        status="ok",
    )

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_binary_threshold")

    def no_change(text, **_kwargs):
        return text

    monkeypatch.setattr(reporter, "force_redactor", lambda: no_change)
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))
    rows = sorted(PrivateJsonlLedger(metrics_root).read(), key=lambda row: row["event_sequence"])
    assert all(reporter.validate_metric(row) is None for row in rows)
    assert [row["active_embedded_raw_bytes"] for row in rows] == [len(raw)]
    assert rows[0]["active_embedded_stored_bytes"] == len(raw)

    quality = report["quality"]
    expected_raw = sum(
        row["active_object_raw_bytes"] + row["active_embedded_raw_bytes"]
        for row in rows
        if not row["active_event_duplicate"]
    )
    expected_effective = sum(
        row["active_manifest_bytes"]
        + (0 if row["active_object_reused"] else row["active_object_stored_bytes"])
        + row["active_embedded_stored_bytes"]
        for row in rows
        if not row["active_event_duplicate"]
    )
    assert quality["active_raw_bytes"] == expected_raw
    assert quality["active_effective_bytes"] == expected_effective
    assert quality["legacy_cohort_active_bytes"] == expected_effective

    omitted_binary_raw = sum(row["active_embedded_raw_bytes"] for row in rows)
    omitted_binary_stored = sum(row["active_embedded_stored_bytes"] for row in rows)
    old_ratio = (quality["active_effective_bytes"] - omitted_binary_stored) / (
        quality["active_raw_bytes"] - omitted_binary_raw
    )
    assert old_ratio <= report["registered_thresholds"]["max_storage_ratio_to_raw"]
    assert quality["storage_ratio_to_raw"] > report["registered_thresholds"]["max_storage_ratio_to_raw"]
    assert "storage_ratio_to_raw" in quality["holds"]
    assert report["verdict"] == "HOLD"


def test_duplicate_callback_does_not_double_binary_capacity(tmp_path, monkeypatch):
    plugin = load_module(PLUGIN_PATH, "context_canvas_autopilot_binary_duplicate")
    cache_root, metrics_root = configure_plugin(plugin, tmp_path, monkeypatch, legacy_tool_threshold=1)
    raw = bytes((index * 41 + 23) % 256 for index in range(4096))
    url = "data:application/octet-stream;base64," + base64.b64encode(raw).decode("ascii")
    event = {
        "tool_name": "vision_analyze",
        "args": {"image_url": url},
        "result": {"ok": True},
        "session_id": "binary-duplicate",
        "tool_call_id": "stable-binary-event",
        "status": "ok",
    }
    plugin.on_post_tool_call(**event)
    plugin.on_post_tool_call(**event)

    reporter = load_module(REPORT_PATH, "context_canvas_v2_soak_report_binary_duplicate")

    def no_change(text, **_kwargs):
        return text

    monkeypatch.setattr(reporter, "force_redactor", lambda: no_change)
    report = reporter.collect(report_args(reporter, metrics_root, cache_root))
    rows = PrivateJsonlLedger(metrics_root).read()
    first = rows[0]
    assert report["sample"]["metric_rows"] == 2
    assert report["sample"]["unique_events"] == 1
    assert report["sample"]["duplicate_callbacks"] == 1
    assert rows[1]["active_event_duplicate"] is True
    assert rows[1]["active_embedded_stored_bytes"] == 0
    assert report["quality"]["active_raw_bytes"] == first["active_object_raw_bytes"] + first["active_embedded_raw_bytes"]
    assert report["quality"]["active_effective_bytes"] == (
        first["active_manifest_bytes"]
        + first["active_object_stored_bytes"]
        + first["active_embedded_stored_bytes"]
    )
