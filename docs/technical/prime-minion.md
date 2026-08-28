# Prime Agent Minion Bridge

## Purpose

The Prime Agent Minion Bridge lets Hermes Agent use Prime Agent as a bounded coding/research runtime without handing Prime the provider credential or final task authority.

Hermes owns:

- task admission and routing;
- OpenAI Codex OAuth credentials;
- integration, shared verification, and final claims;
- session identity and lifecycle policy.

Prime owns:

- the coding-agent loop;
- tool-call parsing and IPython execution;
- workspace interaction within the invoking user's permissions;
- native JSONL transcript persistence.

## Invocation flow

```text
Hermes delegate_minion
  -> validate workspace, route, Prime pin, and session ownership
  -> start a loopback-only Codex Responses relay
  -> start one isolated Prime RPC process
  -> Prime streams inference through the relay
  -> Prime performs bounded workspace/tool work
  -> Hermes reads back the effective route and final state
  -> stop the Prime and relay process groups
```

The relay translates credential ownership, not agent ownership. Prime still decides when to call its tools; Hermes still decides whether the resulting work is accepted.

## Credential containment

The relay process resolves an authenticated OpenAI Codex credential from the Hermes credential pool. The child receives only:

- a loopback base URL;
- a freshly generated synthetic bearer; and
- a sanitized environment with provider secret variables removed.

The parent hands the same fresh bearer to the relay through the relay's anonymous stdin pipe and to the child through the sanitized child environment. The relay requires an exact constant-time bearer match before resolving the Hermes credential, then replaces the synthetic bearer and forwards only the supported Codex Responses path. It binds to `127.0.0.1` and supports HTTP/SSE rather than WebSocket.

Neither the real OAuth credential nor the synthetic bearer is written to the durable minion session manifest.

## Route contract

Every invocation declares:

```text
provider + model + reasoning_effort
```

The current provider is `openai-codex`; the model/effort matrix is defined in the plugin schema. Hermes asks Prime for its effective route before and after task execution. A mismatch fails closed instead of silently accepting a fallback.

## Ephemeral and resumable modes

### Ephemeral

- Prime starts with `--no-session`.
- The process and relay exit after one invocation.
- No Prime transcript is retained by the plugin.

### Resumable transcript

- Hermes creates an opaque `minion_<uuid>` identity.
- The internal manifest binds the canonical workspace and pinned Prime commit.
- Prime persists native JSONL under the session directory.
- A later invocation starts a new relay and a new Prime process with `--resume <internal-file>`.
- The caller supplies only the opaque session ID, never a filesystem path.

Resume preserves transcript context and facts already written to disk. It does not preserve a kernel, subprocess, shell environment, or in-flight tool call.

## Session lifecycle

```text
IDLE -> RUNNING -> IDLE
                 -> INTERRUPTED
IDLE/INTERRUPTED -> CLOSED
```

The manifest is atomically replaced and protected by an exclusive local lease. Resume revalidates:

- session ID format;
- session state;
- canonical workspace;
- pinned Prime source commit;
- transcript location and ownership boundary; and
- explicit route readback.

An uncertain process loss records `INTERRUPTED`. The bridge does not replay the prior prompt or tool call automatically because a workspace mutation may already have occurred.

## Process cleanup

Timeout and cancellation attempt a Prime RPC abort, then terminate the Prime and relay process groups. Successful completion also stops both process groups before returning. Verification should check both the terminal tool result and the absence of orphan processes.

## Host compatibility boundary

The plugin is a standalone Hermes extension and does not patch Hermes core. It relies on these host surfaces:

- plugin tool registration with `is_async=True` for coroutine handlers;
- the Hermes credential pool implementation;
- Hermes profile state resolution; and
- current OpenAI Codex request headers and endpoint behavior.

A future change to any of those surfaces requires focused compatibility testing. Import/registration checks alone are insufficient: at least one actual registry dispatch must cross the async adapter.

## Security boundary

Prime is not an operating-system sandbox. The runtime has the invoking user's permissions in the selected workspace. Use a bounded canonical workdir and the normal Hermes approval policy. Credentials stay on the Hermes side, but filesystem authority still needs ordinary operator discipline.

Prime starts with `--no-extensions`, so its vulnerable project-local extension discovery path is disabled and only the bridge's explicit CLI extension is loaded. The pinned upstream dependency tree still has npm advisories. In particular, `extract-zip` is used by Prime's managed-tool archive installer and npm currently reports `GHSA-jmr9-qjv8-65gv` with no fix. Normal bridge RPC startup does not call that installer, and bootstrap does not rewrite the upstream lockfile. Treat this as a residual supply-chain boundary: inspect current audit output, use trusted workspaces or external OS isolation, and requalify any new Prime pin.

## Verification layers

1. Focused Python tests for relay authentication, route validation, session leases, interrupted-state handling, and sanitized status.
2. `node --check` for the Prime extension.
3. Credential-free Prime RPC route matrix.
4. Credential-free two-process session identity probe.
5. Optional authenticated smoke for tool calling.
6. Optional authenticated two-turn resume smoke across Prime process exit.
7. Real Hermes gateway registry dispatch for `delegate_minion`, status, close, and closed-state readback.
