# Technical Note: Task Canvas

A Task Canvas is a small evidence-backed working map for an agent task.

The supported workflow is explicit: start a Canvas for a named task, attach the
smallest sufficient evidence with `canvas_record`, and read or search that map
after compaction or handoff. Hermes session history retains chronology; the
Canvas retains curated state and paths back to verifiable evidence.

The former broad-capture Autopilot is retired and forced off in production. See
the [Autopilot v2 archive](context-canvas-v2-reverse-shadow.md) for its historical
design, retirement decision, and retained safety replay.

Canonical state lives in JSON and JSONL:

```text
~/.hermes/context-canvas/<session_id>/
  canvas.json
  events.jsonl
  canvas.mmd
  refs/tc_001.md
  state.json
```

The rule is simple:

```text
node summary -> evidence ref -> original/verifiable content
```

`canvas.mmd` is only a Mermaid projection. Do not edit it as the source of
truth.

## Why this exists

Long agent tasks often lose the shape of the work. Raw logs are too large for
active context, but summaries without evidence drift. Task Canvas keeps the map
small and keeps the terrain recoverable.

## Factual-node invariant

Nodes with factual kinds (`finding`, `action`, `decision`, `blocked`,
`verification`) and finished statuses (`done`, `blocked`, `deprecated`,
`verify`) must include at least one evidence ref.

## Concurrent capture safety

Parallel tool calls may finish at the same time, so Task Canvas serializes every
same-session mutation. JSON, state, Mermaid, ref, and closeout files are written
with atomic replacement so readers do not observe partial files.

On Linux and WSL, a per-session POSIX file lock protects separate processes as
well as threads. On non-POSIX hosts, the package provides thread safety only;
route writes for one canvas through a single process when multiple processes may
capture evidence concurrently.

Starting an explicit session ID is non-destructive. If the canvas already
exists, `start` returns it with `created: false` instead of resetting its nodes,
refs, or event history. Automatically generated IDs include subsecond and random
components to avoid collisions between simultaneous starts.

## Graceful search when a canvas needs attention

Search validates each canonical canvas before reading its nodes. If one canvas
is malformed or unreadable, the result reports it in `skipped_sessions` and
continues searching other canvases and available evidence refs. This keeps one
damaged summary from hiding the rest of the evidence store.

Treat a nonzero `skipped_count` as a file-integrity signal, not proof that the
MCP transport or every canvas is unavailable. Preserve the original file before
repair, verify the session directory and JSON structure, and use the evidence
refs as the recovery source when they remain readable.
