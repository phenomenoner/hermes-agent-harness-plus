---
name: context-canvas-memory
description: Use when a Hermes Agent task becomes long, tool-heavy, evidence-heavy, or likely to lose context. Maintain a compact Task Canvas with raw evidence refs instead of carrying every log in active context.
version: 1.0.1
author: hermes-agent-harness-plus contributors
license: MIT
metadata:
  hermes:
    tags: [context-management, task-canvas, evidence, mcp, long-running-tasks]
    related_skills: []
---

# Context Canvas Memory

## Overview

Use a Task Canvas as a short-term working map for long Hermes Agent tasks. The
canvas keeps concise nodes in JSON, while raw logs, diffs, and command outputs
live in referenced evidence files.

The goal is not to remember everything forever. The goal is to keep the agent
from losing the shape of the work while preserving a path back to the facts.

## When to Use

- A task has five or more tool calls.
- Logs, diffs, browser output, or research notes are too large for active
  context.
- The agent has tried several paths and needs to avoid repeating dead ends.
- The final answer must cite what was actually checked.

Do not use it for secrets, private memory dumps, or simple one-shot questions.

## Basic Workflow

1. Start a canvas with a one-sentence goal.
2. Add raw evidence refs for important tool outputs.
3. Add or update short nodes that point to those refs.
4. Mark failed paths as blocked or deprecated instead of leaving stale notes.
5. Close out with decisions, verification, and follow-up items.

## Invariant

Every factual finished node should follow this chain:

```text
node summary -> evidence ref -> original/verifiable content
```

If a note has no evidence, mark it as a plan, question, or assumption.

## Common Pitfalls

1. Treating Mermaid as the database. Mermaid is a display projection; JSON is the
   source of truth.
2. Writing confident summaries without evidence refs.
3. Capturing everything. A canvas is a map, not a transcript.
4. Saving temporary task scratch into durable memory.
5. Treating one skipped or malformed canvas as a full MCP outage. Check
   `skipped_count`, preserve the original file, and recover from readable refs
   before considering wider service repair.

## Verification Checklist

- [ ] The canvas goal is clear.
- [ ] Factual completed nodes have evidence refs.
- [ ] Large outputs are offloaded to refs.
- [ ] Active blockers and deprecated paths are visible.
- [ ] Parallel writers use the same CanvasStore session path; on non-POSIX
  systems, cross-process writes are routed through one process.
- [ ] A nonzero search `skipped_count` is reviewed as a file-integrity signal.
- [ ] The final answer is consistent with the evidence.
