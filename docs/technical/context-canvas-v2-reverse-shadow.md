# Context Canvas v2 reverse-shadow archive

## Status

Archived revision: `0.2.5-retired-broad-capture`

**Broad post-tool snapshot capture and automatic semantic promotion are retired.**
The production plugin runtime is forced to `off`, including when an old config
still requests an active mode. Do not start a new payload-capture soak or canary.

Context Canvas itself is not retired. The supported path remains the explicit
Canvas package, MCP tools, and `context-canvas-memory` skill.

## Decision

The job to be done is to recover decisions, blockers, dependencies,
verifications, and evidence after a long task, context compaction, or handoff.
That job is already covered by two intentional layers:

1. Hermes session history preserves chronology and can be searched later.
2. Explicit Context Canvas records a curated evidence map with
   `canvas_start`, `canvas_record`, `canvas_read`, `canvas_search`, and
   `canvas_closeout`.

The broad Autopilot experiment added a second payload-persistence plane but did
not establish an incremental improvement over that baseline in recovery time,
tool reruns, task quality, or user trust. Its runtime authority is therefore
removed rather than extended through another soak.

The implementation, source-only replay, reporter, and regression tests remain
in the repository for historical inspection and safety debt while old copies may
still exist. A green replay or report has **no product or rollout authority**.

## Historical design

The experiment separated four roles that its predecessor mixed together:

1. **Observation** — one content-free metric row per tool event.
2. **Snapshot cache** — a sanitized point-in-time invocation/result envelope,
   compressed and addressed by SHA-256.
3. **Semantic projection** — selected failures, successful verification
   commands, and state-changing actions became Canvas nodes.
4. **Lifecycle** — cache sessions recorded turn end/finalization separately from
   the semantic graph.

Historical active modes used this layout:

```text
~/.hermes/context-canvas-cache-v2/       # owner-only snapshot cache
  objects/text/sha256/ab/<digest>.json.zlib
  objects/binary/sha256/cd/<digest>.bin
  sessions/<session>/
    snapshots/sr_000001.json
    state.json
    lifecycle.json

~/.hermes/context-canvas/                # semantic projection
  auto-v2-<session>/
    canvas.json
    events.jsonl
    refs/tc_001.md

~/.hermes/context-canvas-soak/
  v2-active-legacy-shadow/
    metrics.jsonl
```

This layout is historical evidence, not an installation recommendation.

## Retained safety and forensics behavior

The dormant path retains these bounded defenses and regression checks:

- production config resolves every requested mode to `off`;
- the Hermes force-redactor is applied to a bounded fixed point before text is
  persisted; backend drift, cycles, or non-convergence cancel capture;
- valid embedded `data:*;base64` payloads are moved to content-addressed binary
  objects and replaced by digest/MIME/size receipts;
- malformed data URLs are removed and recorded as
  `invalid_data_urls_removed`; this is a fidelity loss, not a storage failure;
- a real binary-object storage failure remains an externalization hard failure;
- reporter and replay checks require one contiguous data URL, avoiding false
  positives from separated documentation terms;
- Context Canvas self-calls remain excluded;
- hook failures remain outside the original tool result.

Fixed-point behavior only proves that another pass of the same redactor no
longer changes the text. It is not a confidentiality guarantee and does not
prove that every sensitive value was recognized.

The old `retention_days` field is metadata. The archived implementation does not
provide an enforced expiry/garbage-collection path, so it must not be described
as an active retention policy.

## Runtime fence

Existing installations should remove `context-canvas-autopilot` from
`plugins.enabled`. A stale entry may remain temporarily during cleanup, but it
must read:

```yaml
plugins:
  entries:
    context-canvas-autopilot:
      mode: off
      revision: 0.2.5-retired-broad-capture
```

The production code accepts only `off`. Historical active modes are available
only through the module's explicit test configuration used by source replay and
regression tests.

### Archived code-execution trust boundary

Historical replay still loads the Context Canvas package. Its `tool_root` is a
**code-execution trust setting**, not a data directory. Any test-only root must
be an absolute, owner-controlled directory containing regular
`context_canvas/__init__.py`, `context_canvas/core.py`, and
`context_canvas/snapshot.py` files. Resolution gives that root import
precedence and rejects modules loaded from another origin.

Validation requires POSIX effective-UID semantics. It rejects symbolic links,
group- or world-writable code paths, and replacement-capable ancestors; on a
platform without an equivalent ownership check, activation must fail closed.
Do not point historical replay at a download or shared writable directory.

## Historical report semantics

The reporter still emits `PASS`, `HOLD`, or `FAIL` so old evidence can be read:

- **FAIL** — a safety/integrity hard gate failed.
- **HOLD** — hard gates passed but sample, performance, storage, redactor-audit,
  or snapshot-fidelity criteria did not.
- **PASS** — the checked historical safety criteria passed.

Every report now also emits:

```json
{
  "decision": "historical_safety_evidence_only",
  "product_authority": "none"
}
```

A `PASS` never authorizes installation, live capture, a canary, or semantic
promotion.

Historical hard failures include:

- invalid metric rows or capture failures;
- Context Canvas self-capture;
- model-visible tool-result replacement;
- a successful capture without the expected redactor backend;
- data-URL object-storage failures;
- invalid manifests, missing objects, decompression failures, or digest
  mismatches;
- a contiguous raw `data:*;base64` value in a text envelope;
- persisted text changed by another redactor pass;
- event-pointer or metric/manifest join failures.

`invalid_data_urls_removed` is surfaced separately as
`snapshot_fidelity_loss`. It is intentionally not mislabeled as object-storage
failure.

## Historical verification only

The retained source can be checked without touching live storage:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_context_canvas_autopilot.py \
  tests/test_context_canvas_soak_report.py

python scripts/context_canvas_v2_replay.py --sync-writes
```

Replay writes only to a newly created `/tmp/context-canvas-v2-replay-*`
directory unless an explicit output root is supplied. It is historical safety
evidence only. Do not copy its mode into Hermes config or use its result as a
live-readiness gate.

## If a future exact snapshot need appears

Start a new, separately reviewed design rather than reactivating this plugin.
The minimum contract is:

- explicit opt-in for one named task and one next/specified tool call;
- visible capture indicator;
- bounded bytes and default-no-promotion behavior;
- enforced expiry and orphan-object garbage collection;
- list, read-back, and immediate delete operations;
- default denial for secrets, credentials, memory, and session-history tools;
- sanitize/integrity failure cancels only the capture, never the original tool;
- measured benefit against explicit Canvas plus Hermes session history.

Until such a need and design are independently established, broad payload
capture remains retired.
