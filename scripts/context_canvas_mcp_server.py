#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.22.0"]
# ///
"""Context Canvas MCP sidecar for Hermes Agent.

Run this script from a clone of hermes-agent-harness-plus, or set
HERMES_CONTEXT_CANVAS_TOOL to the context-canvas package directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TOOL_ROOT = Path(os.getenv("HERMES_CONTEXT_CANVAS_TOOL", Path(__file__).resolve().parents[1] / "packages" / "context-canvas")).expanduser()
sys.path.insert(0, str(TOOL_ROOT))

from context_canvas.mcp_server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()
