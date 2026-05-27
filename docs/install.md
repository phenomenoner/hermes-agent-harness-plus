# Install and Enable

This guide assumes you already have Hermes Agent installed and working. The
harness is optional: copy only the pieces you want.

## 1. Clone the toolbox

```bash
git clone https://github.com/phenomenoner/hermes-agent-harness-plus.git
cd hermes-agent-harness-plus
```

If you use the Qdrant helper scripts, install [`uv`](https://docs.astral.sh/uv/) or run the scripts with a Python environment that already has `fastembed`, `PyYAML`, and `mcp` installed.

## 2. Test the Task Canvas package

```bash
python -m pip install -e '.[mcp]' pytest
python -m pytest -q
```

## 3. Use Context Canvas as a local CLI

```bash
PYTHONPATH=packages/context-canvas python -m context_canvas.cli start   --session-id demo   --goal "Keep evidence for a long Hermes task"
```

By default, canvases are written under `~/.hermes/context-canvas`. Override that
with `HERMES_CONTEXT_CANVAS_HOME` when you want a different storage location.

## 4. Add the Context Canvas MCP server to Hermes Agent

Add this to your Hermes Agent `config.yaml`, replacing the repo path with your
local clone path:

```yaml
mcp_servers:
  context_canvas:
    command: "python"
    args: ["/absolute/path/to/hermes-agent-harness-plus/scripts/context_canvas_mcp_server.py"]
    env:
      HERMES_CONTEXT_CANVAS_HOME: "/home/you/.hermes/context-canvas"
```

Restart Hermes Agent after changing MCP configuration. The tools will appear as
`mcp_context_canvas_canvas_start`, `mcp_context_canvas_canvas_add_ref`, and so on.

## 5. Enable the optional autopilot plugin

The autopilot plugin observes completed tool calls and writes evidence after a
long or large tool-use pattern. It is deliberately conservative: it never edits
conversation history, never mutates compression, and fails open if it cannot
write a canvas.

```bash
mkdir -p ~/.hermes/plugins
cp -R plugins/context-canvas-autopilot ~/.hermes/plugins/
```

Then add the plugin key to your Hermes config:

```yaml
plugins:
  enabled:
    - context-canvas-autopilot
```

Optional tuning:

```bash
export HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD=5
export HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS=6000
export HERMES_CONTEXT_CANVAS_MAX_REF_CHARS=50000
export HERMES_CONTEXT_CANVAS_TOOL=/absolute/path/to/hermes-agent-harness-plus/packages/context-canvas
```

Restart Hermes Agent so plugin hooks are loaded.

## 6. Add Qdrant recall helpers

Start a local Qdrant service however you prefer. One simple Docker example:

```bash
docker run -d --name qdrant-hermes -p 127.0.0.1:6333:6333 qdrant/qdrant:v1.17.1
```

Add the MCP sidecar:

```yaml
mcp_servers:
  qdrant:
    command: "uv"
    args: ["run", "--script", "/absolute/path/to/hermes-agent-harness-plus/scripts/qdrant_mcp_server.py"]
    env:
      QDRANT_URL: "http://127.0.0.1:6333"
      QDRANT_SEARCH_ALL_COLLECTIONS: "hermes_skills_multilingual_v1,hermes_sessions_recent_multilingual_v1"
```

Index skills or recent sessions only after you have reviewed what will be
indexed:

```bash
uv run --script scripts/qdrant_ingest_hermes_skills.py --dry-run
uv run --script scripts/qdrant_ingest_hermes_sessions.py --days 14 --max-sessions 150 --dry-run
```

Without `uv`, install dependencies first and run the scripts with Python:

```bash
python -m pip install fastembed PyYAML mcp
python scripts/qdrant_ingest_hermes_skills.py --dry-run
```

Remove `--dry-run` when the preview is safe.

## 7. Install the public skills

Once this repo is reachable from GitHub, a Hermes Agent user can install a skill
from its raw URL, or copy the skill directory into `~/.hermes/skills/`.

```bash
mkdir -p ~/.hermes/skills/harness-plus
cp -R skills/context-canvas-memory ~/.hermes/skills/harness-plus/
cp -R skills/qdrant-recall-sidecar ~/.hermes/skills/harness-plus/
```

Start a fresh Hermes session after adding skills.
