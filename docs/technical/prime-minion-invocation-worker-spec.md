# Prime Minion 0.3.0 invocation-worker lifecycle specification

Status: `SPECIFIED — correction-batch probes passed; implementation authorized`
Specification generation: `prime-minion-invocation-worker-v2`
Base source: `22c69b7e01b6d743e59b1b5dccd03c45303610bc`
Target plugin version: `0.3.0`

## 1. Intent and scope

Each Prime Minion turn MUST run under one invocation-local lifecycle owner that contains the relay, embedded Prime RPC process, every descendant, and all ephemeral runtime files. Normal completion, error, timeout, cancellation, spawn/transport failure, hard Hermes-parent death, and detached/TERM-ignoring descendants MUST converge to an invocation-bound terminal state with no invocation process, zombie, relay listener, private mount, or ephemeral runtime residue.

The public contract remains:

- `delegate_minion`, `minion_session_status`, and `close_minion_session`;
- ephemeral and resumable turns;
- explicit requested/effective provider, model, and reasoning effort;
- transcript resume without resident process resurrection;
- existing session schema and public state names.

### In scope

- one per-invocation Python worker that is PID 1 in a private PID namespace and mount owner in a private mount namespace;
- one worker-private tmpfs for `agent-home`, `tmp`, and other ephemeral state;
- relay and process-local embedded Prime RPC as children of the same worker;
- one authoritative Hermes-liveness/control pipe, one framed request channel, one framed final-result channel, and bounded diagnostics;
- cancellation-resistant parent spawn/finalization;
- cleanup-before-terminal-success ordering;
- Linux/WSL capability verification and fail-closed unsupported-host behavior;
- exact lifecycle regressions, process probes, fresh-install acceptance, and publication evidence.

### Non-goals

- Prime is not an OS sandbox. It retains the invoking user's ordinary authority in the selected workdir, session transcript directory, and other same-user-readable filesystem paths.
- Nested user namespaces remove mount-administration capability in the worker's owning user namespace; they do not claim filesystem, network, credential-file, or data-confidentiality isolation.
- No network namespace, seccomp policy, container, machine-wide cgroup, systemd/s6 service, shared daemon, process-name scan, global socket shutdown, or Hermes-core change.
- No new durable database, grant store, scheduler, recovery service, public MCP tool/schema, or automatic replay guarantee.
- No live cutover. Installation and acceptance use a fresh projection from exact committed bytes.
- Hosts without the complete `linux-user-mount-pid-v1` capability profile are unsupported and fail closed.

## 2. Necessity and architecture decision

Observable outcome: a turn may report terminal success only after every invocation-owned process and ephemeral resource is gone, without deleting/stopping another invocation or a foreign filesystem object.

- Delete/decline: safe but does not meet the owner's explicit Prime readiness requirement.
- Manual cleanup: cannot cover hard parent death or unattended cancellation.
- Extend the old guard: rejected after the frozen 0.2.1 candidate received a batch-complete BLOCKED verdict and a source-confirmed nested-subtree custody risk.
- Host supervisor/global cgroup: rejected because it adds prohibited host/global authority and WSL deployment seams.
- Selected: `PLATFORM_PRIMITIVE + EMBED` — one ephemeral invocation worker using Linux user, mount, and PID namespaces, with no new durable worker state.

Complexity budget:

- one short-lived `unshare` launcher helper and one namespace PID1 worker per turn;
- one private mount/PID namespace and one tmpfs per turn;
- one fixed persistent empty mount anchor plus one bootstrap-bound parent/anchor identity receipt per installed projection, never provisioned, recursively cleaned, or deleted per invocation;
- one liveness/control pipe, one framed request stream, one framed final-result stream, and one bounded diagnostic stream;
- two worker-owned workloads: relay and embedded Prime RPC, each behind a nested user-namespace launcher;
- no new persistent owner, registry, daemon, schema, or public interface.

## 3. Process, PID, and FD topology

```text
Hermes Python process (host PID H)
  owns session lease/manifest and exact subprocess handle U
  owns control write FD CW (the only writer)
  owns request writer and concurrent result/diagnostic readers
  execs:
    unshare launcher helper U (host PID U; outside new PID namespace)
      inherits only control read FD CR plus stdin/stdout/stderr
      forks/execs:
        invocation_worker.py W (host PID W; namespace PID 1)
          inherits CR plus stdin/stdout/stderr and pre-opened mount-anchor FD A
          launches:
            nested-user launcher -> relay
            nested-user launcher -> embedded Prime RPC
```

Normative ownership matrix:

| Actor | Lifecycle authority | Control FD | Mount-anchor FD A / runtime FD R | Real provider credential | Durable paths |
|---|---|---|---|---|---|
| Hermes parent | session + launcher handle + public result | **CW only** | opens A and passes it; closes its copy after spawn settlement | existing Hermes/Codex custody | manifest/transcript owner |
| `unshare` helper | bootstrap/wait only; no independent verdict | CR inherited solely to pass to W; never CW | A inherited solely to pass to W | none | none required |
| worker PID1 | sole invocation lifecycle and cleanup verdict | **CR only** | A is the pre-mount target; R is opened and verified after mount | none | validated workdir/transcript paths |
| relay | provider translation only | none | no A; optional least-authority duplicate of R for private runtime access | may load existing credential abstraction; receives synthetic handoff input only where already required | no manifest authority |
| Prime | agent/RPC workload only | none | no A; least-authority duplicate of R for `PRIME_AGENT_HOME`/`TMPDIR` | no real credential is passed in request, environment, or FD; synthetic relay bearer only | validated workdir and optional transcript |

FD rules:

- `os.pipe2(O_CLOEXEC | O_NONBLOCK where supported)` creates the control pipe before launcher spawn.
- Hermes retains CW and passes only CR and A via an explicit `pass_fds` allowlist. CW is never in `pass_fds`; it remains `CLOEXEC` and is the unique writer.
- `unshare` passes CR/A through its exec/fork boundary. Worker validates that both refer to the expected FD kinds before any child spawn.
- Worker launches relay and Prime with `close_fds=True`. CR, A, worker result FD, and unrelated inherited descriptors MUST be absent. A child may receive only its explicit least-authority duplicate of post-mount R when private runtime access is required; that duplicate refers to tmpfs, not the host anchor inode.
- Relay/Prime receive only their documented stdin/stdout/stderr pipes and any exact transcript/workdir descriptors explicitly added later by the accepted implementation; every such addition requires a test.

Parent-loss authority:

- Control EOF without a prior `S` byte is the **only authoritative Hermes-parent-loss signal**.
- Worker MUST NOT compare `getppid()` to H; its direct parent is the `unshare` helper.
- PDEATHSIG, if used in W, protects only unexpected death of direct parent U and is defense in depth.
- U has no independent lifecycle state. If U dies, `--kill-child` and/or worker PDEATHSIG may accelerate teardown, but success still requires the parent-observed protocol and exit predicates.
- U never holds CW. Therefore Hermes death closes the unique writer even if U/W/relay/Prime remain alive momentarily, and W observes EOF.

## 4. Fixed mount anchor and private tmpfs

The installed projection/bootstrap creates one empty owner-only mount anchor, for example `<plugin>/.runtime/invocation-anchor`, mode `0700`, plus a `0600` identity receipt. The receipt binds the canonical path and exact device/inode/uid/mode of both the already trusted `0700` runtime parent and anchor. These are persistent installation structures, not per-invocation residue. Production has no caller-selected anchor path. They are never provisioned, chmod-repaired, recursively cleared, or deleted by turn cleanup.

Before spawn, Hermes:

1. opens the fixed runtime parent and identity receipt using `O_DIRECTORY/O_RDONLY | O_NOFOLLOW | O_CLOEXEC` as applicable;
2. requires the current parent and anchor to match the bootstrap receipt exactly before any mkdir, chmod, child creation, mount, spawn, or cleanup action;
3. opens the recognized anchor using `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`, verifies `fstat(A)` still matches both the receipt and `lstat(path)`, and passes A explicitly to U/W;
4. fails closed without touching content or mode when the receipt, parent, anchor, ownership, mode, type, path, or identity is missing, replaced, symlinked, or otherwise unrecognized;
5. never authorizes parent-side recursive cleanup of the anchor or whatever later occupies its pathname.

Worker:

1. installs its signal/control handling before resource mutation;
2. makes mount propagation private;
3. mounts a new `0700`, size-bounded tmpfs through `/proc/self/fd/A` using an in-process `mount(2)` binding; if an external mount helper is ever used instead, A is its sole additional explicit `pass_fds` entry and that custody is tested;
4. before any untrusted child starts, opens the canonical anchor pathname as post-mount runtime FD R, verifies via `fstatfs`/private mountinfo that R is the newly created tmpfs, and fails closed if the pathname was replaced before R was acquired;
5. creates `agent-home` and `tmp` only through R (for example `/proc/self/fd/R/...`), never through pre-mount A;
6. verifies mount identity from its private `/proc/self/mountinfo` before any child spawn;
7. starts relay/Prime under nested user namespaces that share the mount view but lack capabilities in the outer user namespace that owns the mount namespace; private runtime paths are rooted at explicit duplicates of R;
8. after all children are terminated and reaped, closes child duplicates of R, calls `umount2` against the mounted object with `MNT_DETACH`, and verifies the invocation mount is absent from private mountinfo;
9. closes R and A and exits. It does not remove the fixed anchor.

A top-level parent or anchor replacement before admission is rejected by the bootstrap identity receipt without mutation or worker spawn. A top-level anchor replacement after A admission but before R acquisition causes the R identity/mount check to fail before child spawn. Replacement after R acquisition is not followed: A binds the underlying mount target and R binds the mounted tmpfs. A replacement path/sentinel MUST survive. A foreign directory cannot be moved into the mounted tmpfs by direct `rename(2)` because the mount boundary returns `EXDEV`; worker cleanup never recursively traverses tmpfs or a host subtree. Prime may still copy/delete files using its ordinary same-user authority; that is outside the cleanup claim and not described as sandboxed.

Worker crash before/after mount needs no host-path deletion authority: kernel teardown of the private mount namespace releases the tmpfs, process exit closes A, and the fixed anchor remains by design. Parent waits for U to exit and never attempts ambiguous `rmdir(path)` recovery.

Parallel invocations may privately mount distinct tmpfs instances on the same anchor FD in distinct mount namespaces. They share no mount instance or runtime bytes.

## 5. Capability profile

Capability profile `linux-user-mount-pid-v1` requires all of:

1. Linux or WSL kernel with unprivileged user namespaces enabled;
2. `unshare` support for `--user --map-root-user --mount --pid --fork --kill-child=SIGKILL --mount-proc`;
3. private mount propagation;
4. owner-only anchor open with `O_NOFOLLOW` and `/proc/self/fd/<fd>` descriptor-targeted tmpfs mount;
5. size/mode-bounded tmpfs;
6. nested user namespace mapping that preserves invoking-user access to required same-user workdir/transcript/auth paths while denying mount administration in the outer user namespace;
7. `umount2(MNT_DETACH)` and private mountinfo verification;
8. worker PID1 signal/reap behavior and invocation-scoped `/proc` visibility.

Bootstrap/fresh-install preflight checks these in the listed order using disposable no-provider probes. Failure returns the stable public error category `unsupported lifecycle host: linux-user-mount-pid-v1 prerequisite <name> failed` before relay startup or Prime spawn. No process-group-only fallback exists.

## 6. Worker startup, signals, reaping, and cleanup

The worker entrypoint uses a minimal import/startup path and performs this order before mount or child spawn:

1. validate CR and A;
2. set CR nonblocking and register it with the event loop/selector;
3. install explicit handlers for `SIGTERM`, `SIGINT`, `SIGHUP`, and `SIGCHLD`;
4. initialize an empty direct-child table and bounded diagnostic drains;
5. read any already-pending control state: EOF/invalid byte means parent loss; `S` means intentional stop;
6. validate namespace PID is 1 and private `/proc` is mounted;
7. only then mount tmpfs and proceed.

Signal meanings:

- `SIGTERM`, `SIGINT`, `SIGHUP`, control `S`, or parent-loss EOF transition to `STOPPING`; parent loss also fixes the final verdict as failure.
- `SIGCHLD` triggers nonblocking `waitid(P_ALL, 0, WEXITED | WNOHANG)`/`waitpid(-1, WNOHANG)` until no exited child remains. Direct child identities/statuses are retained before reaping.
- Unexpected signal/protocol state fixes the verdict as failure and enters cleanup.

Cleanup algorithm:

1. stop accepting/producing operation success and preserve any provisional result only in memory;
2. request Prime RPC abort when possible;
3. send TERM to every remaining PID in the private PID namespace except PID1, using invocation-scoped `/proc` enumeration; all targets are invocation-owned;
4. concurrently drain child stdout/stderr/RPC and reap until the TERM deadline;
5. send KILL to every remaining namespace PID except PID1;
6. continue `waitid`/`waitpid` until all direct/orphaned children are reaped and private `/proc` contains only PID1, or the hard deadline expires;
7. detach and verify absence of the tmpfs mount;
8. close all child/control/anchor descriptors;
9. only then enter `EXIT_SUCCESS` or `EXIT_FAILURE` and emit at most one final frame.

Completion predicate before a success frame:

- provisional operation result is valid;
- relay and Prime direct children have exited with accepted status;
- every direct/orphan child has been reaped;
- invocation `/proc` contains only namespace PID1;
- worker listener/child pipes are closed;
- tmpfs mount is absent from private mountinfo;
- diagnostics are drained/redacted within bounds;
- no parent-loss, protocol, timeout, cancellation, cleanup, mount, or identity failure occurred.

The worker final frame is emitted while PID1 itself still exists. Hermes accepts it only after U/W subsequently exits with the expected code. If W is unexpectedly killed, U exits nonzero/no valid success pair exists; Hermes returns/records failure. Namespace PID1 exit causes the kernel to terminate any impossible-to-reap residual descendant, but such fallback can never convert the invocation to success.

Deadlines are constants exposed to tests: RPC abort grace, TERM grace, KILL/reap grace, unmount/diagnostic grace, parent cleanup margin, and hard-kill wait. Hermes MUST wait through the complete worker cleanup budget plus margin before killing U. No deadline is inferred from task timeout.

## 7. IPC framing and bounded drains

All protocol streams are binary and bounded:

| Channel | Framing | Maximum | Drain rule |
|---|---|---:|---|
| parent request -> worker stdin | 4-byte big-endian unsigned length + UTF-8 JSON object | 1 MiB | worker reads exact frame from startup; extra bytes/multiple frames fail protocol |
| worker result -> stdout | 4-byte big-endian unsigned length + UTF-8 JSON object | 2 MiB | Hermes starts retained reader immediately after spawn; exactly one frame then EOF |
| worker diagnostics -> stderr | unframed bytes | retain last 64 KiB | Hermes drains from spawn until EOF, discarding older bytes; never stops draining on overflow |
| relay readiness/stdout | readiness line max 64 KiB, then continuous bytes/events as defined by relay | retain only bounded needed records | worker drains from child spawn through exit |
| relay stderr | bytes | retain last 64 KiB | continuous drain through exit |
| Prime RPC stdout | JSON line max 4 MiB; total stream continuously consumed | bounded retained result/tool metadata only | worker reads from spawn through abort/exit; overlong/malformed lines are protocol failure |
| Prime stderr | bytes | retain last 64 KiB | continuous drain through exit |

Request/result schemas use exact required keys, reject unknown top-level protocol keys, reject invalid UTF-8/non-object JSON, and contain no raw credential. Task/result text exceeding the public bound fails before worker spawn or becomes a bounded error; it is never silently truncated as success.

Parent request writer, result reader, and diagnostic reader are retained concurrent tasks from spawn. Parent never waits for U/W exit before draining output. On malformed, oversized, multiple, missing, or trailing result frames, it continues drain to EOF, sends `S`, settles the worker, and returns protocol failure. Cancellation cannot abandon these tasks.

Worker child stdout/stderr drains begin at each spawn and continue during STOPPING/CLEANING. Overflow changes retention, not draining. Diagnostic text is redacted using the existing secret policy before public inclusion.

## 8. Nested-user identity, environment, paths, and credentials

Both relay and Prime launch in a nested user namespace using a tested mapping equivalent to `--user --map-root-user` from the outer worker user namespace. The transitive mapping presents the invoking host uid/gid for same-user filesystem access while the nested root lacks capabilities in the outer user namespace that owns the mount namespace.

Preflight and acceptance jointly prove:

- nested relay can import and use the existing Hermes/Codex credential abstraction and bind loopback;
- nested Prime can read/write the admitted workdir and, in resumable mode, the exact session transcript directory;
- both can read/write worker tmpfs paths supplied to them;
- neither can unmount or alter the worker-owned mount;
- neither inherits CR, A, worker result protocol descriptors, or unrelated Hermes FDs.

Environment policy:

| Child | Required/sanitized inputs | Forbidden propagation |
|---|---|---|
| relay | minimal locale/PATH/Python import environment, explicit Hermes home/auth abstraction inputs, one synthetic-bearer handoff channel, parent PID/control internal to relay if separately required | provider secret values in argv/log/result; worker CR/A/result FDs; unrelated token/key/password env |
| Prime | minimal locale/PATH/runtime variables, private `PRIME_AGENT_HOME`, private `TMPDIR`, loopback relay URL, synthetic bearer, admitted workdir/session paths, telemetry disabled | real provider key/token/password env/argv/request; worker CR/A/result FDs; unrelated Hermes FDs |

The wrapper's credential claim is precise: it does not transmit the real provider credential to Prime through argv, request, environment, or inherited FD. Because Prime is not a filesystem sandbox and runs with same-user file authority, this is not a claim that Prime is technically incapable of reading a credential file accessible to that user.

UID/GID mapping or required path/auth access failure is an unsupported-host/startup failure before Prime prompt execution. Relay and Prime use separate environment builders and FD allowlists.

## 9. Worker protocol and terminal verdict

The control protocol is:

- open/no byte: Hermes alive;
- exactly one `S`: intentional stop/cancellation;
- bare EOF before `S`: abrupt Hermes loss;
- any other byte, repeated `S`, bytes after `S`, or invalid FD: protocol failure; fail closed.

Worker states:

```text
STARTING -> MOUNTED -> RELAY_READY -> PRIME_RUNNING -> RESULT_READY
     \          \             \              \             \
      -------------> STOPPING -> CLEANING -> EXIT_SUCCESS | EXIT_FAILURE
```

Rules:

- `RESULT_READY` is provisional and in-memory only.
- No success frame is emitted until the completion predicate in section 6 holds.
- Parent loss always produces nonzero worker/launcher exit and no success frame.
- Intentional stop without a completed operation produces failure/interruption, not success.
- Cleanup failure outranks provisional operation success.
- Failure may emit one bounded error frame only after cleanup; missing/malformed/multiple frames remain failures.
- Hermes requires exactly one valid frame, expected worker/launcher exit code, settled reader tasks, and invocation-scoped residue predicates before acceptance.

## 10. Parent cancellation and worker-crash semantics

- Launcher spawn runs in a retained task so cancellation cannot lose a subprocess born before Python returns its handle.
- Parent finalization runs in one retained shielded task. The first and repeated cancellation requests are remembered; they do not cancel/replace the finalizer or erase its cleanup exception.
- On timeout/cancellation Hermes writes one `S`, closes CW exactly once, and waits through the worker's full cleanup budget plus margin. Hard kill of U is allowed only after that bound.
- On normal operation Hermes keeps CW open until valid frame + U exit settlement, then closes it exactly once.
- If U/W exits unexpectedly while Hermes lives, parent completes all drains, rejects any success lacking the exit/protocol pair, closes CW, and returns failure. There is no per-invocation host root to recover/delete.
- If Hermes dies, CW closes by kernel action. W observes EOF, fails closed, cleans children/mount, and exits. If W itself is killed before cleanup, PID/mount namespace teardown removes invocation processes/tmpfs; fixed anchor remains and no ambiguous path deletion occurs.

## 11. Durable session ordering and hard-death recovery

Existing schema is authoritative:

| Situation | Turn status | Manifest state | Durable owner/linearization |
|---|---|---|---|
| new admitted turn | `RUNNING` | `RUNNING` | `begin_turn()` under session lease |
| success after worker cleanup/exit predicates | `COMPLETED` | `IDLE` | `record_completed()` under same lease |
| failure while Hermes survives | `INTERRUPTED` | `INTERRUPTED` | `record_interrupted()` before releasing lease |
| hard Hermes death | remains `RUNNING` temporarily | remains `RUNNING` temporarily | kernel closes CW; no nonexistent parent write is claimed |
| next mutation lease after hard death | previous turn atomically becomes `INTERRUPTED`; requested new turn becomes `RUNNING`, or close becomes `CLOSED` after interruption repair | corresponding existing state | next lease holder after proving prior owner identity stale |

Lease owner identity MUST be invocation-bound, not bare PID: owner record includes host PID, `/proc/<pid>/stat` start time, and boot ID (or an equivalent stable tuple). PID reuse cannot preserve a stale lease. This is internal metadata and does not change the public session schema.

Read-only `minion_session_status` after hard death may report the last durable `RUNNING` state plus a public-safe `owner_alive`/staleness observation only if already allowed by the public schema; otherwise it reports existing fields unchanged and makes no repair. It never replays or mutates.

The next `delegate_minion`/resume lease holder verifies stale exact owner identity, marks the previous RUNNING turn interrupted with `previous owner disappeared before terminal lifecycle closure`, then begins the new turn in the existing atomic manifest write path. The next `close_minion_session` lease holder performs the same stale-turn repair before writing `CLOSED`. A live exact owner remains busy and cannot be stolen.

Before `record_completed()` all are required:

- valid worker success frame and expected U/W exit;
- parent reader/finalizer settlement;
- requested/effective route equality;
- valid Prime session identity and transcript file under the existing session root;
- no invocation process/listener/private mount according to section 12;
- no cleanup/protocol/cancellation failure.

The session lease remains held across worker execution and finalization. Any surviving-parent failure while the turn is RUNNING records interruption. No prior task is automatically replayed.

## 12. Invocation-scoped residue baseline

No global process-name kill or global socket/mount equality is used. Each invocation has an evidence identity containing:

- exact U subprocess handle and host PID/start time;
- worker host PID and PID-namespace inode obtained from a bounded startup/evidence channel that carries no terminal verdict;
- private mount-namespace inode and tmpfs mount ID from private mountinfo;
- worker-owned relay listener address/port receipt;
- control/request/result FD identities;
- session generation when resumable.

The startup/evidence receipt may be a separate bounded framed read-only channel or bounded structured diagnostic record. It cannot authorize success and must not add a second lifecycle owner. Missing/malformed evidence fails the applicable verification/acceptance cell.

Baseline-restored means:

- U/W exact identities absent;
- no process remains in the recorded PID namespace;
- recorded relay listener no longer accepts connections and its exact owner identity is absent;
- recorded mount namespace/tmpfs mount no longer exists;
- all invocation protocol FDs are closed;
- fixed anchor still exists or any pathname replacement remains untouched; there is no per-invocation root;
- for resumable mode, only intended manifest/transcript changes remain.

Tests/probes may inspect `/proc`, namespace inodes, exact child relationships, recorded listener, and recorded FDs for that invocation. They must not infer closure from a machine-wide absence of `prime-agent`, `tsx`, or a default socket.

## 13. Compatibility

- Public tool names/input-output schema, session schema version, state names, route matrix, and Prime pin `bc0fa7606abb3b7af0f765319518d255e6ae553d` remain unchanged.
- Successful durable state remains turn `COMPLETED` + manifest `IDLE`; no session `COMPLETED` state is added.
- Plugin version advances from base `0.2.0` to `0.3.0`. The blocked uncommitted 0.2.1 candidate is not reused or published.
- Existing sessions remain readable/resumable when Prime pin/workdir binding and transcript are valid. Internal lease-owner records may gain exact process identity fields.
- Installation is a fresh projection from an exact commit; the old installed projection/runtime is not reused.

## 14. Falsifiable acceptance matrix

### T0 — static and constructibility

- C01 capability profile checks each `linux-user-mount-pid-v1` prerequisite and fails before relay/Prime.
- C02 fixed anchor admission uses `O_NOFOLLOW`, fd/path identity, owner/mode checks; no recursive cleanup exists.
- C03 descriptor-targeted tmpfs mount and detach leave the persistent anchor empty/untouched.
- C04 nested child writes tmpfs/workdir/transcript as admitted but cannot unmount the outer-owned mount.
- C05 direct foreign-directory `os.rename()` into tmpfs fails `EXDEV`; sentinel remains.
- C06 FD topology probe proves Hermes is sole CW holder; U/W only CR/A; relay/Prime lack all lifecycle FDs.
- C07 request/result/diagnostic/RPC framing limits and continuous drains reject overlong, malformed, extra, and multi-frame data without deadlock.
- C08 Python/Node compile, Ruff/check/format, Plugin Doctor, manifest version, and diff gates pass.

### T1/T2 — controlled lifecycle links

- L01 normal success: one valid post-cleanup frame, accepted exit, exact invocation baseline restored.
- L02 operation error; L03 timeout: no success, bounded cleanup.
- L04 Hermes CW closes before worker handler install; L05 after handler; L06 after mount; L07 relay-ready; L08 Prime-running: nonzero/no success and invocation baseline restored.
- L09 U dies while Hermes lives; L10 W dies while Hermes lives: no success; namespace teardown; no path deletion.
- L11 unexpected control byte/repeated `S`; L12 intentional `S`: exact protocol semantics and exactly-once FD closure.
- L13 relay failure; L14 Prime spawn failure; L15 RPC transport failure; L16 malformed/oversized/multiple/missing final frame: parent error and no deadlock/residue.
- L17 detached `setsid()` + double-fork + TERM-ignoring descendant: TERM/KILL/reap predicate is discriminating; namespace ends cleanly.
- L18 PID1 startup/handler checkpoint signals and child-status attribution are tested.
- L19 top-level anchor pathname replacement before/after mount preserves replacement sentinel and never triggers rmdir/rmtree.
- L20 foreign nested transposition direct rename fails; cleanup never traverses foreign host content.
- L21 cancellation during U spawn recovers exact handle; cancellation/repeated cancellation during finalization cannot abandon cleanup or erase its failure.
- L22 Hermes waits full worker budget + margin before hard kill.
- L23 two concurrent invocations use separate namespace/mount identities; stopping one leaves the other functional.
- L24 relay/Prime env/FD/path matrix proves required access and forbidden credential/lifecycle handoff.
- L25 result/diagnostic/RPC overflow under success/error/cancel/crash remains bounded and deadlock-free.
- L26 resumable success persists only after cleanup/exit. Cleanup failure/cancellation yields turn+manifest interruption.
- L27 hard parent death leaves RUNNING durably; next resume/close exact stale-owner lease repair produces existing-schema transitions and never replays prior task.
- L28 PID reuse with different start time cannot keep/steal lease; live exact owner remains busy.
- L29 requested/effective route and sanitized diagnostic semantics survive every failure path.

### T3 — real process/runtime lifecycle

- P01 pinned-runtime bootstrap and capability profile verify from a fresh isolated projection.
- P02 credential-free 18-route get/set/readback matrix inside the real nested worker topology.
- P03 two-process resume preserves Prime session/transcript identity.
- P04 parallel A/B lifecycle and cleanup independence.
- P05 hard Hermes-parent death at pre-handler, mounted, relay-ready, and Prime-running checkpoints.
- P06 repeated normal/error/timeout/cancel runs restore recorded invocation process, listener, namespace, mount, and FD baselines.
- P07 anchor replacement/foreign rename adversarial probes preserve sentinels.
- P08 full plugin tests and actual repository CI-defined checks; unrelated base failures require exact-base reproduction.

### T4 — installed/live/public progression

Only after exact candidate bytes pass T0-T3 and one batch-complete independent Sol/high review:

- A01 commit and push exact branch bytes;
- A02 build a fresh isolated projection from that commit, preflight capabilities, and bootstrap the pinned runtime;
- A03 atomically install the fresh projection and restart the Hermes gateway;
- A04 real ephemeral and resumable `delegate_minion` acceptance with requested/effective route readback and post-turn invocation baseline;
- A05 status, resume, close, closed-state readback, timeout/cancel, and hard gateway-parent lifecycle drill;
- A06 PR, CI, merge, and GitHub `main` exact-byte readback.

## 15. Review and stop policy

- This v2 correction batch resolves the first specification review findings. It does not transfer implementation or release PASS.
- No second same-watermark specification review loop is required; main-session correction probes must close the changed contract cells before implementation.
- Formal source review starts only after candidate bytes and T0-T3 evidence are stable, with one batch-complete final wave.
- A blocking final finding stops publication.
- The old 0.2.1 findings/bytes remain historical evidence and transfer no PASS.
- Repeated same-cause failure in the worker/private-mount architecture stops implementation and returns to owner; it does not authorize another guard/system seam.

## 16. Evidence status and next action

Already observed on this WSL host with disposable no-provider probes:

- private user+mount+PID namespaces construct;
- tmpfs tears down with namespace exit;
- direct foreign subtree rename is blocked by the mount boundary;
- nested-user child accesses tmpfs but cannot administer the outer-owned mount;
- descriptor-targeted tmpfs mount and lazy detach work.

Correction-batch constructibility/source reconciliation completed before implementation:

1. PASS — unique CW ownership across H/U/W/child execs and fail-closed EOF;
2. PASS — fixed-anchor crash/replacement with A→post-mount-R custody and no host-path deletion;
3. PASS — PID1 signal/reap of detached TERM-ignoring descendants;
4. PASS — bounded framed IPC with multi-megabyte concurrent diagnostic/result draining and malformed/oversized/multiple-frame rejection;
5. PASS — nested-user required tmpfs/workdir/transcript/simulated-auth access, mount-admin denial, secret-env exclusion, CR/A exclusion, and explicit R inheritance;
6. PASS — existing session schema supports stale RUNNING → prior turn INTERRUPTED + next RUNNING without schema change. Stale close repair and exact lease owner identity are explicit implementation REDs.

These receipts open RED-first implementation. They prove constructibility only, not product/runtime acceptance.
