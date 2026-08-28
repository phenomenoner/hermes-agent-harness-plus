# Prime Agent Minion Bridge

Run [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) as an optional Hermes Agent **minion** while keeping provider credentials on the Hermes side.

Prime owns the coding-agent loop, IPython tools, and workspace interaction. Hermes owns admission, route selection, OAuth credentials, integration, verification, and the final claim.

## What it provides

- `delegate_minion` — run an ephemeral task or create/resume a durable transcript session.
- `minion_session_status` — inspect sanitized durable session state.
- `close_minion_session` — close a session without deleting its transcript.
- Explicit per-turn `provider`, `model`, and `reasoning_effort` with effective-route readback.
- Fresh Prime process, loopback relay, and synthetic bearer for every invocation.
- A pinned Prime runtime installed outside Git with a reproducible bootstrap script.

Current verified route matrix:

- Provider: `openai-codex`
- Models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`
- Effort: `none`, `low`, `medium`, `high`, `xhigh`, `max`
- Prime Agent: commit `bc0fa7606abb3b7af0f765319518d255e6ae553d`, CLI `0.8.1`

This is an OpenAI Codex vertical slice, not a universal provider gateway.

## Install

Prerequisites:

- a working Hermes Agent installation with standalone plugin support;
- an authenticated `openai-codex` credential in Hermes;
- Git, Node.js, and npm;
- `aiohttp>=3.9,<4` in the Python environment that runs `hermes`.

From a Harness Plus clone:

```bash
python -m pip install 'aiohttp>=3.9,<4'
mkdir -p ~/.hermes/plugins
cp -R plugins/prime-minion ~/.hermes/plugins/prime-minion
cd ~/.hermes/plugins/prime-minion
python scripts/bootstrap_runtime.py
python scripts/bootstrap_runtime.py --verify-only
hermes plugins doctor . --ci
hermes plugins enable prime-minion
hermes gateway restart
```

Start a fresh Hermes session after enabling or updating the plugin. A running gateway keeps the plugin registration it loaded at startup.

## Run an ephemeral task

```json
{
  "task": "Implement the bounded change and run its focused test",
  "workdir": "/absolute/path/to/project",
  "provider": "openai-codex",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "max",
  "session_mode": "ephemeral",
  "timeout_seconds": 1800
}
```

Ephemeral mode is the default and does not keep a Prime transcript.

## Create and resume a transcript session

Create:

```json
{
  "task": "Complete the first bounded slice and report what remains",
  "workdir": "/absolute/path/to/project",
  "provider": "openai-codex",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "max",
  "session_mode": "resumable",
  "timeout_seconds": 1800
}
```

The result includes an opaque identifier:

```json
{
  "status": "completed",
  "session_mode": "resumable",
  "session_id": "minion_<opaque-id>",
  "generation": 1,
  "session_state": "IDLE"
}
```

Resume by sending a new explicit task with the same `session_id`:

```json
{
  "task": "Continue from the previous result and run the next focused check",
  "workdir": "/absolute/path/to/project",
  "provider": "openai-codex",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "max",
  "session_id": "minion_<opaque-id>",
  "timeout_seconds": 1800
}
```

Each turn still starts a new relay and a new Prime process. Resume restores the transcript and disk workspace; it does not resurrect a process or kernel.

## Durable contract

A resumable session binds:

- its opaque session identity;
- the canonical workspace;
- the pinned Prime commit; and
- an explicit requested/effective route for each turn.

The store uses atomic manifest replacement, owner-only permissions, and an exclusive per-session lease. Public tool results never expose the internal transcript path.

States:

```text
IDLE -> RUNNING -> IDLE
                 -> INTERRUPTED
IDLE/INTERRUPTED -> CLOSED
```

If a process disappears before recording a terminal result, the turn becomes `INTERRUPTED`. The plugin never blindly replays that prompt or an uncertain tool effect. A later resume is a new explicit turn after the caller reconciles the workspace.

Preserved:

- conversation and tool-call transcript;
- compacted conversational context;
- workspace files and Git state already written to disk;
- per-turn requested/effective route and terminal status.

Not preserved:

- IPython variables held only in RAM;
- shell environment changes held only by the exited child;
- live subprocesses or kernels;
- a tool call that was in flight when the process disappeared.

## Credential boundary

```text
Hermes tool call
  -> loopback-only Responses relay
  -> one Prime RPC process with a synthetic bearer
  -> Hermes credential pool and Codex endpoint
  -> Prime agent/tool loop
  -> compact result to Hermes
```

- The relay binds `127.0.0.1` on an ephemeral port.
- Prime never receives the real OAuth credential.
- Provider credentials are stripped from the Prime child environment.
- Every invocation receives a new synthetic bearer.
- The isolated Prime profile pins HTTP/SSE; this relay does not support WebSocket.
- Prime starts with `--no-extensions`; only this package's explicit `prime_extension.mjs` is loaded.

Prime remains an execution runtime, not an operating-system sandbox. It runs with the invoking user's workspace permissions.

### Pinned-runtime supply-chain note

The bootstrap preserves Prime's pinned lockfile and reports npm's audit output; it does not run `npm audit fix` or silently change upstream dependencies. At this release validation, `npm audit --omit=dev` reports known advisories in the pinned Prime dependency tree, including `GHSA-jmr9-qjv8-65gv` in `extract-zip`, for which npm reports no fix. The affected archive extraction path belongs to Prime's managed-tool installer and is not used by the bridge's normal RPC bootstrap or task path. Review current audit output before deployment, keep workspaces trusted or externally isolated, and requalify the bridge when moving the Prime pin.

## PMO routing

The companion rule in [`ops-rules/prime-minion-pmo-routing.md`](../../ops-rules/prime-minion-pmo-routing.md) keeps authority separate from execution:

- the main agent coordinates, integrates, verifies, and owns the final claim;
- after the dispatch gate admits delegation, a verified `Luna/max` execution lane may default to Prime minions for stable bounded work;
- `Terra/high` review may use a native subagent or Prime minion when independence is preserved;
- worker completion is evidence, not acceptance.

## Verify

Credential-free checks:

```bash
python -m pytest -q plugins/prime-minion/tests
python plugins/prime-minion/scripts/probe_rpc.py --matrix
python plugins/prime-minion/scripts/probe_resume_rpc.py
node --check plugins/prime-minion/prime_extension.mjs
```

Provider-backed smoke checks are opt-in because they consume an authenticated Codex call:

```bash
python plugins/prime-minion/scripts/smoke_minion.py
python plugins/prime-minion/scripts/smoke_resume_minion.py
```

The resume smoke proves that a second Prime process can recover context that appears only in the first turn, preserve a workspace proof, advance the durable generation, and close the session.

## Current limits

- OpenAI Codex only.
- HTTP/SSE only.
- Transcript resume, not a resident worker or exactly-once durable orchestrator.
- The relay imports Hermes credential-pool internals, so Hermes auth changes require focused compatibility testing.

## License and upstream

The bridge is MIT licensed. Prime Agent is not vendored; the bootstrap script installs the pinned upstream checkout under the plugin's ignored `.runtime/` directory. See the repository [`NOTICE`](../../NOTICE) for upstream and license pointers.
