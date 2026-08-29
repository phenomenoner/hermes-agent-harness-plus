# Prime Minion invocation-worker WAL

## 2026-08-29 — successor architecture start

Objective:
- Deliver Prime Agent Minion FULL lifecycle readiness and publication with one per-invocation worker that owns relay, embedded Prime, descendants, tmpfs, cleanup, and terminal verdict.

Commitment floor:
- Linux/WSL lifecycle closure for normal, error, timeout, cancellation, spawn/transport failure, hard Hermes-parent death, detached/TERM-ignoring descendants, repeated and parallel invocations.
- Invocation-bound cleanup must not stop another invocation or delete a foreign path/subtree.
- Resumable `COMPLETED` is persisted only after worker/relay/Prime cleanup, worker exit, and root absence.
- Fresh-install exact-commit acceptance precedes PR/merge/publication.

Scope and authority:
- Authorized: specification, implementation, tests, bounded no-provider probes, exact-commit branch publication, fresh install, gateway restart, live acceptance, PR/CI/merge after gates pass.
- Forbidden: Hermes-core/system-service seam, machine-wide kill, shared daemon, global cgroup/process-name scan, new DB/public tool/schema, live cutover, reuse of old installed projection, AAR work.

Repository identity:
- Worktree: isolated successor worktree (host path intentionally omitted)
- Branch: `feat/prime-minion-invocation-worker-lifecycle`
- Base HEAD: `22c69b7e01b6d743e59b1b5dccd03c45303610bc`
- Created clean from base while the old dirty worktree remained untouched.

Frozen predecessor generation:
- Old branch: `fix/prime-minion-owned-lifecycle`
- Old dirty candidate: 13 paths, digest `3e27ded13ddbb9d44d20d8ccb158ba5856ab139da20c89a05ce2b8ecd02a3487`
- Formal verdict: `BLOCKED`, two HIGH findings and batch-complete finding set.
- Additional source-confirmed risk: recursive cleanup had no nested-subtree provenance fence.
- Stop decision: do not patch, commit, install, or publish that candidate.

Necessity decision:
- Deletion/manual cleanup do not meet the owner requirement.
- Existing guard patching is retired after repeated lifecycle blockers.
- systemd/s6/cgroup add prohibited host/global authority.
- Selected: platform user+mount+PID namespaces with an embedded ephemeral invocation worker and no new durable mechanism.

Specification authority:
- `docs/technical/prime-minion-invocation-worker-spec.md`
- Target plugin version: `0.3.0`
- Public tools/session schema/Prime pin remain unchanged.

Constructibility evidence:
- PASS: private user+mount+PID namespace.
- PASS: tmpfs disappears on namespace exit.
- PASS: host foreign subtree direct `rename(2)` into tmpfs fails across mount.
- PASS: nested-user child writes tmpfs but cannot unmount parent-owned mount.
- PASS: tmpfs mount through `/proc/self/fd/<root_fd>`.
- PASS: lazy unmount restores an empty outer inode.
- One discarded oracle used GNU `mv`, which copied then deleted across filesystems; it was replaced by direct `os.rename()` and is not product evidence.

Current state:
- New worktree exists and is clean except for this specification/WAL generation.
- No implementation, test, runtime installation, gateway change, provider call, commit, push, PR, or publication has occurred in this successor lane.
- AAR is separately PAUSED with a hash-bound checkpoint; do not touch it.

Verdict vector:
- architecture constructibility: PASS for local primitives only
- specification: drafted, pending bounded independent review
- implementation: NOT STARTED
- T0–T3 source/runtime evidence: NOT STARTED
- formal review: NOT STARTED
- fresh install/live acceptance: NOT STARTED
- publication: NOT STARTED

Active blockers and stop conditions:
- Implementation stays closed until the specification receives a bounded read-only review and any contract-level blocker is resolved once.
- If the worker/private-mount architecture fails the same ownership/lifecycle invariant after one bounded implementation repair, stop and return to owner.
- No terminal success may be recorded before full cleanup.

Next safe action:
1. Read-only Terra/high review of the specification against base source, old blocker batch, and constructibility evidence.
2. Main-session reconcile and freeze the accepted specification bytes.
3. Implement RED-first lifecycle tests and the smallest worker/parent vertical slice.

Claims not yet proven:
- worker protocol correctness;
- PID1 reaping and detached descendants;
- parent-loss closure at every startup phase;
- relay/Prime co-ownership;
- resumable ordering under cancellation/cleanup failure;
- real pinned runtime, provider route, installed gateway, or public repository acceptance.

## 2026-08-29 — specification review and coherent correction batch

Review identity:
- Delegation: bounded independent read-only specification review (local execution ID omitted)
- Reviewer: Terra/high, strict read-only constructibility/contract review
- Verdict: `SPEC_BLOCKED`
- Review changed files: 0
- Source HEAD remained `22c69b7e01b6d743e59b1b5dccd03c45303610bc`; only pre-existing spec/WAL were untracked.

Batch-complete findings:
1. `unshare --fork` helper made PPID/PDEATHSIG wording inconsistent with Hermes-parent liveness and lacked exact FD custody.
2. Per-invocation outer root had an unobservable crash-before-identity-receipt cleanup gap.
3. PID1 signal/reap/termination algorithm was underspecified.
4. Request/result/diagnostic/RPC streams lacked exact limits and concurrent-drain semantics.
5. Nested-user UID/GID, auth/transcript access, environment, and FD boundaries were not testable.
6. Durable `COMPLETED` terminology conflicted with existing turn/session states and hard-death recovery ownership.
7. Capability profile and residue baseline were not reproducibly invocation-scoped.

Disposition:
- This is the first specification review, not a repeated implementation failure.
- One coherent contract correction is authorized; implementation remains closed until its six corrected constructibility/source cells pass.
- No second same-watermark spec review loop will be started.

Specification successor:
- v1 replaced by `prime-minion-invocation-worker-v2` in the same canonical spec file.
- Per-invocation host root was removed. A fixed owner-only mount anchor remains installation structure and is never recursively cleaned/deleted per turn.
- Hermes control EOF is the only parent-loss authority; worker PPID is not compared with Hermes. PDEATHSIG applies only to direct launcher death as defense in depth.
- PID1 handler/reaper/TERM-KILL/unmount completion predicate is normative.
- Request/result use bounded length-prefixed JSON; all output/diagnostic/RPC channels are drained concurrently from spawn.
- Explicit H/U/W/relay/Prime PID/FD/environment/path table added.
- Durable success is turn `COMPLETED` plus manifest `IDLE`; hard death temporarily leaves `RUNNING`, repaired by the next exact stale-owner mutation lease.
- Lease identity must include PID start time and boot ID (or equivalent), not bare PID.
- Capability profile is `linux-user-mount-pid-v1`; baseline is invocation-scoped.

Current gate:
- specification text: `SPECIFIED`
- correction constructibility/source cells: PASS
- implementation: OPEN for RED-first isolated successor worktree only

Correction receipts:
- `control_fd_topology_and_eof=PASS`
- `fixed_anchor_crash_and_replacement=PASS`
- `pid1_signal_reap_detached_descendant=PASS`
- `framed_ipc_concurrent_drain_and_limits=PASS`
- `nested_user_access_env_fd_boundary=PASS`
- `durable_existing_schema_recovery_constructible=PASS`
- Expected implementation REDs retained: stale RUNNING repair on close; exact lease PID/start-time/boot-ID identity.
- A disposable host-local probe supplied constructibility evidence only; its behavior was translated into repository product tests before implementation closure.

Important probe correction:
- A pre-mount anchor FD bypasses an overmount when accessed through its own `/proc/self/fd/A` magic link. The accepted design therefore uses A only as the descriptor-bound mount target, then opens/verifies post-mount runtime FD R before child spawn; all private runtime access uses R. Replacement after R acquisition preserves both tmpfs custody and the foreign pathname.

Next safe action:
Assign one exclusive implementation owner for the tightly coupled worker/parent/session seam. Add RED product tests first, then implement the smallest vertical slice. Main session performs diff review and centralized T0-T3 verification. Any repeated same-cause ownership/lifecycle contradiction stops and returns to owner.

## 2026-08-29 — source candidate ready for exact-byte review

Current state:
- Plugin version is `0.3.0`; Prime remains pinned to `0.8.1` at `bc0fa7606abb3b7af0f765319518d255e6ae553d`.
- The worker/launcher/session implementation, repository probes, lifecycle tests, and technical/ops documentation are present in the isolated successor branch.
- No source commit, push, fresh installed projection, gateway pickup, live dispatch, PR, merge, or FULL readiness claim has occurred yet.
- A previously proposed host-restart handoff after closeout is cancelled and is not part of the remaining completion sequence.
- AAR remains separately PAUSED; Prime readiness does not amend that gate.

Source evidence on the current executable epoch:
- Plugin suite: `39 passed`.
- Full repository suite from an owner-only fresh extraction of the staged candidate tree: `174 passed`.
- Ruff, compileall, Node syntax, and `git diff --check`: PASS.
- Plugin Doctor: standalone manifest `0.3.0`, runtime discovery/import/registration PASS, three tools registered.
- Lifecycle capability profile `linux-user-mount-pid-v1`: PASS.
- Local runtime pin verification: Prime `0.8.1` at the exact pinned commit: PASS.
- Credential-free embedded Prime route/readback matrix: 18/18 routes, no prompt/provider request, cleanup predicates true.
- Credential-free two-process resumable readback: same real Prime session/transcript across two complete worker lifecycles, no prompt/provider request, cleanup predicates true.
- Post-interruption process readback: no invocation worker, namespace launcher, embedded Prime, or probe process remained.

Candidate/review policy:
- Candidate bytes must include all intended tracked and new plugin/spec/test/documentation files and exclude `.runtime`, caches, logs, credentials, and host-local receipts.
- Formal review is read-only and blind to desired verdict. One primary reviewer plus one narrow lifecycle/coverage auditor is the maximum useful shape; main session owns synthesis and any repair.
- Candidate-byte changes after review invalidate the dependent review cells and require a newly bound successor review before installation.

Remaining gates:
1. Freeze and bind exact source candidate; run the final current-byte P02/P03 and public-safety checks.
2. Complete blind review and narrow coverage audit; synthesize a batch-complete verdict.
3. Repair only supported in-scope findings, then rebind and reacquire invalidated evidence/review.
4. Commit and push the reviewed source candidate; verify remote branch identity.
5. Perform clean-install-only pickup of exact bytes, restart the gateway once, then execute bounded registry/live lifecycle acceptance and residue checks.
6. Open PR, require green CI, merge, and read back GitHub `main` exact bytes.
7. Append final WAL/handoff status. Prime may be marked FULL only if every required installed/live/publication gate is green.

## 2026-08-29 — formal source review wave 1 closed BLOCKED

Frozen candidate and reports:
- Candidate tree: `4fe13dd2dea3e78ef79bd237b60acdd27ba0f5c0`.
- Sealed primary report SHA-256: `f375030ff886ecf88dcf943b3658baac94bd67c6ef959f6a4489f1fbb353a098`.
- Narrow audit SHA-256: `3d6a18574eeed3248b5eff8e85c406180bf74981c0132d38edaadf338ec0ad23`.
- Main-agent synthesis SHA-256: `829d9b5c1e00a6f70305b89d4e40be4fe49a671dc44a2e4333d1f9777b5b812d`.
- Official synthesis validator: valid, no errors or warnings; actual verdict `BLOCKED`; finding set `AUDITED_BATCH_COMPLETE`.

Accepted finding batch:
1. `F01-UNBOUND-ANCHOR-PREIMAGE` — HIGH, blocking. Admission derives trust from the current anchor pathname. An existing same-uid replacement can be chmod-mutated or mounted before installation identity is established. The narrow audit extended the same finding family to `path.parent`: current code may mkdir/chmod a caller-selected ancestor before validating the anchor.
2. `F02-TMPFS-DOC-SIZE` — LOW, nonblocking. Public docs state 8 MiB while production mounts `64M`.

Repair contract:
- Eliminate caller-selected production anchor paths.
- Provision and bind the fixed anchor during explicit bootstrap/test setup; normal admission is read-only until exact trusted anchor-route identity is proven.
- Before successful admission, do not mkdir, chmod, create beneath, mount, spawn, or clean the current anchor or any mutable ancestor.
- Missing, replaced, symlinked, mode/identity-mismatched, or foreign states fail closed with content, sentinel, and mode unchanged and no worker start.
- Preserve descriptor A, post-mount R, exact mount-ID proof, R-based detach, and invocation-bound cleanup.
- Add RED-first regressions for foreign 0700/non-0700 anchors and foreign parent/missing-child cases; correct public capacity text to `64M`.

Review-tool envelope note:
- The standalone `validate-audit` CLI currently hard-codes reciprocal topology and rejects schema-valid `NARROW` input. Direct Draft 2020-12 schema and binding checks passed, and `validate-synthesis` exercised the same narrow-audit semantics successfully. This is a review-tool envelope defect, not a candidate finding; the installed skill/tool was not modified.

Current gate:
- Formal wave 1 is closed; no additional reviewer or same-wave retry is authorized.
- Repair lane is OPEN for the synthesized batch only. Any changed executable bytes require complete affected source gates, a new exact tree, and a newly bound blind source review before install/publication.
- Fresh install, gateway/live acceptance, commit/push, PR/CI/merge, and `FULL` remain closed.

## 2026-08-29 — product-scope clarification and minimum FULL gate

Operator clarification:
- `FULL` means Hermes can use Prime Agent as a subagent alternative and use Prime's resumable/RLM ability reliably.
- `FULL` does not mean host-security certification, universal provider support, cross-login survival, or adding a daemon, database, global cgroup, system service, or Hermes-core seam.

Necessity disposition:
- Retain the existing private namespace PID1 worker, bounded IPC/tmpfs, session lease, and bootstrap-bound anchor as already-implemented lifecycle scope guards for a Prime/RLM process tree that may spawn descendants.
- Do not reopen the architecture or add new lifecycle machinery while current product-path tests remain green.
- Do not repeat the predecessor's 30-cell whole-candidate ceremony. The successor requires one exact-tree, read-only delta closure covering F01/F02 plus the public product boundary; unchanged source evidence remains reusable.
- Thinkroom job `3fbf800a-719a-4b51-b1d0-727b6e8c26c8` completed on backend `scripted-v1`. Its branches and evidence were explicitly unverified, so it supplied orchestration smoke only and no design authority.

Minimum `FULL` acceptance:
1. Current exact source passes T0, plugin tests, P02 route matrix, P03 two-worker resume, and invocation-bound residue readback.
2. Exact successor tree receives a focused independent read-only repair/product-boundary closure with no unresolved material finding.
3. The committed exact bytes are clean-installed; registry exposes `delegate_minion`, `minion_session_status`, and `close_minion_session` from that projection.
4. One authenticated bounded delegation and one authenticated resumable/RLM continuation pass through the real registry with effective-route readback.
5. Completion/error cleanup leaves no task-owned worker, relay, Prime descendant, listener, or private mount.
6. PR CI is green, the change is merged, and GitHub `main` reads back the exact merged commit/content.

Current repair evidence:
- Focused anchor/ancestor/race regressions: 8 passed.
- Plugin suite: 46 passed.
- Ruff, compileall, Node syntax, staged/unstaged diff hygiene: PASS.
- P02: 18/18 routes, provider requests 0, namespace PID 1, cleanup true.
- P03: two worker lifecycles, same real Prime transcript/session, provider requests 0, cleanup true.
- Residue: fixed anchor empty, exact anchor mounts 0, exact worker/launcher/probe argv processes 0.
- Installed projection, gateway, provider-backed registry use, commit/push, PR, merge, and `FULL` remain pending.

## 2026-08-29 — delta review reopened F01; bounded repair applied

Delta review:
- Reviewed tree `7b6dc681b94f825b680ad6d0bdb543b186e49acf` is superseded and remains `BLOCKED`.
- Independent Terra/high finding `F01-RUNTIME-INDIRECT-ANCHOR-OVERRIDE` established that production `PRIME_MINION_RUNTIME_DIR` indirectly selected `runtime.parent / "invocation-anchor"`, despite the direct anchor override being test-only.
- F02 is closed; the minimum `FULL` product boundary is proportionate but could not pass while the fixed-route assertion was false.

Bounded reflection and repair:
- Trigger: focused lifecycle checks were green, but independent source review contradicted the assumption that rejecting only the direct anchor override fixed the complete production route.
- Disposition: one in-scope `INVESTIGATE/local_repair`; no architecture reopen.
- RED public-handler regression reached the forbidden worker seam and returned `production runtime override reached worker spawn`.
- Production runtime environment overrides now fail closed as test-only before runtime commit verification or worker dispatch. Runtime resolution is inside the handler's existing error envelope.
- The private fixture seam remains direct `_run_invocation` parameters; no daemon, store, receipt version, service, or public schema was added.
- Focused direct/indirect override plus anchor/ancestor/provision-failure regressions: 6 passed after repair.
- Ruff, compileall, and staged/unstaged diff hygiene: PASS after repair.

Next safe action:
- Run the final plugin suite on stabilized executable bytes, freeze a new exact staged tree, and obtain only an identity-bound closure disposition for this named F01 extension before commit/install.

## 2026-08-29 — second and final same-cause F01 repair

Closure result on tree `96a7b190c33fd0e61fb355589fda9d5439e8a0e3`:
- `BLOCKED`: indirect runtime override was closed, but a direct `PRIME_MINION_ANCHOR_PATH` override reached `_runtime_commit()` and its `git rev-parse` subprocess before the invocation-layer rejection.
- Existing direct-anchor regression was private-layer only and did not prove public-handler ordering.

Final bounded repair:
- Added a public `delegate_minion` RED with a valid-looking same-uid anchor/receipt and forbidden commit/RPC seams. Pre-repair it reached the forbidden worker seam.
- `_runtime_root()` now rejects both runtime and anchor environment overrides before returning the fixed installed runtime. The invocation-layer direct-anchor guard remains as defense in depth and for private fixture discrimination.
- Focused direct/indirect public-handler, private-layer, ancestor, and provision-failure regressions: 7 passed after repair.
- Same-cause repair budget is exhausted. Any further F01 production-route escape stops this lane for owner decision; no third patch is authorized.

Next safe action:
- Run one final plugin/T0 gate, freeze a new exact tree, and request a last identity-bound no-new-scope F01 closure. PASS may proceed to commit; BLOCKED stops before install.

## 2026-08-29 — source closure PASS; host-installer compatibility repair

Source closure:
- Exact tree `ee9453d4a9b8261eb4f3a0717815068f784da7c3` received terminal narrow Terra/high `PASS`: F01 direct/indirect closed, F02 closed, public oracles discriminating, and the minimum `FULL` boundary proportionate.
- Commit `30b47f507d1df203304d4d7f23531dfa9f2bf38b` has that exact tree and was pushed with matching remote commit/tree/parent readback.

Fresh-install observation:
- Exact-SHA CLI install failed before replacement because the current host installer supports manifest version 1 while the candidate declared version 2.
- Atomic failure readback preserved the old installed `0.2.0` projection, its prior HEAD/dirty state, and absent install metadata.
- The v2-only dependency/config metadata was not consumed by this plugin and was not required for the three-tool product route. The manifest is reduced to the honest v1 surface supported by the host installer; no Hermes upgrade, manual-copy bypass, runtime change, or additional product claim is introduced.

Next safe action:
- Verify manifest/T0 and plugin tests, commit/push the metadata-only compatibility successor, then retry exact-SHA clean install. The earlier executable source review remains bound to unchanged executable bytes; installation is the discriminator for this packaging change.

## 2026-08-29 — clean install PASS; CI capability classification repair

Installed candidate:
- Metadata-only compatibility successor `2dcc75c62166a86900c93a43ceed69e1e02f4b08` was pushed and its remote tree/parent were read back exactly.
- Official exact-SHA clean install succeeded: plugin `0.3.0` enabled, pinned install metadata exact, and all 23 installed source files matched the commit with no missing, extra, or mismatched source bytes.
- Fresh runtime bootstrap verified Prime Agent `0.8.1` at `bc0fa7606abb3b7af0f765319518d255e6ae553d`; runtime parent/anchor/receipt modes, receipt schema, FD admission, empty anchor, and post-bootstrap source parity passed.
- The package manager reported four inherited Prime dependency advisories (two moderate, two high). No automatic dependency rewrite is authorized in this lane.

Gateway pickup:
- In-gateway restart was correctly blocked because it would terminate the command process. No restart occurred and no bypass was attempted.
- One external-shell gateway restart remains the only user-owned prerequisite for registry/live delegation and resumable continuation acceptance.

CI RED and repair:
- Draft PR CI failed only in 11 true-process cases because GitHub's Linux runner denied the required user/mount/PID namespace operation while tests classified capability using `sys.platform` alone.
- Ten true-process decorators cover 14 cases, including a four-way parent-death matrix. All now depend on one session fixture that invokes the same production `check_capability_profile()` against a provisioned anchor.
- Unsupported hosts skip those T3 cases with the exact capability failure; supported Linux/WSL hosts still execute them. Production admission remains fail closed and no CI-name bypass, mock namespace, or process-group fallback exists.
- Discriminating evidence: capability-absent simulation skipped the selected true-process case with `unshare failed`; the supported WSL plugin suite remained 48/48 PASS with zero skips; Ruff, compileall, and diff hygiene passed.
- A local whole-repository run was not green because six unrelated Context Canvas soak-report tests could not resolve a trusted local Context Canvas tool root and produced no metric rows. The prior GitHub run passed that component and this successor changes only the Prime test module and WAL; publication still requires a fresh complete GitHub CI PASS.

Next safe action:
- Run the final repository suite, commit/push the test/WAL-only CI successor, and require green PR CI.
- After the external-shell gateway restart, run the minimum authenticated registry delegation plus two-turn resumable/RLM continuation and exact residue readback. Only then mark the PR ready and merge.

## 2026-08-29 — live registry continuation PASS; IPython bootstrap gap repaired

Live acceptance evidence before this repair:
- The gateway selected Prime through the public registry and returned the exact bounded ephemeral result.
- A real resumable session completed two authenticated turns; turn 2 recovered the marker stored only in turn 1, then the session was explicitly closed and read back as `CLOSED` at generation 2.
- The first real Prime IPython tool use failed before command execution. Prime attempted first-use `uv python install 3.11` inside the invocation-private HOME, so text delegation/resume were green but RLM/tool execution was not yet acceptable.

Root cause and bounded repair:
- Bootstrap installed and pinned the Prime Node runtime but did not provision Prime's Python kernel environment.
- Prime's existing `PRIME_AGENT_KERNEL_PYTHON` contract is used; no daemon, service, database, global environment, process-group fallback, or namespace architecture change is introduced.
- Install now calls Prime's own `ensureKernelPython()` once with a fixed sibling owner-controlled `kernel-venv`; verification requires the exact executable path.
- Production worker ignores caller overrides, fails closed when that fixed executable is missing, and passes only the fixed path to embedded Prime.

Discriminating evidence:
- RED: bootstrap helper absent; production reached the child-spawn seam without a fixed kernel runtime.
- GREEN: both focused tests passed independently.
- Affected invocation-worker suite: 37/37 PASS.
- Ruff, compileall, and diff hygiene: PASS.

Remaining gate:
- Freeze the repair bytes, perform one blind read-only delta review, require complete Prime plugin/CI verification, clean-install the exact successor, and rerun a real IPython/RLM call plus completion/error residue readback. `FULL`, PR merge, and GitHub `main` remain closed until those gates pass.
- AAR `0.6.0a1` remains independently hash-bound `PAUSED`; this repair does not alter that verdict.

## 2026-08-29 — kernel bootstrap delta review BLOCKED; no-uv repair applied

Identity-bound review:
- Exact staged tree `73ba62e57ac5b3eda8903244a9de4c7ad27e7e89` received a batch-complete independent Terra/high `BLOCKED` verdict; reviewer modified 0 files.
- Sole material finding: the documented fresh-install prerequisites did not include `uv`, while Prime 0.8.1 refuses non-interactive first-install kernel provisioning without either an existing `uv` or `PRIME_AGENT_INSTALL_UV=1`.
- No other material finding was identified; fixed worker path, caller-override exclusion, and missing/non-executable fail-closed behavior were source-supported.

Smallest repair and discriminating evidence:
- The installer-only kernel environment now sets `PRIME_AGENT_INSTALL_UV=1`; verification and invocation runtime environments do not receive it.
- The bootstrap regression supplies a PATH with no `uv` and requires the fixed venv plus install flag. RED failed with missing key; GREEN passed after the one-line production repair.
- Ruff, compileall, Node syntax, Plugin Doctor, and diff hygiene: PASS.
- First post-repair full Prime suite had one cancellation-timing failure outside the repair seam; mount absence and child reaping were true, task residue readback was zero, and the isolated test passed on the single permitted retry. The final complete Prime suite passed 50/50. No repeated same-cause retry was used.

Current gate:
- Rebind a successor exact tree and perform the required narrow identity-bound re-review of the installer/bootstrap path and affected regression. No commit, push, clean install, provider call, PR promotion, merge, or `FULL` claim is authorized before that closure.

## 2026-08-29 — source and live release-candidate closure PASS

Review and source publication:
- Successor tree `5f89024e8f611fb1308804fa74d8e29e7a8c5b84` received narrow identity-bound Terra/high `PASS`; the no-uv fresh-install blocker is `CLOSED`, no new material finding was identified, and reviewer modified 0 files.
- Reviewed commit `7c2ac7db2b08f9e19a180c888ad363e29216bc6f` has that exact tree. Local, remote branch, and PR head readback matched; GitHub CI `test` completed `SUCCESS`.

Clean install and bootstrap:
- A first whole-repository install was correctly blocked as `DANGEROUS` because the scanner evaluated unrelated repository instructions and specifications; `--force` did not bypass it and the installed target was unchanged.
- The official monorepo-subdirectory identifier limited installation to `plugins/prime-minion`. Its `CAUTION` findings were the reviewed `/proc` namespace and subprocess product behavior, so the trusted exact-SHA install used the CLI's explicit `--force` confirmation without disabling scanning.
- Exact subdirectory install produced plugin `0.3.0`; installed source parity was 23/23 with no missing, mismatched, or extra source files.
- Fresh bootstrap installed Prime `0.8.1` at `bc0fa7606abb3b7af0f765319518d255e6ae553d`, provisioned the fixed kernel venv, and passed Prime's own runtime/default-package verification plus lifecycle profile `linux-user-mount-pid-v1`.
- The four inherited npm audit advisories remain two moderate and two high; no automatic dependency rewrite was authorized.

Authenticated live acceptance without another gateway restart:
- Public handler/schema bytes were unchanged; the existing registry handler spawned the newly installed worker path directly.
- Real ephemeral delegation sent the provider request with route/effective-route `openai-codex / gpt-5.6-luna / low`, used `ipython` once with `is_error=false`, and returned exact result `PRIME_IPYTHON_ACCEPTED` in 8.452 seconds.
- Controlled timeout used one IPython call sleeping 120 seconds under a 30-second invocation deadline. The public result was `status=error / invocation timed out`; shutdown diagnostics contained expected closing-transport/EPIPE noise.
- Immediate residue readback after timeout: task-owned worker/launcher/Prime/RLM processes 0, anchor mounts 0, anchor entries 0.

Publication gate:
- Executable, resumable/RLM, install, live success, and live error-cleanup gates are PASS for the minimum product boundary.
- This WAL addition is metadata-only. Require its own CI, exact final-head install/source readback, PR merge, and GitHub `main` exact commit/content readback before declaring Prime Minion `0.3.0 FULL`.
- AAR `0.6.0a1` remains independently hash-bound `PAUSED`; Prime closure does not amend its dispatcher-to-store close-fence P0.
