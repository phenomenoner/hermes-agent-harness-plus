---
name: context-canvas-memory
description: Use for compaction, coordination, or two complexity cues.
version: 1.3.0
author: hermes-agent-harness-plus contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [context-management, task-canvas, evidence, mcp, long-running-tasks]
    related_skills: []
---

# Context Canvas Memory

Use a Task Canvas as the short-term, evidence-backed working map for a long Hermes task. Canonical JSON holds concise nodes; raw logs, diffs, and source excerpts live in referenced evidence files; Mermaid is only a projection.

## Trigger Policy

Choose before the task grows, not after context is already crowded. Apply this ordered decision table; the first matching row decides:

| Priority | Match when | Decision |
|---|---|---|
| 1 | `safety_exclusion = true` | `do_not_start` |
| 2 | `decisive_trigger = true` | `start` |
| 3 | `simple_or_bounded = true` | `do_not_start` |
| 4 | `complexity_signals >= 2` | `start` |
| 5 | `otherwise` | `do_not_start` |

Decisive triggers:

- The task resumes from context compaction or must survive a likely compaction.
- Parallel agents, background jobs, or separate workstreams need one evidence spine.
- A deployment, restart, migration, destructive action, or external write has several safety and verification gates.
- Multiple non-trivial deliverables or at least three substantive phases carry branching, coordination, handoff, or independent verification risk; mere formatting subparts and the ordinary edit/test/report phases do not count.
- Several alternatives, failed paths, or changing assumptions must remain visible.
- The user explicitly asks for a durable working map or a handoff across sessions.

Complexity signals:

- Work is likely to exceed five substantive tool calls.
- More than one repository, source system, or large artifact must be inspected.
- Delegation results must be reconciled with parent-agent verification.
- The task is likely to span several conversational turns.
- The final answer must separate checked evidence from assumptions.

Make the decision before the third substantive tool call. A substantive call gathers task evidence, mutates state, delegates work, or verifies an outcome; loading a skill or reading the task list alone does not count. Do not wait to observe the fifth call when the task shape already predicts it.

Apply these exclusions with the decision table above:

- A simple one-shot question or bounded edit with no branching, delegation, handoff, or decisive trigger does not need a manual Canvas.
- Never put secrets, credentials, or unrestricted private-memory dumps into a Canvas.
- Do not use a Canvas as a raw transcript of every tool call. A Canvas is a map, not the terrain.
- Do not use it as durable long-term recall without triage; use MemPalace after closeout.

## Prerequisites

Verify the MCP server through `terminal`:

```bash
hermes mcp test context_canvas
```

Canvas data lives under the configured Context Canvas home. MCP changes and newly enabled tools may require a new Hermes session.

## Canonical Workflow

### 1. Start early with a stable ID

Call `mcp__context_canvas__canvas_start` with:

- `goal`: one sentence describing the finished outcome.
- `title`: a short human-readable label.
- `session_id`: an explicit stable slug for durable work.

Save the returned `session_id`. If compaction loses it, recover with `canvas_recent`; never guess an ID or start a duplicate by reflex. Reusing the same ID is non-destructive and returns `created: false`.

Always inspect the operation payload's `ok` field. A healthy MCP transport can still return an application-level failure as JSON.

### 2. Record evidence and nodes atomically

Prefer `mcp__context_canvas__canvas_record`. It writes an evidence ref and upserts the concise node in one call.

Each record should contain:

- `content`: original, verifiable evidence or a faithful command result.
- `summary`: the short claim represented on the canvas.
- `source`: file, URL, command, tool, or artifact origin.
- `node_kind`: `finding`, `decision`, `verification`, `blocked`, `gap`, `question`, `plan`, `action`, or `assumption`.
- `node_status`: `planned`, `doing`, `verify`, `done`, `blocked`, or `deprecated`.
- `node_id`: a stable semantic identifier when the fact may be updated.
- `depends_on`: prerequisite node IDs when ordering matters.

Use separate `canvas_add_ref` and `canvas_upsert_node` only when evidence and node lifecycles genuinely differ. Reusing a stable `node_id` aggregates evidence refs and retains the newest `max_refs` entries; the default is 12.

Manual Canvas calls do not perform automatic redaction. Values supplied to `canvas_start`, `canvas_add_ref`, `canvas_upsert_node`, `canvas_record`, and other `canvas_*` operations can be persisted in canonical JSON, events, refs, search results, or closeout exports. The `*` selector below covers every manually supplied field; named selectors add requirements:

| Selector | Required before the call | Automatic protection |
|---|---|---|
| `*` | `minimize+sanitize` | `no` |
| `credential-bearing-url` | `minimize+sanitize+remove-secret` | `no` |
| `sensitive-path` | `minimize+sanitize+replace-with-non-identifying-label` | `no` |
| `autopilot-sanitization` | `manual-safety-still-required` | `no` |

The wildcard includes `goal`, `title`, `summary`, `label`, `source`, `content`, `session_id`, `node_id`, `depends_on`, `metadata`, and every other supplied field. Prefer the smallest safe excerpt that still verifies the claim. Remove credentials, private keys, tokens, personal data, secret query parameters, private hostnames, account identifiers, and unnecessary filesystem detail rather than copying and redacting an oversized payload afterward.

### 3. Keep the graph truthful

Apply this validation contract:

| Rule | Node kinds | Statuses | Contract |
|---|---|---|---|
| Evidence required | `finding`, `action`, `decision`, `blocked`, `gap`, `verification` | `done`, `blocked`, `deprecated`, `verify` | Every matching factual node has at least one readable evidence ref. |

Also keep these graph rules:

- An unsupported statement must be a `plan`, `question`, or `assumption`.
- A disproved path becomes `deprecated`; a currently impossible path becomes `blocked`.
- Update an existing semantic node rather than creating near-duplicates.
- Record decisions with rejected alternatives and consequences, not just the chosen label.
- Capture high-value outputs, not every command heartbeat.

The core invariant is:

```text
concise node -> evidence ref -> original/verifiable content
```

### 4. Checkpoint at phase boundaries

Update the canvas at a phase boundary, not on a timer and not after every call. A checkpoint is warranted when:

- An authoritative source baseline is established or superseded.
- A branch is selected, rejected, blocked, or reopened.
- Delegated or background work returns and is accepted, corrected, or rejected.
- A mutation, deployment, or other side effect is about to cross its approval boundary.
- A verification batch changes a claim from `doing` to `verify`, `done`, or `blocked`.
- Work is about to pause, compact, hand off, or produce the final response.

Before entering a new phase, read the canvas if the current map no longer fits in active context. If the canvas has not changed since the previous phase, explicitly decide whether there was no durable change or whether a checkpoint was missed.

### 5. Coordinate parent and child work

Give concurrent writers the same CanvasStore session path and partition stable node IDs when safe cross-process locking is available. Otherwise route writes through one coordinator.

A separate child Canvas is acceptable only when isolation is intentional. The parent must add an integration node that names the child Canvas ID, records which findings were accepted or rejected, and points to parent-visible evidence. For finished child work, run `mcp__context_canvas__canvas_closeout(session_id="<child-session-id>", write_ref=true)` and integrate its export; that operation does not close lifecycle. For unfinished child work, leave follow-up as `planned` and current blockers as `blocked`. Do not let a child become an invisible competing source of truth.

### 6. Read and recover deliberately

Use:

- `canvas_read(session_id, include_refs=false)` for canonical state and the Mermaid projection.
- `canvas_search(query)` to find nodes and local refs by substring.
- `canvas_recent()` to recover exact session IDs after compaction.

Set `include_refs=true` only when raw evidence is needed. Keeping refs out of routine reads preserves context savings.

A nonzero `skipped_count` is a file-integrity signal. Review the skipped file, preserve readable refs, and repair that canvas before declaring the entire MCP service broken.

## Manual Canvas vs Autopilot

> **Current status:** broad Autopilot capture is retired; this section retains the historical policy contract for evidence interpretation and regression tests.

An explicit Canvas is the supported path: an intentional goal, decision, dependency, and verification map backed by evidence refs. The former Autopilot v2 broad-capture mechanism is retired and its production runtime is forced to `off`; do not enable it or treat replay output as rollout authority.

For historical evidence interpretation only, it was a fail-open mechanism that stored sanitized snapshots before applying this semantic projection policy. “Sanitized” means the configured redactor reached a fixed point; it is not proof that every sensitive value was recognized.

| Policy case | Semantic class |
|---|---|
| `failed-tool-result` | `failure` |
| `failed-allowlisted-verification-command` | `failure` |
| `successful-allowlisted-verification-command` | `verification` |
| `successful-allowlisted-mutation-tool` | `action` |
| `successful-allowlisted-mutation-command` | `action` |
| `successful-delegation` | `action` |
| `successful-ordinary-tool-result` | `none` |
| `successful-ordinary-command` | `none` |

Fixed tool and command allowlists determine which successful calls match the verification and mutation cases; failure classification takes precedence.

The archived mechanism did not know the task goal, rejected alternatives, assumptions, or why evidence changed a decision. That is why broad capture is not a substitute for explicit records and read-back verification. If a future task has a real point-in-time snapshot need, require a new explicit, one-shot, bounded, visible, expiring, and deletable design rather than reactivating the archive.

## Closeout and Durable Triage

### Pre-final reconciliation

Before claiming a long task is complete:

1. Call `mcp__context_canvas__canvas_read(session_id="<active-session-id>", include_refs=false)`.
2. Resolve every stale `doing` or `verify` node to `done`, `deprecated`, `planned`, or `blocked`; unfinished follow-up uses `planned`, and an active blocker uses `blocked`.
3. Confirm current decisions supersede obsolete ones instead of coexisting ambiguously.
4. Confirm every factual kind and finished status covered by the evidence-required contract has a readable ref.
5. Integrate all returned child or delegated work into the parent map.
6. Run `mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=false)` as a non-writing preview.
7. If the preview matches the verified outcome, run `mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=true)` to write the export.

`canvas_closeout` only previews or writes a triage summary, event, and MemPalace-ready export. It does not set lifecycle state and does not mark the Canvas closed; no current manual Canvas MCP operation closes lifecycle, and the Canvas remains readable and updateable after export. A successful export does not invoke MemPalace or mean every node deserves long-term storage.

Before filing anything durably:

- Keep reusable decisions, stable constraints, exact evidence, and unresolved risks.
- Drop scratch notes, temporary TODO state, command chatter, and stale completion logs.
- Redact or exclude all credentials and secrets.
- Verify the final report agrees with the canvas evidence.

## Node Guidance

- `finding`: observed behavior or source fact.
- `decision`: chosen approach with trade-offs.
- `verification`: test, build, health check, or read-back proof.
- `blocked`: path cannot proceed now; include the blocker.
- `gap`: required evidence or coverage is missing.
- `question`: unresolved issue whose answer changes the work.
- `assumption`: explicitly unverified premise.
- `plan` / `action`: intended or executing work, not proof of completion.

Status should describe current truth. A planned node is not done merely because time passed.

## Pitfalls

- Starting after the evidence flood instead of forecasting task shape.
- Treating Mermaid as the database; JSON is canonical.
- Writing confident summaries without evidence refs.
- Capturing every tool result and recreating a transcript.
- Starting a second canvas after compaction instead of using `canvas_recent`.
- Leaving stale active nodes after the work has finished.
- Keeping child findings outside the parent evidence map.
- Treating archived Autopilot snapshots as current truth or a complete reasoning record.
- Filing the full closeout into MemPalace without triage.

## Verification

Before finalizing a long task:

1. `mcp__context_canvas__canvas_read(session_id="<active-session-id>", include_refs=false)` returns the intended goal and exact active session ID.
2. Every factual kind with status `done`, `blocked`, `deprecated`, or `verify` has at least one readable ref.
3. Blocked and deprecated paths remain visible.
4. Decision nodes include relevant trade-offs.
5. Verification nodes contain actual command or source evidence.
6. No stale active node contradicts the final report.
7. Child work is integrated into the parent map.
8. `mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=true)` writes the intended export without being mistaken for lifecycle closure.
9. The final report does not claim more than the evidence proves.
