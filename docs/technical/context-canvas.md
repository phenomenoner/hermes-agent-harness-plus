# Technical Note: Task Canvas

A Task Canvas is a small evidence-backed working map for an agent task.

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
