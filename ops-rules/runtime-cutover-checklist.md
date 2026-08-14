# Runtime Cutover Checklist

Use this checklist when replacing a running agent service, supervisor, worker,
plugin host, or sidecar with a reviewed candidate.

The goal is a reversible transition whose candidate, authority, rollback material,
and final runtime can all be read back. A successful build or restart command is
not enough.

## 1. Bind the candidate and the maintenance window

Before touching the live target, record:

```text
Candidate: <immutable version, commit, or artifact id>
Candidate digest: <sha256 or equivalent>
Live target: <service and deployment root>
Window: <start and end>
Cutover owner: <role or operator>
Abort owner: <role or operator>
Rollback authority: <role or operator>
Allowed probes: <bounded read-only or approved live checks>
```

Require an immutable candidate identity. A branch name, mutable file path, or
"latest" tag is not a sufficient fence. Confirm that no second operator,
deployment, repair, or scheduler owns the same target during the window.

## 2. Freeze a live baseline

Capture the smallest baseline needed to distinguish the old runtime from the new
one:

- executable and configuration identities;
- process ID plus native process start identity or service generation;
- supervisor, worker, scheduler, and sidecar ownership;
- queue, lease, outbox, or in-flight work counts that must survive the change;
- storage or memory owner pointers that must not silently switch;
- one pre-cutover health result and one real domain read/query.

Use exact process instances, not PID alone. If a required owner or state cannot be
observed reliably, stop and repair the observability gap before cutover.

## 3. Build current-window rollback material

Create rollback material from the live baseline in the same maintenance window.
It should include the exact prior executable, required configuration, owner
metadata, and consistency-safe snapshots of mutable state.

Verify the rollback set before continuing:

- write a manifest from the files or snapshots actually captured;
- read back sizes and digests;
- open or parse every required artifact;
- run a bounded restore rehearsal in an isolated target when practical;
- keep the rollback set available until post-cutover verification is complete.

Do not rely on an older backup merely because it has a familiar name. Do not
prune the prior usable generation until the new runtime and its rollback path
have both been verified.

## 4. Compile the transaction before mutation

Write the cutover as an ordered transaction instead of an improvised command
sequence. A useful phase model is:

```text
prepared -> held -> backed-up -> applied -> restarted -> verified
                                             |             |
                                             +-> rollback <-+
```

For each phase, define:

- exact preconditions and candidate/target digests;
- the one authority allowed to advance it;
- the command or API action;
- the durable receipt written after readback;
- the rollback entry point;
- the conditions that block further progress.

Persist phase changes in an append-only or monotonic ledger. Make retries
idempotent where possible, and reject stale transactions, mismatched candidates,
or a target that changed after preparation.

## 5. Hold work and re-read the preconditions

Immediately before replacement:

1. stop accepting new work or place the target in its supported hold mode;
2. drain, preserve, or explicitly disposition in-flight work;
3. prove the expected process and lifecycle owners are the only live owners;
4. re-read candidate, live-target, and rollback identities;
5. abort if any fenced value differs from the prepared transaction.

A preflight from earlier in the day does not prove the current window is safe.
The final precondition read must happen next to the mutation.

## 6. Apply the smallest atomic change

Prefer an atomic replace, generation switch, or service-manager operation over a
series of partially visible writes. Keep source, staging, live, and rollback
roots distinct.

After applying the candidate:

- sync or flush the durable artifact when the platform supports it;
- start exactly one authoritative supervisor generation;
- reject duplicate launch owners or unresolved legacy wrappers;
- record the observed candidate digest before moving to verification.

If the apply phase fails, do not continue to restart or canary steps just to see
what happens. Enter the documented rollback path.

## 7. Verify from outside the replaced process

The old service and any helper it launched may share one termination boundary.
Run post-cutover verification from an independent supervisor, timer, CI job, or
fresh process.

Require all of the following before declaring success:

- a new and exact process identity or service generation;
- the live executable and configuration match the candidate;
- the expected supervisor, worker, scheduler, and sidecar owners are singular;
- readiness or health passes;
- one real domain read/query succeeds;
- queue, outbox, lease, and storage-owner invariants match the baseline policy;
- rollback material is still present and readable.

A restart request with no independent result remains `pending`. A healthy process
with the wrong bytes or wrong owner remains a failed cutover.

## 8. Commit the transition or roll it back

Use an explicit decision gate:

- **Commit** only when every required readback and domain probe passes.
- **Rollback** on candidate mismatch, missing owner, duplicate owner, state drift,
  failed domain behavior, or an incomplete independent verifier.
- **Hold for investigation** when evidence is ambiguous and rollback would create
  greater immediate risk.

Rollback should restore the exact prior generation, restart it through its normal
authority, and repeat the same independent identity, health, domain, and state
checks. "Rollback command returned zero" is not restore evidence.

## 9. Close out with a compact receipt

Keep detailed logs in the local evidence store. A share-ready closeout can stay
small:

```text
Candidate: <immutable id and digest>
Baseline: <prior id and essential state>
Rollback: <manifest and restore rehearsal result>
Transaction: <final phase and ledger location>
Runtime readback: <process/generation and live digest>
Domain probe: <bounded result>
State invariants: <queue/owner continuity result>
Decision: <committed, rolled back, or held>
Residual gaps: <none or bounded follow-up>
```

Do not include credentials, account identifiers, private data, raw messages, or
machine-specific paths in a public report.
