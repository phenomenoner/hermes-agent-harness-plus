# SQLite State Incident Checklist

Use this checklist when a long-running agent, scheduler, or local sidecar reports
SQLite corruption, locking, I/O errors, or an unexpected recovery directory.

The goal is to preserve evidence, distinguish a historical incident from a
current failure, and choose the smallest safe next step before changing a live
state store.

## 1. Hold mutations until the state owner is known

Start with read-only observations. Do not run `VACUUM`, `REINDEX`, a write-mode
checkpoint, schema migration, repair utility, or ad-hoc file replacement while
the active writer is unknown.

Record:

- the service generation or start time and its restart count;
- the database, `-wal`, and `-shm` filenames, inode numbers, sizes, and modified
  times;
- every process that currently holds one of those files open;
- the SQLite library version used by the service and by the diagnostic tool.

Treat the main database and its WAL sidecars as one live state set. Copying only
the main file can omit committed pages that still live in the WAL. Avoid hashing
large, changing files during the first pass; metadata and holder checks usually
answer the ownership question with less load.

If an unexpected process has a write-capable handle, stop and resolve ownership
before taking a snapshot or attempting repair.

## 2. Put log evidence on the service timeline

Search a bounded log window for specific signatures such as malformed database,
FTS corruption, disk I/O, lock contention, or WAL recovery messages. Record the
first and last matching timestamps and the count for each signature without
copying unrelated log payloads into the incident report.

Compare every hit with the current service generation:

- older errors prove that an incident occurred, not that it is still active;
- a clean current generation is useful evidence, but not a substitute for a real
  database query;
- a timeout or truncated log query is `unknown`, not a clean result.

This timeline prevents old errors from forcing unnecessary repair on a healthy
replacement generation.

## 3. Use bounded, read-only database probes

When practical, run diagnostics with the same SQLite build and application
runtime used by the service. Open the database with `mode=ro`, enable
`query_only`, and begin with the smallest probes that can falsify health:

1. open `sqlite_master`;
2. inspect the expected schema names;
3. run `quick_check` on a few critical tables;
4. run broader checks only when the focused result or incident contract requires
   them.

A bounded Python probe can make timeout semantics explicit:

```bash
STATE_DB=/path/to/state.db \
SCOPED_TABLES=sessions,settings \
CHECK_SECONDS=30 \
python3 - <<'PY'
import os
import re
import sqlite3
import time
from pathlib import Path

name_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
db_path = Path(os.environ["STATE_DB"]).resolve()
tables = [name for name in os.environ["SCOPED_TABLES"].split(",") if name]
seconds = float(os.environ.get("CHECK_SECONDS", "30"))

connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
connection.execute("PRAGMA query_only=ON")
print("query_only=", connection.execute("PRAGMA query_only").fetchone()[0])
print("journal_mode=", connection.execute("PRAGMA journal_mode").fetchone()[0])
connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
print("sqlite_master=ok")

for table in tables:
    if not name_pattern.fullmatch(table):
        raise SystemExit(f"invalid table name: {table!r}")
    deadline = time.monotonic() + seconds
    connection.set_progress_handler(
        lambda: 1 if time.monotonic() >= deadline else 0,
        1_000,
    )
    try:
        rows = connection.execute(f'PRAGMA quick_check("{table}")').fetchall()
        result = [row[0] for row in rows]
        print(f"{table}=", "ok" if result == ["ok"] else result)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            print(f"{table}=unknown_timeout")
        else:
            raise
    finally:
        connection.set_progress_handler(None, 0)

connection.close()
PY
```

The table list is deliberately explicit. Do not accept arbitrary SQL from an
alert payload or external request. On large databases, a global
`integrity_check` can be too expensive for an initial health decision.

Interpret results precisely:

- `ok` means that probe completed successfully;
- `unknown_timeout` means the probe did not finish;
- a returned error or non-`ok` check result is actionable evidence;
- successful focused checks do not prove that every table is healthy.

## 4. Classify recovery generations before retention

A recovery directory may contain several files for one event. Group the main
database, WAL, SHM, manifest, and receipts by generation instead of counting each
file as a separate recovery.

For each generation, record whether it is:

- an empty scaffold;
- an incident-source or quarantine bundle;
- a restore candidate that has not been exercised;
- a verified recovery point;
- a superseded generation retained for forensics.

A candidate becomes a verified recovery point only after its required chain
passes, for example:

```text
consistent capture -> file/manifest integrity -> application-specific checks
-> bounded restore rehearsal -> service readback
```

Preserve the original incident source and prior verified generations until the
new recovery point passes. Apply retention only to verified, completed
generations and keep the repository's required recovery floor.

## 5. Choose observation, snapshot, or repair from evidence

Observation is usually appropriate when all of these are true:

- current-generation logs have no recurring corruption or I/O signature;
- the live database opens read-only;
- critical scoped checks complete with `ok`;
- only the expected service owns the live state set;
- a verified recovery point already exists.

Open a separate repair decision when any of these occurs:

- read-only open fails;
- a completed check returns corruption or another non-`ok` result;
- current-generation errors repeat;
- an unknown writer owns the files;
- the application cannot read required state even though SQLite opens.

Before a mutation, capture a consistent candidate through the application's
supported maintenance procedure or SQLite backup API. Do not improvise a live
main-file-only copy. Define the rollback generation, stop conditions, and the
independent post-change verifier before applying the change.

## 6. Verify recovery from outside the changed process

A successful repair command, file replacement, or service start is not the final
result. From a fresh process or independent supervisor, verify:

- the new process identity and start time;
- read-only database access and the required scoped checks;
- one real application query;
- expected file ownership;
- absence of new incident signatures during the observation window;
- rollback material remains usable until the acceptance window closes.

If an expensive check times out, report that boundary as unknown and acquire a
smaller discriminating probe. Do not convert incomplete evidence into a healthy
or corrupt verdict.

## 7. Close out without publishing private state

A compact incident closeout can use this shape:

```text
Timeline: <incident window and current service generation>
Ownership: <expected or unresolved holders>
Read-only checks: <completed results and explicit unknowns>
Recovery: <latest verified generation and retained evidence>
Decision: <observe, snapshot, repair, or restore>
Next gate: <natural run, bounded check, or maintenance approval>
Privacy: <logs and state payloads kept local>
```

Keep raw database contents, user records, account identifiers, local paths, and
full logs out of shared documentation. Publish only the reusable procedure and
sanitized verification contract.
