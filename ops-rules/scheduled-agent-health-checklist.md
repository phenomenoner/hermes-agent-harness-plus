# Scheduled Agent Health Checklist

Use this checklist when a Hermes Agent workflow runs from cron, a systemd timer,
a CI schedule, or another unattended runner.

The goal is simple: quiet success, useful failure messages, and checks that prove
the scheduled job really did the work it claims to do.

## 1. Start with the scheduler state

Record the basic state before changing anything:

```bash
hermes cron list --all
hermes cron status
```

If your schedule is outside Hermes Agent, use the equivalent command for that
runner, such as `systemctl --user list-timers`, `crontab -l`, or your CI run
history.

Check:

- the job is enabled in the runner you expect;
- the last run time and next run time match the intended cadence;
- the last status is not the only evidence you use;
- there is not a duplicate job doing the same maintenance under another runner.

## 2. Verify the underlying work, not only the wrapper

A scheduler can report success even when the useful work was skipped, partially
blocked, or validated with the wrong probe. Pair scheduler status with one small
source-level check.

Good examples:

- a refresh job: run the read-only health check for the refreshed index;
- an artifact job: confirm the expected output file exists and renders or parses;
- a sync job: compare the local and remote commit or release marker;
- a watchdog: run its verbose mode once, then its quiet mode.

For an MCP-backed job, pair these checks with the
[MCP Sidecar Health Checklist](mcp-sidecar-health-checklist.md). A reconnect
warning should be followed by a fresh transport test and a real read/query
before any restart or data repair.

Prefer deterministic commands that work in minimal shell environments. Avoid
relying on interactive helpers or notebook-style execution paths inside an
unattended runner.

## 3. Resolve helper inputs explicitly

Scheduled agents often run from a project directory, but the instructions they
load may live somewhere else. Before treating a missing file warning as a job
failure, confirm which layer owns the file.

Good rules:

- project files should be opened with repo-relative paths from the job's
  configured workdir;
- installed skill references should be loaded through the skill tooling, or by an
  explicit skill-install path supplied by the operator;
- runtime-generated receipts should go to a known artifact directory, not beside
  the helper script by accident;
- examples in public docs should use placeholders such as `<project-root>` or
  `<skill-name>` instead of machine-specific paths.

If a scheduled run mixes these layers, fix the prompt, script, or skill note so
future runs know where each reference is expected to live.

## 4. Inspect logs with a narrow window

Use the smallest log scope that can answer the question:

```bash
journalctl --user -u <service-name> --since "2 hours ago" --no-pager
```

For file logs, inspect the known file and a bounded line range. Avoid broad
recursive searches across a whole home directory or runtime tree; they are slow,
noisy, and more likely to pull private data into a public report.

Look for:

- missed-run or catch-up messages;
- shutdown or timeout errors near the expected run window;
- partial attempts that did not update the job's normal status field;
- dependency failures that were swallowed by a wrapper script.

## 5. Keep successful runs quiet

For recurring maintenance, make the healthy path produce no output or a compact
single-line receipt. Alert only when action is needed.

A good scheduled helper should:

- exit `0` and print nothing when everything is healthy;
- print a short, actionable message on failure;
- include the next manual command to run when safe;
- keep repair commands separate from detection unless automatic repair is an
  explicit feature.

## 6. Update the existing job before adding another one

When a recurring check needs better behavior, first improve the existing job,
script, or prompt. Add a new scheduled job only when the cadence, destination, or
responsibility is genuinely different.

Before adding a new job, write down:

```text
Purpose:
Cadence:
Owner runner:
Success signal:
Failure signal:
Overlap checked against:
```

This prevents quiet scheduler sprawl and makes future cleanup easier.

## 7. Treat one-shot checks as temporary probes

A one-shot scheduled check can be the right tool for verifying tomorrow's
cleanup, a delayed webhook, or a post-maintenance observation. Keep it different
from recurring maintenance:

- before the scheduled time, record it as `pending`, not failed;
- after the due time, verify the source result instead of trusting only the
  scheduler's status field;
- close, remove, or archive the probe after it has produced its evidence;
- do not convert a one-shot probe into a recurring job unless the responsibility
  really needs a cadence.

This keeps short-lived follow-up checks useful without growing a second layer of
maintenance jobs.

## 8. Close out with evidence

A useful closeout note says:

```text
Changed: <script, job, or docs touched>
Scheduler: enabled/disabled, last run, next run
Verification: <commands and short result>
Quiet path: <confirmed or not applicable>
Privacy: <public scan or local-only note>
```

Keep raw logs, account details, and machine-specific paths out of public docs.
