#!/usr/bin/env python3
"""Validate and score a Context Canvas v2 reverse-shadow soak.

The report is deliberately content-free: it aggregates bounded metrics and
validates snapshot/object integrity without printing tool arguments, results,
secret matches, or binary payloads.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()
TOOL_ROOT = Path(os.getenv("HERMES_CONTEXT_CANVAS_TOOL", HERMES_HOME / "context-canvas-tool")).expanduser()
SOURCE_TOOL_ROOT = Path(__file__).resolve().parents[1] / "packages" / "context-canvas"
for candidate in (TOOL_ROOT, SOURCE_TOOL_ROOT, HERMES_HOME / "hermes-agent"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore  # type: ignore[import-not-found]  # noqa: E402

METRIC_FIELDS = {
    "schema_version",
    "revision",
    "ts",
    "mode",
    "session_hash",
    "event_id",
    "event_sequence",
    "identity_unknown",
    "event_identity_unknown",
    "tool_name",
    "result_chars",
    "active_excluded",
    "self_capture_excluded",
    "active_capture_attempted",
    "active_capture_ok",
    "active_error_type",
    "active_event_duplicate",
    "active_snapshot_id",
    "active_object_raw_bytes",
    "active_object_stored_bytes",
    "active_embedded_raw_bytes",
    "active_embedded_stored_bytes",
    "active_manifest_bytes",
    "active_object_reused",
    "active_redaction_applied",
    "active_redactor_backend",
    "active_embedded_objects",
    "active_externalization_errors",
    "active_semantic_class",
    "active_semantic_promoted",
    "active_semantic_ref",
    "active_semantic_error_type",
    "legacy_shadow_capture",
    "legacy_shadow_reason",
    "legacy_shadow_estimated_bytes",
    "legacy_shadow_estimated_nodes",
    "replacement_applied",
    "async_write",
    "queue_depth",
    "queue_wait_ms",
    "persist_ms",
    "hook_ms",
}
BOOL_FIELDS = {
    "active_excluded",
    "self_capture_excluded",
    "active_capture_attempted",
    "active_capture_ok",
    "active_event_duplicate",
    "active_object_reused",
    "active_redaction_applied",
    "active_semantic_promoted",
    "legacy_shadow_capture",
    "replacement_applied",
    "async_write",
    "identity_unknown",
    "event_identity_unknown",
}
INT_FIELDS = {
    "schema_version",
    "result_chars",
    "event_sequence",
    "active_object_raw_bytes",
    "active_object_stored_bytes",
    "active_embedded_raw_bytes",
    "active_embedded_stored_bytes",
    "active_manifest_bytes",
    "active_embedded_objects",
    "active_externalization_errors",
    "legacy_shadow_estimated_bytes",
    "legacy_shadow_estimated_nodes",
    "queue_depth",
}
FLOAT_FIELDS = {"hook_ms", "queue_wait_ms", "persist_ms"}
STRING_FIELDS = METRIC_FIELDS - BOOL_FIELDS - INT_FIELDS - FLOAT_FIELDS


def parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 3)
    weighted = ordered[low] * (high - rank) + ordered[high] * (rank - low)
    return round(weighted, 3)


def validate_metric(row: dict[str, Any]) -> str | None:
    if set(row) != METRIC_FIELDS:
        return "field_set"
    for field in BOOL_FIELDS:
        if type(row[field]) is not bool:
            return f"type:{field}"
    for field in INT_FIELDS:
        if type(row[field]) is not int or row[field] < 0:
            return f"type:{field}"
    for field in STRING_FIELDS:
        if type(row[field]) is not str:
            return f"type:{field}"
    for field in FLOAT_FIELDS:
        if type(row[field]) not in {int, float} or isinstance(row[field], bool) or row[field] < 0:
            return f"type:{field}"
    if row["schema_version"] != 2:
        return "schema_version"
    if row["replacement_applied"]:
        return "replacement_applied"
    if not re.fullmatch(r"[0-9a-f]{64}", row["event_id"]):
        return "event_id"
    try:
        parse_ts(row["ts"])
    except Exception:
        return "timestamp"
    return None


def force_redactor() -> Any | None:
    try:
        from agent.redact import redact_sensitive_text  # type: ignore[import-not-found]

        return redact_sensitive_text
    except Exception:
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def collect(args: argparse.Namespace) -> dict[str, Any]:
    metrics_root = Path(args.metrics_root).expanduser()
    cache_root = Path(args.cache_root).expanduser()
    rows = PrivateJsonlLedger(metrics_root).read()
    cutoff = None
    if args.since_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    selected: list[dict[str, Any]] = []
    invalid_metric_rows = 0
    metric_error_classes: dict[str, int] = {}
    for row in rows:
        error = validate_metric(row)
        if error:
            invalid_metric_rows += 1
            metric_error_classes[error] = metric_error_classes.get(error, 0) + 1
            continue
        if cutoff and parse_ts(row["ts"]) < cutoff:
            continue
        selected.append(row)

    raw_selected_count = len(selected)
    raw_selected = list(selected)
    unique_by_event: dict[str, dict[str, Any]] = {}
    for row in raw_selected:
        unique_by_event.setdefault(row["event_id"], row)
    duplicate_callbacks = raw_selected_count - len(unique_by_event)
    selected = list(unique_by_event.values())

    # Quality/capacity calculations use unique events, but hard gates must see
    # every valid selected metric row. A conflicting duplicate is deliberately
    # retained here so its RuntimeError cannot be hidden by event-id dedupe.
    hard_attempts = [row for row in raw_selected if row["active_capture_attempted"]]
    hard_successes = [row for row in hard_attempts if row["active_capture_ok"]]
    capture_failures = len(hard_attempts) - len(hard_successes)
    self_capture_violations = sum(
        bool(row["self_capture_excluded"] and (row["active_capture_attempted"] or row["active_capture_ok"]))
        for row in raw_selected
    )
    replacements = sum(bool(row["replacement_applied"]) for row in raw_selected)
    redactor_backend_violations = sum(
        bool(row["active_capture_ok"] and row["active_redactor_backend"] != "hermes_force")
        for row in raw_selected
    )
    externalization_errors = sum(row["active_externalization_errors"] for row in raw_selected)
    semantic_errors = sum(bool(row["active_semantic_error_type"]) for row in raw_selected)

    eligible = [row for row in selected if not row["active_excluded"]]
    attempts = [row for row in selected if row["active_capture_attempted"]]
    successes = [row for row in attempts if row["active_capture_ok"]]
    unknown_identity_events = sum(
        bool(row["identity_unknown"] or row["event_identity_unknown"]) for row in selected
    )
    semantic_promotions = sum(bool(row["active_semantic_promoted"]) for row in selected)
    semantic_nodes = len(
        {
            (row["session_hash"], row["active_semantic_class"])
            for row in selected
            if row["active_semantic_promoted"] and row["active_semantic_class"] != "none"
        }
    )
    legacy_nodes = sum(row["legacy_shadow_estimated_nodes"] for row in selected)
    semantic_reduction = (1.0 - semantic_nodes / legacy_nodes) if legacy_nodes else 0.0
    physical_successes = [row for row in successes if not row["active_event_duplicate"]]
    effective_active_bytes = sum(
        row["active_manifest_bytes"]
        + (0 if row["active_object_reused"] else row["active_object_stored_bytes"])
        + row["active_embedded_stored_bytes"]
        for row in physical_successes
    )
    active_raw_bytes = sum(
        row["active_object_raw_bytes"] + row["active_embedded_raw_bytes"] for row in physical_successes
    )
    storage_ratio_to_raw = effective_active_bytes / active_raw_bytes if active_raw_bytes else 0.0
    legacy_bytes = sum(row["legacy_shadow_estimated_bytes"] for row in selected)
    storage_reduction = (1.0 - effective_active_bytes / legacy_bytes) if legacy_bytes else 0.0
    legacy_cohort = [row for row in physical_successes if row["legacy_shadow_capture"]]
    legacy_cohort_active_bytes = sum(
        row["active_manifest_bytes"]
        + (0 if row["active_object_reused"] else row["active_object_stored_bytes"])
        + row["active_embedded_stored_bytes"]
        for row in legacy_cohort
    )
    legacy_cohort_bytes = sum(row["legacy_shadow_estimated_bytes"] for row in legacy_cohort)
    legacy_cohort_storage_reduction = (
        1.0 - legacy_cohort_active_bytes / legacy_cohort_bytes if legacy_cohort_bytes else 0.0
    )
    coverage = len(successes) / len(attempts) if attempts else 0.0
    hook_values = [float(row["hook_ms"]) for row in selected]
    persist_values = [float(row["persist_ms"]) for row in selected]
    queue_wait_values = [float(row["queue_wait_ms"]) for row in selected]
    max_queue_depth = max((row["queue_depth"] for row in selected), default=0)

    manifests = sorted((cache_root / "sessions").glob("*/snapshots/sr_*.json")) if cache_root.exists() else []
    if args.max_manifests and len(manifests) > args.max_manifests:
        manifests = manifests[-args.max_manifests :]
    snapshot_store = SnapshotStore(cache_root)
    integrity_errors = 0
    raw_data_url_text_objects = 0
    persisted_redactor_hits = 0
    manifests_checked = 0
    manifest_event_ids: set[str] = set()
    redactor = force_redactor()
    for path in manifests:
        try:
            checked = snapshot_store.validate_manifest(path)
            manifest_event_ids.add(str(checked["manifest"].get("event_id", "")))
            envelope = checked["envelope"]
            args_text = str(envelope.get("args", ""))
            result_text = str(envelope.get("result", ""))
            combined = args_text + "\n" + result_text
            if "data:" in combined and ";base64," in combined:
                raw_data_url_text_objects += 1
            if redactor is not None:
                file_read = str(checked["manifest"].get("tool_name", "")) in {"read_file", "search_files"}
                redacted_args = redactor(
                    args_text,
                    force=True,
                    file_read=file_read,
                    redact_url_credentials=True,
                )
                redacted_result = redactor(
                    result_text,
                    force=True,
                    file_read=file_read,
                    redact_url_credentials=True,
                )
                if redacted_args != args_text or redacted_result != result_text:
                    persisted_redactor_hits += 1
            manifests_checked += 1
        except Exception:
            integrity_errors += 1

    pointer_errors = 0
    committed_event_ids: set[str] = set()
    for path in sorted((cache_root / "sessions").glob("*/events/*.json")) if cache_root.exists() else []:
        try:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("invalid event pointer type")
            pointer = json.loads(path.read_text(encoding="utf-8"))
            event_id = str(pointer.get("event_id", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", event_id) or event_id != path.stem:
                raise ValueError("event pointer identity mismatch")
            committed_event_ids.add(event_id)
        except Exception:
            pointer_errors += 1
    expected_event_ids = {row["event_id"] for row in successes}
    manifest_join_errors = 0
    if not args.max_manifests:
        manifest_join_errors = len(expected_event_ids ^ manifest_event_ids) + len(expected_event_ids ^ committed_event_ids)

    timestamps = [parse_ts(row["ts"]) for row in selected]
    observed_hours = 0.0
    if len(timestamps) >= 2:
        observed_hours = round((max(timestamps) - min(timestamps)).total_seconds() / 3600, 3)

    hard_failures: list[str] = []
    if invalid_metric_rows:
        hard_failures.append("invalid_metric_rows")
    if capture_failures:
        hard_failures.append("capture_failures")
    if self_capture_violations:
        hard_failures.append("self_capture_violation")
    if replacements:
        hard_failures.append("replacement_applied")
    if redactor_backend_violations:
        hard_failures.append("redactor_backend")
    if externalization_errors:
        hard_failures.append("externalization_errors")
    if semantic_errors:
        hard_failures.append("semantic_promotion_errors")
    if integrity_errors:
        hard_failures.append("snapshot_integrity")
    if pointer_errors:
        hard_failures.append("event_pointer_integrity")
    if manifest_join_errors:
        hard_failures.append("event_manifest_join")
    if raw_data_url_text_objects:
        hard_failures.append("raw_data_url_persisted")
    if persisted_redactor_hits:
        hard_failures.append("persisted_redactor_hits")

    enough_duration = observed_hours >= args.min_hours
    enough_events = len(eligible) >= args.min_events
    enough_sessions = len({row["session_hash"] for row in eligible}) >= args.min_sessions
    quality_holds: list[str] = []
    if not enough_duration:
        quality_holds.append("insufficient_duration")
    if not enough_events:
        quality_holds.append("insufficient_events")
    if not enough_sessions:
        quality_holds.append("insufficient_sessions")
    if attempts and coverage < 0.999:
        quality_holds.append("capture_coverage")
    if unknown_identity_events:
        quality_holds.append("unknown_event_identity")
    p95 = percentile(hook_values, 0.95)
    p99 = percentile(hook_values, 0.99)
    if p95 > args.max_p95_ms:
        quality_holds.append("hook_p95")
    if p99 > args.max_p99_ms:
        quality_holds.append("hook_p99")
    persist_p95 = percentile(persist_values, 0.95)
    persist_p99 = percentile(persist_values, 0.99)
    queue_wait_p95 = percentile(queue_wait_values, 0.95)
    if persist_p95 > args.max_persist_p95_ms:
        quality_holds.append("persist_p95")
    if persist_p99 > args.max_persist_p99_ms:
        quality_holds.append("persist_p99")
    if queue_wait_p95 > args.max_queue_wait_p95_ms:
        quality_holds.append("queue_wait_p95")
    if max_queue_depth > args.max_queue_depth:
        quality_holds.append("queue_depth")
    if legacy_nodes and semantic_reduction < args.min_semantic_reduction:
        quality_holds.append("semantic_reduction")
    if active_raw_bytes and storage_ratio_to_raw > args.max_storage_ratio_to_raw:
        quality_holds.append("storage_ratio_to_raw")
    if legacy_cohort_bytes and legacy_cohort_storage_reduction < args.min_legacy_cohort_storage_reduction:
        quality_holds.append("legacy_cohort_storage_reduction")
    if redactor is None:
        quality_holds.append("redactor_audit_unavailable")

    if hard_failures:
        verdict = "FAIL"
        decision = "rollback_or_disable_v2"
    elif quality_holds:
        verdict = "HOLD"
        decision = "continue_soak_or_optimize"
    else:
        verdict = "PASS"
        decision = "retire_legacy_shadow"

    return {
        "schema_version": 1,
        "verdict": verdict,
        "decision": decision,
        "window": {
            "since_hours": args.since_hours,
            "observed_hours": observed_hours,
            "first_ts": min(timestamps).isoformat() if timestamps else None,
            "last_ts": max(timestamps).isoformat() if timestamps else None,
        },
        "sample": {
            "metric_rows": raw_selected_count,
            "unique_events": len(selected),
            "duplicate_callbacks": duplicate_callbacks,
            "eligible_events": len(eligible),
            "capture_attempts": len(attempts),
            "capture_successes": len(successes),
            "sessions": len({row["session_hash"] for row in eligible}),
            "manifests_checked": manifests_checked,
            "manifests_sampled": bool(args.max_manifests),
        },
        "hard_gates": {
            "failures": hard_failures,
            "invalid_metric_rows": invalid_metric_rows,
            "metric_error_classes": metric_error_classes,
            "capture_failures": capture_failures,
            "self_capture_violations": self_capture_violations,
            "replacement_applied": replacements,
            "redactor_backend_violations": redactor_backend_violations,
            "externalization_errors": externalization_errors,
            "semantic_promotion_errors": semantic_errors,
            "snapshot_integrity_errors": integrity_errors,
            "event_pointer_errors": pointer_errors,
            "event_manifest_join_errors": manifest_join_errors,
            "raw_data_url_text_objects": raw_data_url_text_objects,
            "persisted_redactor_hits": persisted_redactor_hits,
            "redactor_audit_available": redactor is not None,
        },
        "quality": {
            "holds": quality_holds,
            "unknown_identity_events": unknown_identity_events,
            "capture_coverage": round(coverage, 6),
            "hook_ms": {
                "p50": percentile(hook_values, 0.50),
                "p95": p95,
                "p99": p99,
                "max": round(max(hook_values), 3) if hook_values else 0.0,
            },
            "persist_ms": {
                "p50": percentile(persist_values, 0.50),
                "p95": persist_p95,
                "p99": persist_p99,
                "max": round(max(persist_values), 3) if persist_values else 0.0,
            },
            "queue_wait_ms": {
                "p50": percentile(queue_wait_values, 0.50),
                "p95": queue_wait_p95,
                "p99": percentile(queue_wait_values, 0.99),
                "max": round(max(queue_wait_values), 3) if queue_wait_values else 0.0,
            },
            "max_queue_depth": max_queue_depth,
            "active_semantic_promotions": semantic_promotions,
            "active_semantic_nodes": semantic_nodes,
            "legacy_shadow_estimated_nodes": legacy_nodes,
            "semantic_node_reduction": round(semantic_reduction, 6),
            "active_effective_bytes": effective_active_bytes,
            "active_raw_bytes": active_raw_bytes,
            "storage_ratio_to_raw": round(storage_ratio_to_raw, 6),
            "legacy_shadow_estimated_bytes": legacy_bytes,
            "overall_storage_reduction_vs_legacy": round(storage_reduction, 6),
            "legacy_cohort_active_bytes": legacy_cohort_active_bytes,
            "legacy_cohort_estimated_bytes": legacy_cohort_bytes,
            "legacy_cohort_storage_reduction": round(legacy_cohort_storage_reduction, 6),
        },
        "registered_thresholds": {
            "min_hours": args.min_hours,
            "min_events": args.min_events,
            "min_sessions": args.min_sessions,
            "max_p95_ms": args.max_p95_ms,
            "max_p99_ms": args.max_p99_ms,
            "max_persist_p95_ms": args.max_persist_p95_ms,
            "max_persist_p99_ms": args.max_persist_p99_ms,
            "max_queue_wait_p95_ms": args.max_queue_wait_p95_ms,
            "max_queue_depth": args.max_queue_depth,
            "min_semantic_reduction": args.min_semantic_reduction,
            "max_storage_ratio_to_raw": args.max_storage_ratio_to_raw,
            "min_legacy_cohort_storage_reduction": args.min_legacy_cohort_storage_reduction,
        },
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--metrics-root", default=str(HERMES_HOME / "context-canvas-soak" / "v2-active-legacy-shadow"))
    p.add_argument("--cache-root", default=str(HERMES_HOME / "context-canvas-cache-v2"))
    p.add_argument("--since-hours", type=float, default=0.0)
    p.add_argument("--max-manifests", type=int, default=0)
    p.add_argument("--min-hours", type=float, default=48.0)
    p.add_argument("--min-events", type=int, default=100)
    p.add_argument("--min-sessions", type=int, default=5)
    p.add_argument("--max-p95-ms", type=float, default=5.0)
    p.add_argument("--max-p99-ms", type=float, default=20.0)
    p.add_argument("--max-persist-p95-ms", type=float, default=500.0)
    p.add_argument("--max-persist-p99-ms", type=float, default=2000.0)
    p.add_argument("--max-queue-wait-p95-ms", type=float, default=2000.0)
    p.add_argument("--max-queue-depth", type=int, default=192)
    p.add_argument("--min-semantic-reduction", type=float, default=0.70)
    p.add_argument("--max-storage-ratio-to-raw", type=float, default=0.80)
    p.add_argument("--min-legacy-cohort-storage-reduction", type=float, default=0.25)
    p.add_argument("--output")
    p.add_argument("--watchdog", action="store_true", help="Print only bounded hard-failure alerts; healthy/HOLD is silent")
    return p


def main() -> int:
    args = parser().parse_args()
    report = collect(args)
    if args.output:
        atomic_write_json(Path(args.output).expanduser(), report)
    if args.watchdog:
        failures = report["hard_gates"]["failures"]
        if failures:
            print(
                "Context Canvas v2 soak FAIL: "
                + ",".join(failures[:8])
                + f"; rows={report['sample']['metric_rows']} sessions={report['sample']['sessions']}"
            )
        return 0
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
