import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from context_canvas.core import CanvasStore


def test_start_creates_canonical_files_and_mermaid_projection(tmp_path):
    store = CanvasStore(root=tmp_path)

    result = store.start(goal="Ship Phase2 context canvas", session_id="phase2-test", title="Phase2")

    session_dir = tmp_path / "phase2-test"
    assert result["ok"] is True
    assert result["session_id"] == "phase2-test"
    assert (session_dir / "canvas.json").exists()
    assert (session_dir / "events.jsonl").exists()
    assert (session_dir / "canvas.mmd").exists()
    assert (session_dir / "refs").is_dir()

    canvas = json.loads((session_dir / "canvas.json").read_text())
    assert canvas["goal"] == "Ship Phase2 context canvas"
    assert canvas["title"] == "Phase2"
    assert canvas["nodes"] == []
    assert "graph TD" in (session_dir / "canvas.mmd").read_text()


def test_add_ref_and_node_keeps_evidence_chain(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Evidence chain", session_id="s1")

    ref = store.add_ref("s1", content="raw test output\nPASSED", label="pytest output", source="pytest")
    node = store.upsert_node(
        "s1",
        kind="verification",
        status="done",
        summary="Focused tests passed",
        refs=[ref["ref"]],
    )

    assert ref["ref"] == "refs/tc_001.md"
    assert (tmp_path / "s1" / ref["ref"]).read_text().startswith("# pytest output")
    assert node["node"]["id"] == "N001"
    assert node["node"]["refs"] == ["refs/tc_001.md"]

    canvas = json.loads((tmp_path / "s1" / "canvas.json").read_text())
    assert canvas["nodes"][0]["summary"] == "Focused tests passed"
    mermaid = (tmp_path / "s1" / "canvas.mmd").read_text()
    assert "N001" in mermaid
    assert "tc_001" in mermaid


def test_factual_done_node_requires_ref(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="No hallucinated facts", session_id="s1")

    with pytest.raises(ValueError, match="evidence ref"):
        store.upsert_node("s1", kind="finding", status="done", summary="Unbacked claim")

    planned = store.upsert_node("s1", kind="plan", status="planned", summary="Future action can be unbacked")
    assert planned["node"]["status"] == "planned"


def test_read_search_and_closeout_export(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Search closeout", session_id="s1")
    ref = store.add_ref("s1", content="MemPalace export candidate: stable decision", label="decision note")
    store.upsert_node("s1", kind="decision", status="done", summary="Use JSON as canonical source", refs=[ref["ref"]])

    read = store.read("s1", include_refs=False)
    assert read["canvas"]["goal"] == "Search closeout"

    hits = store.search("canonical", session_id="s1")
    assert hits["hits"]
    assert hits["hits"][0]["session_id"] == "s1"

    closeout = store.closeout("s1", write_ref=True)
    assert closeout["ok"] is True
    assert closeout["export_path"].endswith("closeout.md")
    text = (tmp_path / "s1" / "closeout.md").read_text()
    assert "MemPalace-ready" in text
    assert "Use JSON as canonical source" in text


def test_concurrent_evidence_updates_are_valid_and_unique(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Parallel evidence", session_id="parallel")
    workers = 16
    barrier = threading.Barrier(workers)

    def capture(index: int) -> tuple[str, str]:
        barrier.wait()
        ref = store.add_ref(
            "parallel",
            content=f"parallel evidence {index}",
            label=f"evidence {index}",
        )
        node = store.upsert_node(
            "parallel",
            kind="action",
            status="done",
            summary=f"Captured worker {index}",
            refs=[ref["ref"]],
        )
        return ref["ref"], node["node"]["id"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(capture, range(workers)))

    canvas = json.loads((tmp_path / "parallel" / "canvas.json").read_text())
    refs = [ref for ref, _ in results]
    node_ids = [node_id for _, node_id in results]
    assert len(canvas["nodes"]) == workers
    assert len(set(refs)) == workers
    assert len(set(node_ids)) == workers
    assert {ref for node in canvas["nodes"] for ref in node["refs"]} == set(refs)
    assert len(list((tmp_path / "parallel" / "refs").glob("tc_*.md"))) == workers


def test_search_skips_corrupt_canvas_but_still_searches_refs(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Recoverable search", session_id="corrupt")
    ref = store.add_ref(
        "corrupt",
        content="needle survives in the evidence ref",
        label="recoverable evidence",
    )
    canvas_path = tmp_path / "corrupt" / "canvas.json"
    canvas_path.write_text(canvas_path.read_text() + "}\n")

    result = store.search("needle")

    assert result["ok"] is True
    assert result["skipped_count"] == 1
    assert result["skipped_sessions"][0]["session_id"] == "corrupt"
    assert any(hit["id"] == ref["ref"] for hit in result["hits"])


def test_repeated_start_preserves_existing_canvas(tmp_path):
    store = CanvasStore(root=tmp_path)
    first = store.start(goal="Original goal", session_id="stable")
    ref = store.add_ref("stable", content="durable evidence")
    store.upsert_node(
        "stable",
        kind="action",
        status="done",
        summary="Durable node",
        refs=[ref["ref"]],
    )
    events_before = (tmp_path / "stable" / "events.jsonl").read_text()

    second = store.start(goal="Replacement goal", session_id="stable")

    assert first["created"] is True
    assert second["created"] is False
    assert second["canvas"]["goal"] == "Original goal"
    assert len(second["canvas"]["nodes"]) == 1
    assert (tmp_path / "stable" / "events.jsonl").read_text() == events_before


def test_generated_session_ids_are_collision_resistant(tmp_path):
    store = CanvasStore(root=tmp_path)
    first = store.start(goal="same goal")
    second = store.start(goal="same goal")

    assert first["session_id"] != second["session_id"]
    assert first["created"] is True
    assert second["created"] is True


def test_ref_ids_advance_past_gaps(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Ref gaps", session_id="gaps")
    first = store.add_ref("gaps", content="first")
    (tmp_path / "gaps" / "refs" / "tc_003.md").write_text("reserved evidence")

    next_ref = store.add_ref("gaps", content="next")

    assert first["ref"] == "refs/tc_001.md"
    assert next_ref["ref"] == "refs/tc_004.md"
    state = json.loads((tmp_path / "gaps" / "state.json").read_text())
    assert state["next_ref"] == 5


def test_search_skips_structurally_invalid_canvas_and_searches_refs(tmp_path):
    store = CanvasStore(root=tmp_path)
    store.start(goal="Structural search", session_id="structural")
    ref = store.add_ref("structural", content="structural needle survives")
    canvas_path = tmp_path / "structural" / "canvas.json"
    canvas = json.loads(canvas_path.read_text())
    canvas["nodes"] = None
    canvas_path.write_text(json.dumps(canvas))

    result = store.search("structural needle")

    assert result["skipped_count"] == 1
    assert result["skipped_sessions"][0]["session_id"] == "structural"
    assert any(hit["id"] == ref["ref"] for hit in result["hits"])


@pytest.mark.skipif(os.name != "posix", reason="cross-process locking is supported on POSIX/WSL")
def test_cross_process_start_and_updates_preserve_all_evidence(tmp_path):
    package_root = Path(__file__).resolve().parents[1] / "packages" / "context-canvas"
    workers = 8
    start_at = time.time() + 0.8
    code = """
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from context_canvas.core import CanvasStore
root = Path(sys.argv[2])
index = int(sys.argv[3])
start_at = float(sys.argv[4])
while time.time() < start_at:
    time.sleep(0.001)
store = CanvasStore(root=root)
store.start(goal='shared goal', session_id='shared')
ref = store.add_ref('shared', content=f'process evidence {index}')['ref']
store.upsert_node(
    'shared',
    kind='action',
    status='done',
    summary=f'Process {index}',
    refs=[ref],
)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(package_root), str(tmp_path), str(index), str(start_at)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(workers)
    ]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append((process.returncode, stdout, stderr))

    assert not failures
    canvas = json.loads((tmp_path / "shared" / "canvas.json").read_text())
    refs = list((tmp_path / "shared" / "refs").glob("tc_*.md"))
    events = [
        json.loads(line)
        for line in (tmp_path / "shared" / "events.jsonl").read_text().splitlines()
    ]
    assert len(canvas["nodes"]) == workers
    assert len({node["id"] for node in canvas["nodes"]}) == workers
    assert len(refs) == workers
    assert sum(event["event"] == "canvas_started" for event in events) == 1
    assert sum(event["event"] == "ref_added" for event in events) == workers
    assert sum(event["event"] == "node_added" for event in events) == workers
