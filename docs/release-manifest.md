# Release Manifest

Initial public bundle:

- Context Canvas package, CLI, and MCP server.
- Context Canvas Autopilot plugin.
- Qdrant skill/session indexing, MCP search, refresh, and health-check scripts.
- Skills for Task Canvas and Qdrant recall sidecars.
- Optional complexity × Bayesian delegation calibrator with score, recommend,
  record, and report commands.
- Release, artifact handoff, scheduled-agent, and MCP sidecar health checklists.
- Installation guide and GitHub Pages website.

Kept outside this bundle:

- runtime databases, raw logs, and account-specific configuration;
- calibration observations and other local user-state stores;
- credentials, cookies, tokens, or production secrets;
- local project datasets and machine-specific state.

The repository is meant to stay portable: install the pieces you want, then wire
them to your own local Hermes Agent setup.
