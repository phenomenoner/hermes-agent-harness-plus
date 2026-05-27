# Hermes Agent Harness Plus

A cheerful companion toolbox for people who want Hermes Agent work to be easier
to follow, search, and share.

In one sentence: **Harness Plus adds lightweight evidence trails, local recall,
and ready-to-use helper pieces around Hermes Agent without changing Hermes Agent
itself.**

## The 30-second story

Long agent sessions can move fast. A tool call creates a useful clue, a test log
answers a question, and ten minutes later the important part is buried in the
scrollback.

Harness Plus gives Hermes Agent a small outer harness: optional adapters,
plugins, sidecars, skills, and checklists that help keep the work tidy.

The first bundle focuses on three habits:

- **Keep the map.** A Task Canvas stores compact progress notes while detailed
  evidence lives in referenced files.
- **Find it again.** Qdrant helpers can index selected local skills and recent
  sessions, then quietly check that local recall stays healthy after scheduled
  refreshes or container restarts.
- **Share cleanly.** Install notes, release routines, and reusable skills make it
  easier to turn a useful local helper into something another Hermes Agent user
  can try.

## What's inside

- `packages/context-canvas/` — a small local Task Canvas library with CLI and
  MCP server wrappers.
- `plugins/context-canvas-autopilot/` — an optional Hermes plugin hook that
  writes evidence after long or large tool-use patterns; it does not mutate the
  active conversation.
- `scripts/` — Qdrant and Context Canvas helper scripts for MCP, indexing, and
  health checks.
- `skills/` — Hermes skills that explain when and how to use the harness.
- `ops-rules/` — release and contribution checklists for shared tools.
- `docs/` — the GitHub Pages site plus installation and technical notes.

## Quick start

Clone the repo:

```bash
git clone https://github.com/phenomenoner/hermes-agent-harness-plus.git
cd hermes-agent-harness-plus
```

Run the local Task Canvas smoke tests:

```bash
python -m pip install -e '.[mcp]' pytest
python -m pytest -q
```

Use the Task Canvas CLI directly from the source tree:

```bash
PYTHONPATH=packages/context-canvas python -m context_canvas.cli start \
  --session-id demo \
  --goal "Keep evidence for a long Hermes task"
```

Then read the guide:

- [Install and enable the harness](docs/install.md)
- [Catalog of components](docs/catalog.md)
- [Release manifest](docs/release-manifest.md)

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
