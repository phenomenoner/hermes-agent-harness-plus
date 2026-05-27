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
