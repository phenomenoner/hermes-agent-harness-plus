# MCP Sidecar Health Checklist

Use this checklist when a local MCP sidecar reports a keepalive or reconnect
warning, disappears from the tool list, or returns a transient transport error.

The goal is to tell a recovered connection from a real service or data failure
without reaching for an unnecessary restart, rebuild, or reindex.

## 1. Capture a bounded event window

Record the smallest useful slice of evidence:

- the first and last warning timestamps;
- the affected server names;
- whether the host attempted an automatic reconnect;
- how often the warning repeated in that window.

Keep log reads narrow. Do not copy an entire gateway log or runtime directory
into an incident note.

An empty exception message is an observability gap, not proof of a particular
root cause. Preserve the exception class and a sanitized detail string when the
host supports it, in a bounded local diagnostic receipt. Use the shared-output
rules in section 6 before including those details in a public report.

## 2. Check the host lifecycle

Confirm whether the process that owns the MCP connections stayed stable:

```bash
hermes gateway status
systemctl --user show <service-name> \
  -p MainPID -p ActiveState -p SubState --no-pager
```

Record whether the main PID changed near the warning. A stable host with no
child-process exit is different from a host restart that affected every
sidecar.

If a service status command reports configuration drift, compare behaviorally
important fields before acting: executable, working directory, environment, and
restart policy. Formatting-only differences are not a reason to restart a
healthy service.

## 3. Open a fresh transport probe

Use a fresh CLI process or the host's MCP connection test instead of trusting
only the tool list cached in a long-running conversation:

```bash
hermes mcp list
hermes mcp test <server-name>
```

Run the test once after the warning. Repeated probing can create more reconnect
noise without adding evidence.

A successful transport test proves that a new MCP connection can be opened. It
does not yet prove that the backend can serve useful data.

## 4. Verify one real data path

Run a small, read-only operation through the MCP tool:

- a database sidecar: list collections, then run a known search;
- a file sidecar: list or read one approved test path;
- a task-state sidecar: read a known test record;
- an API sidecar: fetch one low-cost status or lookup result.

Prefer two layers of evidence when available:

1. metadata or health information;
2. one domain query that returns plausible content.

Do not use a keepalive warning alone to trigger a write probe, collection
rebuild, storage repair, or full reindex.

## 5. Classify before repairing

| Evidence | Classification | Next action |
| --- | --- | --- |
| Warning occurred; host stayed stable; fresh transport and real query pass | Recovered transport event | Observe. Do not restart or touch data. |
| Fresh transport fails, but the backend's direct health endpoint is good | Sidecar or connection incident | Reload or restart only the affected connection or sidecar in an appropriate maintenance window, then repeat the real query. |
| Transport opens, but backend status or the real query fails | Backend or data incident | Inspect backend health and configuration; apply the smallest repair that matches the failure. |
| Host PID changed or several unrelated sidecars fail together | Host lifecycle incident | Inspect the gateway or service lifecycle before repairing individual sidecars. |

Escalate only when more than the original warning supports the incident. Useful
signals include repeated transport failures, child exits, failed domain queries,
bad backend health, or dependent scheduled jobs turning unhealthy.

## 6. Keep diagnostics useful and share-ready

For maintainers improving a warning or startup path, keep the original failure
visible even when collecting diagnostics or closing the connection also fails.
These are implementation checks, not extra probes to run after every warning.

### Read the actual failure signals

- Inspect the installed SDK's model attributes and wire aliases rather than
  assuming they match. An attribute such as `is_error` may serialize as `isError`.
- A successful transport is not a successful tool result. Check the SDK error
  flag and the producer's declared failure envelope before accepting startup or
  allowing any dependent write or authorization step.
- Keep an unrecognized reply shape unknown or rejected under the active contract;
  do not reinterpret it as success because a familiar nested field is absent.

### Preserve the primary fault

- Capture the failed phase and original exception or error code before optional
  diagnostic work and before context-manager exit or teardown can replace it.
- If diagnostic collection fails, record a bounded status such as `not_retained`
  and preserve the original failure. Do not invent evidence, return success, or
  replay a side-effecting operation just to obtain a better diagnostic.
- If the operation and cleanup both fail, retain the operation's primary fault
  and record the cleanup fault separately. A cleanup-only failure must still be
  reported; process exit alone does not prove every cleanup obligation completed.
- Attribute a deadline failure to the timeout scope that actually expired, not
  merely to an exception named `TimeoutError`. Do not renew an overall deadline
  at each phase or treat a caller timeout as proof that the child stopped.

### Separate local detail from shared output

- Keep useful exception details in bounded local evidence only after redaction
  and access-control checks. An empty message remains an observability gap.
- For public reports or returned evidence, use a finite vocabulary of phases and
  error categories, with an explicit unknown category. Exception messages,
  dynamic class names, arguments, and tracebacks can all carry private values.
- Apply this boundary inside the real helper's catch path, before serialization;
  a safe outer formatter cannot undo a value already emitted by an inner helper.
- Keep account details, raw configuration, and local data out of shared output.

### Test the real diagnostic boundary

Use controlled collaborators around the actual helper, not a substitute helper
that already returns safe diagnostics. Include the applicable cases:

- blank and ordinary exception messages;
- distinct private-value sentinels in the message and dynamic class name, with
  neither appearing anywhere in shared output;
- a valid producer-declared failure that stops before the next effect, alongside
  a positive control that reaches a harmless test-only boundary;
- diagnostic collection failure, and primary failure followed by teardown failure,
  with the original phase/code preserved and cleanup status recorded separately.

When a real SDK test needs tool or output-schema discovery, make the synthetic
peer answer it. A missing protocol operation is a test-scaffold failure, not
evidence that the product correctly rejected the reply. Keep controlled-test
success separate from installed-runtime or real-task acceptance.

Better diagnostics are usually a safer first fix than restarting a healthy
production process just to load new logging code.

## 7. Verify after any action

After a reload, restart, or targeted repair, repeat the same evidence chain:

1. host lifecycle is stable;
2. a fresh MCP transport test passes;
3. the real read/query passes;
4. any dependent watchdog or scheduled job is healthy;
5. the normal quiet path is quiet again.

For unattended jobs, pair this checklist with the
[Scheduled Agent Health Checklist](scheduled-agent-health-checklist.md).

## 8. Close out with a compact receipt

```text
Observed: <bounded warning window and affected servers>
Host lifecycle: <stable or changed>
Transport: <fresh probe result>
Data path: <read-only query result>
Classification: <recovered transport / sidecar / backend / host>
Action: <none or smallest repair>
Verification: <post-action probes and quiet-path result>
```

Keep raw logs in the local evidence store. The closeout should preserve the
decision and verification, not the whole runtime transcript.
