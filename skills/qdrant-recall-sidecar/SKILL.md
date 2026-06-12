---
name: qdrant-recall-sidecar
description: Use when adding, checking, or troubleshooting a local Qdrant recall sidecar for Hermes Agent skills or recent sessions. Prefer local-first indexing, dry-run previews, and privacy-preserving defaults.
version: 1.1.0
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
6. Schedule the quiet watchdog.
7. If Qdrant runs in Docker, use bounded start/restart for service bring-up and
   restart calibration for post-restart recall verification.
8. If repair is needed, fix the smallest failing collection first, then verify quiet mode.

## Common Pitfalls

1. Mixing embedding models across collections with similar names.
2. Indexing private data because dry-run was skipped.
3. Forgetting that local recall is still a searchable copy of text.
4. Treating Qdrant as a replacement for curated memory. It is retrieval, not
   judgment.
5. Trusting on-disk collection folders without checking the live Qdrant API after
   a container restart.
6. Letting Docker CLI stalls or broad rebuild scripts turn a healthy Qdrant HTTP
   endpoint into a scheduled-task timeout.
7. Rebuilding every corpus when the watchdog only reports one `MISSING`
   collection.
8. Mixing service bring-up with data repair. Start or restart the local Qdrant
   container first; only rebuild collections after the watchdog still reports a
   data or vector-configuration problem.

## Verification Checklist

- [ ] Qdrant is reachable at the configured URL.
- [ ] Collection vector size matches the embedding model.
- [ ] Dry-run preview was reviewed before ingest.
- [ ] MCP search returns compact, relevant results.
- [ ] Health watchdog is silent when healthy and noisy when broken.
- [ ] Docker-backed Qdrant has a bounded start/restart path, when useful.
- [ ] Docker restart calibration is enabled when Qdrant is containerized.
- [ ] Repair runs target the specific missing or unhealthy collection before any
      full rebuild.
