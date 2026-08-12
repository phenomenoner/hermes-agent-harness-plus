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

Restart Hermes Agent after changing MCP configuration. The tools include
`mcp_context_canvas_canvas_start`, `mcp_context_canvas_canvas_recent`, and
`mcp_context_canvas_canvas_record`. Use `canvas_recent` to recover a lost canvas
id; use `canvas_record` when evidence and its concise node belong in one atomic
update.

## 5. Keep the retired broad-capture plugin disabled

The Context Canvas Autopilot broad-capture experiment is archived. Do not copy
or enable it for a new installation. Use the explicit Canvas MCP tools above;
they preserve an intentional evidence map without duplicating every tool result.

If an older installation still lists `context-canvas-autopilot`, remove it from
`plugins.enabled`. A stale entry may remain temporarily while you clean up local
files, but keep it explicitly off:

```yaml
plugins:
  entries:
    context-canvas-autopilot:
      mode: off
      revision: 0.2.5-retired-broad-capture
```

The retained production code forces every requested mode to `off`, including
old active-mode values. Historical source replay and regression tests remain in
the repository for safety inspection only; their results never authorize live
capture. See the [Autopilot archive](technical/context-canvas-v2-reverse-shadow.md)
for the decision and retained checks.

For maintainers running that historical replay, `tool_root` remains a
**code-execution trust setting**, not a data directory. Use an absolute,
owner-controlled directory containing regular files
`context_canvas/__init__.py`, `context_canvas/core.py`, and
`context_canvas/snapshot.py`. Replay gives the selected root import precedence,
revalidates it before use, and rejects imported `context_canvas` modules from
another origin.

Validation requires POSIX effective-UID semantics. It rejects symbolic links,
group- or world-writable code paths, and replacement-capable ancestors. On a
platform without an equivalent ownership rule, historical activation must fail
closed. Never point replay at a download or shared writable directory.

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
