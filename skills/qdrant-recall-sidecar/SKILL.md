---
name: qdrant-recall-sidecar
description: Use when adding, checking, or troubleshooting a local Qdrant recall sidecar for Hermes Agent skills or recent sessions. Prefer local-first indexing, dry-run previews, and privacy-preserving defaults.
version: 1.0.0
author: hermes-agent-harness-plus contributors
license: MIT
metadata:
  hermes:
    tags: [qdrant, recall, vector-search, mcp, privacy]
    related_skills: [context-canvas-memory]
---

# Qdrant Recall Sidecar

## Overview

A Qdrant sidecar gives Hermes Agent a local semantic search layer over selected
skills and recent session text. It is useful when exact keyword search is too
brittle but cloud memory is not desired.

The safe default is local-first: bind Qdrant to `127.0.0.1`, preview indexing
with `--dry-run`, and index only what you are comfortable searching later.

## When to Use

- Hermes needs semantic recall over installed skills.
- Recent user/assistant session text should be searchable locally.
- You want an MCP tool that can search Qdrant from inside Hermes.

Do not use it to index secrets, raw tool outputs, private datasets, or system
prompts unless you have reviewed and approved that data class.

## Basic Workflow

1. Start local Qdrant.
2. Dry-run the skill or session indexer.
3. Recreate or upsert the collection only after previewing content.
4. Add the Qdrant MCP sidecar to Hermes config.
5. Verify collections, vector size, point count, and a real search result.

## Common Pitfalls

1. Mixing embedding models across collections with similar names.
2. Indexing private data because dry-run was skipped.
3. Forgetting that local recall is still a searchable copy of text.
4. Treating Qdrant as a replacement for curated memory. It is retrieval, not
   judgment.

## Verification Checklist

- [ ] Qdrant is reachable at the configured URL.
- [ ] Collection vector size matches the embedding model.
- [ ] Dry-run preview was reviewed before ingest.
- [ ] MCP search returns compact, relevant results.
- [ ] Health watchdog is silent when healthy and noisy when broken.
