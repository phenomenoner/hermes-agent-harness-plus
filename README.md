# Hermes Agent Harness Plus

A small public toolbox for people who want Hermes Agent to feel steadier during
long, tool-heavy work.

In one sentence: **Harness Plus adds evidence, recall, and publishing hygiene
around Hermes Agent without changing Hermes Agent itself.**

## The 30-second story

Hermes Agent is already a capable agent runtime. This repository is the extra
harness we keep around it: tiny adapters, optional plugins, memory sidecars,
ops rules, and reusable skills that make work easier to audit and easier to
share.

The first bundle focuses on three habits:

- **Keep the map, not the mess.** A Task Canvas stores compact progress nodes
  while raw logs and evidence live in referenced files.
- **Recall locally.** Qdrant sidecars index public-safe skills and recent
  sessions so an agent can search its own working history on your machine.
- **Publish safely.** Checklists and labels keep shared harness code separate
  from private identity, private channels, credentials, and local-only projects.

This is **not** a personality pack, a private memory closet, or a dump of one
operator's system. It is a public-facing toolkit that other Hermes Agent users
can inspect, fork, and adapt.

## What's inside

- `packages/context-canvas/` — a small local Task Canvas library with CLI and
  MCP server wrappers.
- `plugins/context-canvas-autopilot/` — an optional Hermes plugin hook that
  writes evidence after long or large tool-use patterns; it does not mutate the
  active conversation.
- `scripts/` — Qdrant and Context Canvas helper scripts for MCP, indexing, and
  health checks.
- `skills/` — public Hermes skills that explain when and how to use the harness.
- `ops-rules/` — release and contribution checklists for keeping public tools
  public-safe.
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
PYTHONPATH=packages/context-canvas python -m context_canvas.cli start   --session-id demo   --goal "Keep evidence for a long Hermes task"
```

Then read the install guide:

- [Install and enable the harness](docs/install.md)
- [Catalog of components](docs/catalog.md)
- [Public-safety checklist](docs/public-safety-checklist.md)

## Design boundary

Harness Plus is intentionally boring:

- no secrets;
- no private chat logs;
- no private agent identity or persona material;
- no private A2A channels;
- no local project datasets;
- no production credentials;
- no hard dependency on a single operator's machine.

Adapters should either be generic or stay out of this repository.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
