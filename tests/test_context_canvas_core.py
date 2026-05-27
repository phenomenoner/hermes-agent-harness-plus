import json
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
