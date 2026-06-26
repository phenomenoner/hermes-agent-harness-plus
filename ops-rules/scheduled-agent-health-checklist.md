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

Prefer deterministic commands that work in minimal shell environments. Avoid
relying on interactive helpers or notebook-style execution paths inside an
unattended runner.

## 3. Inspect logs with a narrow window

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

## 4. Keep successful runs quiet

For recurring maintenance, make the healthy path produce no output or a compact
single-line receipt. Alert only when action is needed.

A good scheduled helper should:

- exit `0` and print nothing when everything is healthy;
- print a short, actionable message on failure;
- include the next manual command to run when safe;
- keep repair commands separate from detection unless automatic repair is an
  explicit feature.

## 5. Update the existing job before adding another one

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

## 6. Close out with evidence

A useful closeout note says:

```text
Changed: <script, job, or docs touched>
Scheduler: enabled/disabled, last run, next run
Verification: <commands and short result>
Quiet path: <confirmed or not applicable>
Privacy: <public scan or local-only note>
```

Keep raw logs, account details, and machine-specific paths out of public docs.
