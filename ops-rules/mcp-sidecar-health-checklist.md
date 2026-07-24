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
host supports it.

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

For maintainers improving the host's warning path:

- log the exception class as well as the sanitized message;
- preserve ordinary error text;
- redact credentials and secret-looking values before they reach logs;
- add regression tests for both blank-message and normal-message exceptions;
- keep account details, raw configuration, and local data out of public reports.

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
