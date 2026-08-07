import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore  # type: ignore[import-not-found]


def test_snapshot_store_roundtrips_and_deduplicates(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    envelope = {"schema_version": 2, "args": "{}", "result": "point-in-time"}

    first = store.put_envelope(envelope)
    second = store.put_envelope(envelope)

    assert first["sha256"] == second["sha256"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert store.read_envelope(first["sha256"]) == envelope
    assert stat.S_IMODE(os.lstat(first["object_path"]).st_mode) == 0o600


def test_binary_object_roundtrip(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    payload = b"\x00\x01binary fixture\xff"
    recorded = store.put_binary(payload)

    assert store.read_binary(recorded["sha256"]) == payload
    assert recorded["raw_bytes"] == len(payload)


def test_concurrent_manifest_allocation_is_unique_and_valid(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    obj = store.put_envelope({"schema_version": 2, "result": "shared"})
    workers = 24

    def record(index):
        return store.record_manifest(
            "shared-session",
            {
                "event_id": hashlib.sha256(f"event-{index}".encode()).hexdigest(),
                "revision": "test",
                "tool_name": "read_file",
                "object_sha256": obj["sha256"],
                "object_relpath": obj["object_relpath"],
                "embedded_objects": [],
                "index": index,
            },
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(record, range(workers)))

    ids = {result["snapshot_id"] for result in results}
    assert len(ids) == workers
    assert len(list((tmp_path / "cache" / "sessions").glob("shared-session-*/snapshots/sr_*.json"))) == workers
    assert all(store.validate_manifest(result["manifest_path"])["ok"] for result in results)


def test_manifest_validation_detects_tampered_object(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    obj = store.put_envelope({"schema_version": 2, "result": "original"})
    manifest = store.record_manifest(
        "tamper",
        {
            "event_id": hashlib.sha256(b"tamper-event").hexdigest(),
            "revision": "test",
            "tool_name": "terminal",
            "object_sha256": obj["sha256"],
            "object_relpath": obj["object_relpath"],
            "embedded_objects": [],
        },
    )
    object_path = tmp_path / "cache" / obj["object_relpath"]
    object_path.write_bytes(b"not-zlib")

    with pytest.raises(Exception):
        store.validate_manifest(manifest["manifest_path"])


def test_duplicate_event_id_is_idempotent_and_conflict_is_rejected(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    first_object = store.put_envelope({"schema_version": 2, "result": "same"})
    event_id = hashlib.sha256(b"stable-event").hexdigest()
    manifest = {
        "event_id": event_id,
        "revision": "test",
        "tool_name": "read_file",
        "object_sha256": first_object["sha256"],
        "object_relpath": first_object["object_relpath"],
        "embedded_objects": [],
    }

    first = store.record_manifest("idempotent", manifest)
    second = store.record_manifest("idempotent", manifest)

    assert first["duplicate_event"] is False
    assert second["duplicate_event"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(list((tmp_path / "cache" / "sessions").glob("idempotent-*/snapshots/sr_*.json"))) == 1

    conflicting = store.put_envelope({"schema_version": 2, "result": "different"})
    with pytest.raises(RuntimeError, match="conflicting payload"):
        store.record_manifest(
            "idempotent",
            {**manifest, "object_sha256": conflicting["sha256"], "object_relpath": conflicting["object_relpath"]},
        )


def test_session_storage_components_do_not_collide(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    store.update_session_state("a/b", {"lifecycle": "one"})
    store.update_session_state("a-b", {"lifecycle": "two"})

    sessions = list((tmp_path / "cache" / "sessions").iterdir())
    assert len(sessions) == 2
    assert {json.loads((path / "lifecycle.json").read_text())["lifecycle"] for path in sessions} == {"one", "two"}


def test_private_jsonl_ledger_concurrent_append_and_read(tmp_path):
    ledger = PrivateJsonlLedger(tmp_path / "metrics")
    workers = 32

    def append(index):
        ledger.append({"index": index, "ok": True})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(append, range(workers)))

    records = ledger.read()
    assert len(records) == workers
    assert {row["index"] for row in records} == set(range(workers))
    assert stat.S_IMODE(os.lstat(ledger.path).st_mode) == 0o600
    assert all(json.loads(line)["ok"] for line in ledger.path.read_text().splitlines())


def test_lifecycle_state_is_atomic_and_private(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    first = store.update_session_state("life", {"lifecycle": "turn-ended"})
    second = store.update_session_state("life", {"lifecycle": "closed"})

    assert first["lifecycle"] == "turn-ended"
    assert second["lifecycle"] == "closed"
    path = next((tmp_path / "cache" / "sessions").glob("life-*/lifecycle.json"))
    assert json.loads(path.read_text())["lifecycle"] == "closed"
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
