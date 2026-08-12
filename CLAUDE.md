# CLAUDE.md

Guidance for AI agents working in this repository.

## What this repo is

A companion toolbox for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
Task Canvas evidence trails (`packages/context-canvas/`), archived Autopilot
source (`plugins/`), local Qdrant recall helpers (`scripts/`), Hermes skills
(`skills/`), and ops checklists (`ops-rules/`). Hermes Agent itself is never
modified; every piece is optional, local-first, and fail-open.

## Before touching README, docs/, or the website

**Read `ops-rules/docs-and-website-update-guide.md` first.** It is the single
source of truth for brand voice, color tokens, section anatomy, the content
sync map, and the verification checklist. Non-negotiables:

- `docs/index.html` stays a single self-contained file (no external assets,
  fonts, or analytics) and keeps its `prefers-reduced-motion` and
  `:focus-visible` accessibility blocks.
- Honest marketing only: no invented numbers, testimonials, or claims.
- The same quick-start commands must appear identically in `docs/install.md`,
  `README.md`, and the website `#quickstart` blocks (visible text and
  `data-copy` attributes).
- Website links to `.md` files use GitHub blob URLs, never raw relative paths.

## Commands

```bash
python -m pip install -e '.[mcp]' pytest   # install for development
python -m pytest -q                        # smoke tests (must pass before push)
```

## Deployment

GitHub Pages serves `main` branch `/docs` automatically — pushing to `main` is
the deploy. Verify after ~1–2 minutes with a cache-busted fetch:
`curl -s "https://phenomenoner.github.io/hermes-agent-harness-plus/?cb=$RANDOM"`.

## Conventions

- Commits: `docs:` / `feat:` / `fix:` prefix, imperative subject ≤ 72 chars.
- Anything public must pass `ops-rules/public-release-checklist.md` — no
  secrets, no machine-specific paths, placeholders documented.
- New third-party dependencies get a source + version + license entry in
  `NOTICE`.
