# Context Canvas v2 reverse-shadow design

## Status

Candidate revision: `0.2.3-reverse-shadow-r4`

This revision covers only the optional Context Canvas soak reporter, package,
and user plugin. It does not alter Hermes conversation history, model-visible
tool results, or prompt construction.

Context Canvas v2 is a lightweight functional plugin focused on snapshot/cache,
selective semantic projection, and reverse-shadow quality comparison. It is not
a system-level security or platformization layer.

## Decision

Context Canvas v2 separates four roles that v1 mixed together:

1. **Observation** — one content-free metric row per tool event.
2. **Snapshot cache** — a full sanitized point-in-time invocation/result
   envelope, compressed and addressed by SHA-256.
3. **Semantic projection** — only failures, successful verification commands,
   and state-changing actions become Canvas nodes.
4. **Lifecycle** — cache sessions record turn end/finalization separately from
   the semantic graph.

The v2 path is active. In `v2_active_legacy_shadow`, the v1 policy receives the
same tool event as a shadow, but it writes only a decision, reason, estimated ref
bytes, and estimated node count to the private metrics ledger. It does not
duplicate v1 payloads, refs, or nodes. The stateful shadow decision is computed
under the hook receive-order lock before an immutable queued event is handed to
workers. `event_sequence` is a bounded, content-free metric field; it lets
reporting and tests recover the ordered decision stream even when persistence
workers finish out of order. Synchronous and asynchronous writes therefore
share the same legacy threshold attribution. `v2_active` does not run the
stateful legacy evaluator and emits only disabled/zero legacy fields.

## Filesystem layout

```text
~/.hermes/context-canvas-cache-v2/       # owner-only cache, not Canvas search
  objects/text/sha256/ab/<digest>.json.zlib
  objects/binary/sha256/cd/<digest>.bin
  sessions/<session>/
    snapshots/sr_000001.json             # bounded manifest, no raw content
    state.json
    lifecycle.json

~/.hermes/context-canvas/                # high-signal semantic map only
  auto-v2-<session>/
    canvas.json
    events.jsonl
    refs/tc_001.md                        # small pointer/excerpt

~/.hermes/context-canvas-soak/
  v2-active-legacy-shadow/
    metrics.jsonl                         # owner-only, content-free
```

A full snapshot is never silently truncated. Embedded `data:*;base64` payloads
are decoded into content-addressed binary objects and replaced in the text
envelope with a digest/MIME/size pointer. Undecodable data URLs are removed and
reported as an externalization error; the reverse-shadow gate then fails.

Automatic persistence always invokes Hermes' force-redactor with URL credential
redaction before the text object is written. No automatic unsanitized copy is
kept. A future sealed-raw tier would require explicit operator approval,
encryption, access logging, and a separate retention policy.

## Active semantic policy

Every non-Canvas tool call receives a cache manifest. A Canvas is created only
when at least one event is promoted:

| Event | Aggregate node |
|---|---|
| Tool error, nonzero exit, traceback | `AUTO_V2_FAILURES` (`blocked/blocked`) |
| Successful test/lint/build/health command | `AUTO_V2_VERIFICATIONS` (`verification/done`) |
| File/config/deploy/memory/cron mutation or delegation | `AUTO_V2_ACTIONS` (`action/done`) |
| Retrieval, browsing, ordinary reads | Cache only; no node |
| Context Canvas MCP/tool calls | Excluded from active cache; shadow decision still measured |

Each aggregate node keeps at most 12 recent semantic refs. Older full snapshots
and manifests remain in the cache until retention policy is applied.

## Configuration

Behavioral settings live in Hermes `config.yaml`:

```yaml
plugins:
  enabled:
    - context-canvas-autopilot
  entries:
    context-canvas-autopilot:
      mode: v2_active_legacy_shadow
      revision: 0.2.3-reverse-shadow-r4
      cache_root: ~/.hermes/context-canvas-cache-v2
      metrics_root: ~/.hermes/context-canvas-soak/v2-active-legacy-shadow
      retention_class: ephemeral-cache
      retention_days: 30
      max_semantic_refs: 12
      legacy_tool_threshold: 5
      legacy_large_result_chars: 6000
      legacy_max_ref_chars: 50000
      metrics_enabled: true
      require_hermes_redactor: true
```

Modes:

- `v2_active_legacy_shadow` — soak mode; v2 persists, v1 only scores.
- `v2_active` — v2 persists without running or emitting legacy comparison.
- `legacy_active_safe` — emergency compatibility mode: v1 decides which events
  are projected, but v2 redaction/object storage remains the persistence layer.
- `off` — no capture and no metrics.

The old `HERMES_CONTEXT_CANVAS_*` behavior variables are compatibility
fallbacks only. New configuration uses `config.yaml`; the path-only
`HERMES_CONTEXT_CANVAS_TOOL` mechanism remains supported.

## Pre-registered benchmark

The soak report is generated by:

```bash
python scripts/context_canvas_v2_soak_report.py
```

### Hard FAIL / disable-or-rollback gates

Every value must remain zero:

- invalid or wrong-schema metric rows;
- active capture failures;
- active snapshots of Context Canvas self-calls;
- model-visible replacement (`replacement_applied` is always false);
- successful captures not using the Hermes force-redactor;
- data-URL externalization errors;
- invalid manifests, missing objects, decompression failures, or digest
  mismatches;
- raw `data:*;base64` remaining in a text envelope;
- stored envelope text that changes when the force-redactor is re-applied.

### Decisive-improvement gates

For the lightweight live reverse-shadow soak, the reporter defaults require at
least 48 hours, 100 eligible tool events, and 5 sessions. The hook latency
gates and reporter defaults are aligned at p95 at most 5 ms and p99 at most
20 ms:

- snapshot coverage: at least 99.9%;
- hook latency: p95 at most 5 ms and p99 at most 20 ms;
- semantic-node reduction versus v1 shadow: at least 70%;
- active effective storage/raw ratio: at most 80%;
- legacy-cohort effective bytes versus estimated v1 refs: at least 25% lower;
- full snapshot/object round trip: 100% for checked manifests;
- manual blind review: v2 is better or equal in at least 80% of sampled
  recoveries, strictly better in at least 60%, and worse in at most 10%;
- curated Canvas search p95 remains under 100 ms and indexed all-scope search
  p95 remains under 500 ms in the separate retrieval benchmark.

The physical-byte comparison counts every manifest and only the first write of
an object deduplicated by content address. For each successful, non-duplicate
callback, the active raw denominator is
`active_object_raw_bytes + active_embedded_raw_bytes`; the effective physical
numerator is `active_manifest_bytes + first-write text-envelope stored bytes +
active_embedded_stored_bytes`. The plugin records all externalized binary
logical bytes in `active_embedded_raw_bytes`, but adds an embedded object's
physical `stored_bytes` only when that event is its first write (`reused: false`).
The reporter applies the same accounting to the legacy cohort. Duplicate event
callbacks remain visible to hard gates but do not recompute capacity totals.
The v1 estimate uses the exact shadow decision and the legacy 50,000-character
ref cap. Reporter deduplication is limited to quality and capacity aggregates;
hard-gate counters always inspect every valid selected metric row, including
conflicting duplicate callbacks.

## Review rules

- **PASS** — all hard gates pass on the live soak, minimum duration/volume is
  met, and every decisive-improvement gate passes. A source replay PASS is
  candidate evidence only, not live retirement evidence. After the live PASS,
  switch to `v2_active`, stop the v1 shadow, and retain the rollback artifact
  for a bounded burn-in window.
- **HOLD** — safety and integrity pass, but sample size or one or more quality
  gates do not. Keep the reverse shadow and optimize the failed dimensions.
- **FAIL** — any hard functional/integrity gate fails. Switch to `off` for a
  capture or integrity regression, or `legacy_active_safe` for a non-security
  operational regression; preserve evidence and investigate before resuming.

Retiring v1 means disabling its runtime shadow and removing it from the next
candidate after the rollback-retention window. Historical v1 canvases are not
rewritten or deleted by this rollout.

## Verification sequence

1. Run focused snapshot/core/plugin tests.
2. Replay a stratified historical sample plus secret, data URL, duplicate,
   failure, verification, mutation, and concurrent synthetic fixtures.
3. Run the full repository test suite.
4. Install the package/plugin into owner-controlled Hermes paths.
5. Verify source/deployed SHA-256 parity when a live candidate is installed.
6. Restart through the native gateway lifecycle, then use a fresh session for
   one real tool event and inspect the manifest, metric, and semantic behavior.
7. Enable the quiet watchdog and one-shot 48-hour live-soak review.

The replay writes only to a newly created `/tmp/context-canvas-v2-replay-*`
directory. It is candidate evidence only, not live retirement evidence, and it
never modifies the live cache or Canvas store.
