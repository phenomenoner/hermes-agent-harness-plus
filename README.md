<div align="center">

# 🧸✨ Hermes Agent Harness Plus

### Give your Hermes Agent a memory, a map, and a health plan 💖

**The cheerful companion toolbox that keeps long agent sessions tidy, searchable, and share-ready — without changing Hermes Agent itself.**

[![CI](https://github.com/phenomenoner/hermes-agent-harness-plus/actions/workflows/ci.yml/badge.svg)](https://github.com/phenomenoner/hermes-agent-harness-plus/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-ff7ab6.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6bbcff.svg)](pyproject.toml)
[![MCP native](https://img.shields.io/badge/MCP-native-8e7dff.svg)](docs/install.md)
[![Local first](https://img.shields.io/badge/data-local--first-68d8b2.svg)](docs/catalog.md)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ffb36b.svg)](CONTRIBUTING.md)

🌈 **[Visit the website](https://phenomenoner.github.io/hermes-agent-harness-plus/)** · 📦 [Install guide](docs/install.md) · 🧩 [Component catalog](docs/catalog.md) · 🐛 [Report a bug](https://github.com/phenomenoner/hermes-agent-harness-plus/issues)

</div>

---

## 😵‍💫 The problem

Long agent sessions move *fast*. A tool call surfaces a golden clue, a test log answers the big question… and ten minutes later it's all buried in scrollback. Your agent forgets, you scroll forever, and nobody can retrace how the answer was found.

## 💡 The fix

**Harness Plus wraps a small, friendly harness around Hermes Agent** — optional adapters, plugins, sidecars, skills, and checklists that build three healthy habits:

| Habit | What it means | Powered by |
|---|---|---|
| 🗺️ **Keep the map** | Compact progress notes on a Task Canvas, with raw evidence parked in referenced files | `context-canvas` |
| 🔎 **Find it again** | Semantic recall over your skills & recent sessions — 100% local, on your own Qdrant | `qdrant_*` helpers |
| 🎁 **Share cleanly** | Install notes, release routines, and reusable skills that travel well to other users | `skills/` + `ops-rules/` |

Everything is **opt-in, local-first, and fail-open**. Take one piece or take them all — Hermes Agent itself is never modified. 🤝

---

## 🎁 What's in the box

```text
hermes-agent-harness-plus/
├── 📦 packages/context-canvas/          Task Canvas library + CLI + MCP wrappers
├── 🤖 plugins/context-canvas-autopilot/ Auto-writes evidence after heavy tool use
├── 🔧 scripts/                          Qdrant & Canvas helpers: MCP, indexing, watchdogs
├── 📚 skills/                           Teach Hermes when & how to use the harness
├── ✅ ops-rules/                        Release / handoff / scheduled-job checklists
└── 📖 docs/                             Website, install guide, technical notes
```

| Component | Superpower |
|---|---|
| 🗺️ **Context Canvas** | Short JSON nodes + evidence refs — the agent keeps the *shape* of the work and a path back to the facts |
| 🛰️ **Canvas MCP sidecar** | Native MCP tools: `canvas_start`, `canvas_add_ref`, `canvas_upsert_node`, `canvas_read`, `canvas_search`, `canvas_closeout` |
| 🤖 **Autopilot plugin** | Watches completed tool calls, writes evidence when things get long or large — never touches conversation history |
| 🔎 **Qdrant recall kit** | Index selected skills & recent sessions locally; dry-run previews and secret-pattern redaction by default |
| 🩺 **Recall watchdog** | Quiet when healthy, loud when broken — validates collections after refreshes, container starts, and restarts |
| 📚 **Public skills** | `context-canvas-memory` & `qdrant-recall-sidecar` — drop-in guidance for any Hermes Agent user |
| 🧭 **Delegation calibrator** | Optional complexity × Bayesian evidence for direct vs `luna_max`; [Baton](https://github.com/phenomenoner/baton-fanout-skill) remains the qualitative dispatch brake and validation authority |

---

## ⚡ Quick start (60 seconds)

```bash
# 1. Grab the toolbox
git clone https://github.com/phenomenoner/hermes-agent-harness-plus.git
cd hermes-agent-harness-plus

# 2. Prove it works on your machine
python -m pip install -e '.[mcp]' pytest
python -m pytest -q

# 3. Start your first Task Canvas 🎉
PYTHONPATH=packages/context-canvas python -m context_canvas.cli start \
  --session-id demo \
  --goal "Keep evidence for a long Hermes task"
```

Then wire it into Hermes Agent (full walkthrough in the [install guide](docs/install.md)):

```yaml
# Hermes Agent config.yaml
mcp_servers:
  context_canvas:
    command: "python"
    args: ["/absolute/path/to/hermes-agent-harness-plus/scripts/context_canvas_mcp_server.py"]
    env:
      HERMES_CONTEXT_CANVAS_HOME: "/home/you/.hermes/context-canvas"
```

---

## 🧭 Design principles

- 🏠 **Local-first.** Your notes, your sessions, your Qdrant on `127.0.0.1`. Nothing leaves home.
- 🪶 **Featherweight.** Optional pieces around Hermes Agent — zero changes to Hermes itself.
- 🛟 **Fail-open.** If a helper can't write, your session continues like nothing happened.
- 🔍 **Preview before index.** `--dry-run` everywhere; secret patterns redacted; system prompts & tool outputs skipped by default.
- 🧾 **Evidence or it didn't happen.** Every finished note points to a ref you can verify: `node summary → evidence ref → original content`.

---

## 📚 Learn more

| Guide | What you'll find |
|---|---|
| 🌈 [Website](https://phenomenoner.github.io/hermes-agent-harness-plus/) | The pretty tour |
| ✦ [Context Canvas origin story](https://phenomenoner.github.io/hermes-agent-harness-plus/context-canvas-story.html) | A first-person product diary: the late-night idea, the first live capture, and the repair that made it real |
| 📦 [Install & enable](docs/install.md) | Step-by-step setup: CLI, MCP, plugin, Qdrant, skills |
| 🧩 [Component catalog](docs/catalog.md) | Every piece, its purpose, and its safety posture |
| 🗺️ [Context Canvas internals](docs/technical/context-canvas.md) | Data model & technical notes |
| 🔎 [Qdrant recall internals](docs/technical/qdrant-recall.md) | Indexing, collections & health checks |
| 🧭 [Delegation calibrator](ops-rules/complexity-bayesian-delegation.md) | Optional complexity × Bayesian routing evidence; Baton remains the dispatch brake and the main agent owns final judgment |
| 🚀 [Release manifest](docs/release-manifest.md) | What ships in each bundle |

---

## 🤝 Contributing

Found a bug? Have a helper that made *your* Hermes life better? We'd love it! 💌

Read [CONTRIBUTING.md](CONTRIBUTING.md) first — the golden rule is **share-ready**: no secrets, no machine-specific paths, copy-pasteable install steps. See also our [Code of Conduct](CODE_OF_CONDUCT.md) and [Security policy](SECURITY.md).

## 📜 License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE) for third-party pointers (Hermes Agent, Qdrant, MCP SDK, FastEmbed 💕).

<div align="center">

---

**Pick the pieces you need, leave the rest — and may your agent never lose a clue again.** 🧸✨

*Built as a companion toolbox for [Hermes Agent](https://github.com/NousResearch/hermes-agent) users.*

</div>
