# Component Catalog

## Context Canvas package

- Path: `packages/context-canvas/`
- Purpose: keep concise task state in JSON while large evidence lives in
  `refs/tc_NNN.md` files.
- Good for: long debugging sessions, code review, research, multi-tool tasks.
- Not for: storing secrets, private memories, or permanent project knowledge.

## Context Canvas MCP sidecar

- Path: `scripts/context_canvas_mcp_server.py`
- Purpose: expose the Task Canvas package to Hermes Agent through native MCP.
- Tools: `canvas_start`, `canvas_add_ref`, `canvas_upsert_node`, `canvas_read`,
  `canvas_search`, `canvas_closeout`.

## Context Canvas Autopilot plugin

- Path: `plugins/context-canvas-autopilot/`
- Purpose: write evidence automatically after the task becomes tool-heavy or a
  tool result is large.
- Safety posture: fail-open, metadata-only for loaded skills, excludes memory and
  canvas tools to avoid recursion.

## Qdrant scripts

- Path: `scripts/qdrant_*.py`, `scripts/qdrant_refresh_sessions_index.sh`, and
  `scripts/qdrant_restart_calibration.sh`
- Purpose: index selected Hermes skills and recent user/assistant session text
  into local Qdrant collections, then check that recall is still live after
  scheduled refreshes or container restarts.
- Default collections: `hermes_skills_multilingual_v1` and
  `hermes_sessions_recent_multilingual_v1`.
- Data handling: session indexing skips system prompts and tool outputs by
  default, redacts common secret patterns, and supports dry-run previews.
- Health checks: the watchdog is quiet when collections are green; the restart
  calibration wrapper records Docker `StartedAt` and reruns the watchdog after a
  container restart before trusting recall.

## Skills

- Path: `skills/context-canvas-memory/SKILL.md`
- Path: `skills/qdrant-recall-sidecar/SKILL.md`
- Purpose: teach Hermes Agent when to use the harness and what pitfalls to avoid.

## Ops rules

- Path: `ops-rules/public-release-checklist.md`
- Purpose: keep shared tools reusable and safe before a commit becomes public.
