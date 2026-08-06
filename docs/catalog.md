# Component Catalog

## Context Canvas package

- Path: `packages/context-canvas/`
- Purpose: keep concise task state in JSON while large evidence lives in
  `refs/tc_NNN.md` files.
- Good for: long debugging sessions, code review, research, multi-tool tasks.
- Capture safety: same-session updates are serialized, canonical files use
  atomic replacement, and search can report a malformed canvas while continuing
  through readable evidence refs.
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

- Path: `scripts/qdrant_*.py`, `scripts/qdrant_refresh_sessions_index.sh`,
  `scripts/qdrant_bounded_start_restart.sh`, and
  `scripts/qdrant_restart_calibration.sh`
- Purpose: index selected Hermes skills and recent user/assistant session text
  into local Qdrant collections, then check that recall is still live after
  scheduled refreshes or container restarts.
- Default collections: `hermes_skills_multilingual_v1` and
  `hermes_sessions_recent_multilingual_v1`.
- Data handling: skill indexing skips hidden catalog directories and
  lifecycle-marked inactive skills; session indexing skips system prompts and
  tool outputs by default, redacts common secret patterns, and supports dry-run
  previews.
- Health checks: the watchdog is quiet when collections are green; the bounded
  start/restart helper can bring a local Docker-backed Qdrant container back up;
  the restart calibration wrapper records Docker `StartedAt` and reruns the
  watchdog after a container restart before trusting recall.

## Skills

- Path: `skills/context-canvas-memory/SKILL.md`
- Path: `skills/qdrant-recall-sidecar/SKILL.md`
- Purpose: teach Hermes Agent when to use the harness and what pitfalls to avoid.

## Complexity × Bayesian delegation calibrator

- Path: `scripts/delegation_bayes.py`
- Guide: `ops-rules/complexity-bayesian-delegation.md`
- Purpose: score observable task complexity and delegability, compare `direct` with
  the fixed `luna_max` lane, and learn from independently verified outcomes.
- Safety posture: Baton remains the qualitative dispatch brake and the main agent
  owns final integration, validation, and judgment. Hard blockers are
  deterministic; only primary attempts update the posterior.
- Data handling: writes normalized JSONL to an XDG/portable user-state default or
  `BATON_DELEGATION_STORE`; it does not persist raw prompts, logs, paths,
  identities, secrets, or chain-of-thought.

## Ops rules

- Path: `ops-rules/public-release-checklist.md`
- Purpose: keep shared tools reusable and safe before a commit becomes public.
- Path: `ops-rules/artifact-handoff-checklist.md`
- Purpose: turn generated images, reports, docs, and bundles into portable
  share-ready artifacts without leaking the private runtime that produced them.
- Path: `ops-rules/scheduled-agent-health-checklist.md`
- Purpose: keep unattended agent jobs quiet on success, specific on failure, and
  backed by source-level verification rather than scheduler status alone.
- Path: `ops-rules/docs-and-website-update-guide.md`
- Purpose: keep the README, docs, and GitHub Pages site consistent in voice,
  visual tokens, and facts as the toolbox evolves; includes the content sync
  map and verification checklist for public-surface updates.
- Path: `ops-rules/mcp-sidecar-health-checklist.md`
- Purpose: distinguish recovered MCP transport events from sidecar, backend, or
  host failures before restarting services or touching indexed data.
