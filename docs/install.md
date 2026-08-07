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

Autopilot v2 is a lightweight functional plugin: it keeps full sanitized
point-in-time tool results in a private, compressed, content-addressed cache.
The semantic Canvas stays small: only failures, successful verification
commands, and state-changing actions are promoted. The plugin never edits
conversation history, never changes a tool result, and fails open if persistence
is unavailable.

```bash
mkdir -p ~/.hermes/plugins
cp -R plugins/context-canvas-autopilot ~/.hermes/plugins/
```

Then enable and configure the plugin in Hermes `config.yaml`:

```yaml
plugins:
  enabled:
    - context-canvas-autopilot
  entries:
    context-canvas-autopilot:
      mode: v2_active_legacy_shadow
      revision: 0.2.3-reverse-shadow-r4
      cache_root: ~/.hermes/context-canvas-cache-v2
      metrics_root: ~/.hermes/context-canvas-soak/v2-active-legacy-shadow
      retention_class: ephemeral-cache
      retention_days: 30
      max_semantic_refs: 12
      legacy_tool_threshold: 5
      legacy_large_result_chars: 6000
      legacy_max_ref_chars: 50000
      metrics_enabled: true
      require_hermes_redactor: true
```

`v2_active_legacy_shadow` makes v2 the real persistence path while evaluating
the old v1 policy on the same event. The shadow writes only content-free
comparison metrics — never duplicate payloads, refs, or nodes. See the
[reverse-shadow design](technical/context-canvas-v2-reverse-shadow.md) for the
registered soak gates and rollback modes. Its lightweight live-soak defaults are
48 hours, 100 eligible events across 5 sessions, at least 70% semantic-node
reduction, and at most 80% active storage/raw ratio; hard functional/integrity
gates remain zero. Source replay is candidate evidence only, not live retirement
evidence, so keep the legacy shadow enabled until the live soak passes.

The old `HERMES_CONTEXT_CANVAS_*` behavior variables are compatibility
fallbacks only. `HERMES_CONTEXT_CANVAS_TOOL` may still point at an installed
Context Canvas package when the plugin is copied separately from this repo.

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

For scheduled checks, keep successful runs quiet and alert only on failures. The
watchdog validates that expected collections exist, are green, have points, and
use the expected vector configuration:

```bash
python3 scripts/qdrant_recall_health_watchdog.py
QDRANT_WATCHDOG_VERBOSE=1 python3 scripts/qdrant_recall_health_watchdog.py
```

If Qdrant is normally a local Docker container, you can put a bounded recovery
step before heavier repair work. It first checks the Qdrant HTTP endpoint, then
uses Docker to start the named container, and tries one restart only if health
does not return. On WSL it can optionally start Docker Desktop when the Windows
paths are available:

```bash
QDRANT_CONTAINER=qdrant-hermes \
  scripts/qdrant_bounded_start_restart.sh
```

Useful tuning knobs:

```bash
export QDRANT_DOCKER_CLI_TIMEOUT=8
export QDRANT_DOCKER_START_WAIT_ATTEMPTS=90
export QDRANT_READY_WAIT_ATTEMPTS=90
export QDRANT_RESTART_WAIT_ATTEMPTS=60
```

If Qdrant runs in Docker, add the restart calibration wrapper to your scheduler.
It records the container `StartedAt` timestamp and reruns the watchdog after a
container restart before trusting recall again:

```bash
QDRANT_CONTAINER=qdrant-hermes \
  scripts/qdrant_restart_calibration.sh
```

Optional: set `QDRANT_REPAIR_CMD` to chain your own local repair script after a
failed calibration. The wrapper checks Qdrant over HTTP before reading Docker
metadata, and Docker inspection is time-bounded so a slow Docker CLI does not
create false scheduler failures.

When a watchdog alert reports only `MISSING <collection>`, prefer rerunning the
ingest or refresh job for that collection instead of rebuilding every corpus.
Save full storage repair for unreachable Qdrant, unhealthy collections, or
vector configuration drift.

## 7. Install the public skills

Once this repo is reachable from GitHub, a Hermes Agent user can install a skill
from its raw URL, or copy the skill directory into `~/.hermes/skills/`.

```bash
mkdir -p ~/.hermes/skills/harness-plus
cp -R skills/context-canvas-memory ~/.hermes/skills/harness-plus/
cp -R skills/qdrant-recall-sidecar ~/.hermes/skills/harness-plus/
```

Start a fresh Hermes session after adding skills.
