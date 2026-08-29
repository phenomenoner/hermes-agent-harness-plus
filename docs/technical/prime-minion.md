# Prime Agent Minion Bridge

## Purpose

The Prime Agent Minion Bridge lets Hermes Agent use Prime Agent as a bounded coding/research runtime without handing Prime the provider credential or final task authority. Version `0.3.0` is a standalone plugin: it does not patch Hermes core or add a daemon, database, system service, cgroup, network namespace, or machine-wide cleanup path.

Hermes owns task admission, explicit route selection, the session lease and manifest, provider credentials, integration, shared verification, and final claims. One short-lived invocation worker owns the relay, process-local embedded Prime RPC, all descendants, private tmpfs, cleanup, and terminal lifecycle verdict for exactly one invocation.

The complete normative contract is [`prime-minion-invocation-worker-spec.md`](prime-minion-invocation-worker-spec.md).

## Invocation topology

```text
Hermes parent H
  ├─ persistent session manifest + exact mutation lease
  ├─ control writer CW (sole writer)
  ├─ bounded request/result/evidence drains
  └─ direct Python launcher
       └─ unshare U: private user + mount + PID namespaces
            └─ worker W: PID namespace PID 1
                 ├─ bootstrap-bound anchor identity receipt
                 ├─ fixed-anchor FD A (mount target identity only)
                 ├─ worker-created 64 MiB tmpfs
                 ├─ verified post-mount runtime FD R
                 ├─ nested-user loopback Responses relay
                 └─ nested-user embedded Prime RPC process
                      └─ all setsid/double-fork descendants are adopted by W
```

The launcher sets a Linux parent-death signal before executing `unshare` and rechecks its direct parent. This only protects the short window before W installs its own handlers. Hermes-parent loss is authoritatively represented by EOF on the invocation control pipe, not by a PPID comparison.

W is namespace PID 1 and the only lifecycle authority. It handles TERM/INT/HUP, reaps adopted orphans, interrupts an active relay/RPC operation, escalates TERM to KILL within fixed budgets, drains child output, detaches its exact tmpfs, closes descriptors, and emits one bounded terminal result.

## Mount and path custody

Bootstrap creates one fixed owner-only `0700` mount anchor and a `0600` identity receipt as installation structure. The receipt binds the canonical anchor path plus the exact device/inode/uid/mode of its `0700` parent and anchor. An invocation never creates, chmods, recursively deletes, or replaces any part of that route; production rejects caller-selected anchor paths.

1. Hermes opens the fixed parent and receipt with `O_NOFOLLOW`, then requires both current parent and anchor identities to match the bootstrap receipt before any mount, child creation, or worker spawn.
2. Hermes opens the recognized anchor as FD `A`, verifies `fstat(A)` still matches the canonical pathname, and binds that identity into the request.
3. W verifies the same anchor after namespace handoff and mounts a private `size=64m,mode=0700,nosuid,nodev,noexec` tmpfs through the descriptor target.
4. W opens the mounted path as post-mount FD `R` and verifies that `R` belongs to the exact newly created mount ID.
5. Private agent-home and tmp paths use only `R`-derived descriptor paths. `A` is never used for private data because a pre-mount descriptor points below an overmount.
6. Relay and Prime receive only an `R` duplicate plus their stdio. They do not receive `A`, the control reader, result/evidence writers, or unrelated lifecycle descriptors.
7. Cleanup detaches through `R`, checks that the exact mount ID disappeared, then proves the fixed outer anchor is empty when its original identity still exists.

A missing receipt, replaced parent or anchor, symlink, mode mismatch, or unrecognized current-live state fails closed without changing content/mode and before worker spawn. Path replacement after `R` acquisition cannot transpose custody to a foreign subtree. Replacement after parent admission but before W acquires `R` fails closed.

## Bounded protocol and evidence

Hermes sends one exact-key, length-prefixed JSON request. Fake relay/Prime commands are not part of that request schema; test fixtures are available only through an internal launcher-only seam.

W emits:

- bounded framed lifecycle evidence (`handlers`, `mounted`, `relay_ready`, `prime_running`, `result_ready`, `cleanup_complete`);
- a bounded terminal JSON result only after cleanup; and
- bounded diagnostic tails on stderr.

Hermes drains result and diagnostics/evidence concurrently. It rejects malformed, oversized, duplicate/trailing, or out-of-order frames. Cancellation is shielded until the invocation finalizer settles; repeated cancellation sends the intentional-stop marker at most once. If W/U exceeds the full worker cleanup budget plus parent margin, Hermes terminates only the exact captured launcher identity and preserves cleanup failure instead of laundering it into cancellation or success.

A `completed` result is accepted only after all of these observations hold:

- W returned valid ordered evidence and a valid bounded result;
- W and U exited and their exact process identities disappeared;
- the task-owned relay listener is closed;
- namespace descendants are gone and were reaped;
- the exact tmpfs mount is absent;
- invocation protocol descriptors are closed; and
- the fixed-anchor baseline is restored.

## Embedded Prime RPC

The worker starts Prime process-locally with:

```text
node --import tsx embedded_rpc.mjs --mode rpc ...
```

Using Node as the launcher preserves the inherited runtime descriptor. The `tsx` CLI is intentionally not the launcher because it closes or reuses non-stdio descriptors before Prime starts. `embedded_rpc.mjs` loads the pinned Prime `main.ts` and injects only `prime_extension.mjs` through the explicit extension-factory API.

The worker uses Prime RPC for initial route readback, explicit model/thinking updates when needed, effective-route readback, prompt/abort, event consumption, and final route readback. Credential-free verification uses the same embedded RPC and worker topology with an internal `route_probe` operation that never sends `prompt`. In resumable probe mode it uses Prime's official no-provider `set_session_name` RPC to create a real transcript before a second process resumes it.

## Credential boundary

The nested relay resolves authenticated OpenAI Codex credentials from Hermes and binds an ephemeral loopback-only listener. Prime receives only:

- the loopback relay URL;
- a fresh synthetic bearer;
- its private `R`-derived home/tmp paths; and
- a role-specific environment allowlist.

Real provider credential values are not sent in Prime argv, RPC request, environment, or inherited descriptors. The relay may receive the minimum Hermes auth-home and TLS paths it needs; Prime does not. The relay validates the synthetic bearer before replacing it with the Hermes-owned credential and supports only the approved HTTP/SSE Responses path.

This is credential containment and lifecycle containment, not an operating-system sandbox. Prime retains the invoking user's workspace authority. Use a bounded canonical workdir and normal Hermes approval policy.

## Resumable sessions and close fence

Hermes keeps a separate owner-only manifest and exact session lease. A lease owner is bound to PID, `/proc/<pid>/stat` start time, boot ID, and a random lease token; bare PID liveness is insufficient.

```text
manifest IDLE -> turn RUNNING -> turn COMPLETED + manifest IDLE
                           \-> turn INTERRUPTED + manifest INTERRUPTED
IDLE/INTERRUPTED -> CLOSED
```

A turn records its exact lease owner. If a process dies while a turn remains `RUNNING`, the next exact mutation lease may close-fence it to `INTERRUPTED` only after proving the prior owner identity is stale. A live or mismatched prior owner is never silently repaired. `COMPLETED` is persisted only after worker terminal acceptance; an uncertain prompt or tool effect is never replayed automatically.

Public status and close results expose opaque session identity and sanitized state, never the internal transcript path or raw failure details.

## Host capability profile

The required profile is `linux-user-mount-pid-v1`. Before provider work, the standalone probe checks:

- Linux user/mount/PID namespace construction and private propagation;
- worker PID 1 with a private `/proc`;
- descriptor-targeted tmpfs mount, exact mount ID, `0700` mode, and bounded size;
- post-mount `R` access and descriptor-targeted detach;
- nested same-user read access;
- absence of leaked `A`/pipe identities; and
- denial of nested mount administration over the outer mount namespace.

Missing or denied primitives return a stable unsupported prerequisite. There is no process-group-only fallback.

## Verification layers

1. T0: Ruff, compileall, Node syntax, manifest/import/registration through `hermes plugins doctor`.
2. T1/T2: framed IPC, RPC flooding, environment/FD allowlists, exact lease identity, stale-RUNNING close fence, cancellation, malformed result, and strict request schema.
3. T3: real private namespaces, PID1 reaping of detached TERM-ignoring descendants, anchor replacement, parent death before handlers and at lifecycle checkpoints, repeated and parallel invocation closure.
4. P02: pinned embedded Prime RPC route/readback matrix for three models × six efforts with no prompt/provider request.
5. P03: credential-free two-process Prime transcript resume through two complete worker lifecycles.
6. Optional authenticated source-candidate smoke: one bounded tool call and two-turn resume.
7. `FULL` product acceptance after hash-bound source closure: exact-byte clean install, gateway registry pickup, one authenticated bounded delegation, one authenticated resumable/RLM continuation, effective-route readback, and task-owned residue absence.

These layers support the product claim; they are not a host-security certification. Existing namespace, tmpfs, IPC, and anchor mechanisms are bounded lifecycle scope guards. Do not add broader isolation, persistence, soak, or review machinery unless a real product-path failure requires it.

The pinned Prime source is `0.8.1` at `bc0fa7606abb3b7af0f765319518d255e6ae553d`. Bootstrap preserves its lockfile and reports npm audit results; it does not run `npm audit fix` or silently move the pin. Namespace containment does not mitigate vulnerabilities in Prime or its dependency tree.
