from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from context_canvas.core import CanvasStore  # noqa: E402

mcp = FastMCP("context_canvas")


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _store() -> CanvasStore:
    return CanvasStore()


@mcp.tool()
def canvas_start(goal: str, session_id: str | None = None, title: str | None = None) -> str:
    """Start an evidence-backed Task Canvas under ~/.hermes/context-canvas.

    Use for long-running debugging/research/code-review tasks. The returned
    session_id is used by all other canvas_* tools.
    """
    try:
        return _json(_store().start(goal=goal, session_id=session_id, title=title))
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


@mcp.tool()
def canvas_add_ref(session_id: str, content: str, label: str = "evidence", source: str = "", kind: str = "evidence") -> str:
    """Offload raw/verifiable evidence into refs/tc_NNN.md for a canvas."""
    try:
        return _json(_store().add_ref(session_id, content=content, label=label, source=source, kind=kind))
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


@mcp.tool()
def canvas_upsert_node(
    session_id: str,
    kind: str,
    status: str,
    summary: str,
    refs: list[str] | None = None,
    depends_on: list[str] | None = None,
    node_id: str | None = None,
) -> str:
    """Add/update a concise canvas node; factual done/verify nodes require evidence refs."""
    try:
        return _json(
            _store().upsert_node(
                session_id,
                node_id=node_id,
                kind=kind,
                status=status,
                summary=summary,
                refs=refs or [],
                depends_on=depends_on or [],
            )
        )
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


@mcp.tool()
def canvas_read(session_id: str, include_refs: bool = False) -> str:
    """Read canonical canvas JSON plus derived Mermaid projection."""
    try:
        return _json(_store().read(session_id, include_refs=include_refs))
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


@mcp.tool()
def canvas_search(query: str, session_id: str | None = None, limit: int = 10) -> str:
    """Search canvas nodes and local evidence refs by substring."""
    try:
        return _json(_store().search(query, session_id=session_id, limit=limit))
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


@mcp.tool()
def canvas_closeout(session_id: str, write_ref: bool = True) -> str:
    """Produce a MemPalace-ready closeout export and triage summary for a canvas."""
    try:
        return _json(_store().closeout(session_id, write_ref=write_ref))
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


if __name__ == "__main__":
    mcp.run()
