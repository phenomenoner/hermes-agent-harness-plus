import asyncio
import json
import os

from context_canvas import mcp_server


def test_mcp_tool_wrappers_use_configured_canvas_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path))

    started = json.loads(mcp_server.canvas_start(goal="MCP smoke", session_id="mcp-s1"))
    assert started["ok"] is True

    ref = json.loads(mcp_server.canvas_add_ref("mcp-s1", content="mcp raw evidence", label="mcp"))
    node = json.loads(
        mcp_server.canvas_upsert_node(
            "mcp-s1",
            kind="verification",
            status="done",
            summary="MCP wrapper can write canvas",
            refs=[ref["ref"]],
        )
    )
    assert node["node"]["id"] == "N001"

    read = json.loads(mcp_server.canvas_read("mcp-s1"))
    assert "graph TD" in read["mermaid"]

    searched = json.loads(mcp_server.canvas_search("wrapper", session_id="mcp-s1"))
    assert searched["hits"]

    closeout = json.loads(mcp_server.canvas_closeout("mcp-s1"))
    assert closeout["export_path"].endswith("closeout.md")


def test_mcp_recent_recovers_ids_and_node_schema_exposes_enums(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path))
    json.loads(mcp_server.canvas_start(goal="Recover this canvas", session_id="recoverable"))

    recent = json.loads(mcp_server.canvas_recent(query="recover", limit=5))
    assert [row["session_id"] for row in recent["sessions"]] == ["recoverable"]

    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    schema = tools["canvas_upsert_node"].inputSchema
    assert "gap" in schema["properties"]["kind"]["enum"]
    assert "done" in schema["properties"]["status"]["enum"]
    assert "canvas_recent" in tools


def test_mcp_record_atomically_adds_evidence_and_node(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(tmp_path))
    json.loads(mcp_server.canvas_start(goal="One-call evidence", session_id="atomic"))

    recorded = json.loads(
        mcp_server.canvas_record(
            "atomic",
            content="focused tests: 4 passed",
            summary="Focused tests passed",
            label="pytest",
            source="pytest -q",
            ref_kind="verification",
            node_kind="verification",
            node_status="done",
        )
    )

    assert recorded["ref"] == "refs/tc_001.md"
    assert recorded["node"]["refs"] == ["refs/tc_001.md"]
    tools = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "canvas_record" in tools
