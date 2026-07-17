# Technical Note: Local Qdrant Recall

The Qdrant helpers are local-first. They assume a Qdrant server is reachable at
`http://127.0.0.1:6333` and use FastEmbed to produce 384-dimensional multilingual
embeddings by default.

Default model:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Default collections:

```text
hermes_skills_multilingual_v1
hermes_sessions_recent_multilingual_v1
```

The collection names are examples for a two-corpus setup. If you add more local
corpora, keep each corpus in its own collection and make sure the ingest-time and
query-time embedding models match.

## Privacy posture

The session indexer:

- indexes only user and assistant text by default;
- skips system prompts and tool outputs unless explicitly enabled;
- truncates long messages;
- redacts common secret-looking patterns;
- supports `--dry-run` so you can preview before writing to Qdrant.

Local recall is useful, but it is still an index of your text. Read the dry-run
output before indexing.

## Keep skill recall aligned with the active catalog

The skill indexer treats the Qdrant collection as a searchable projection of the
active skill catalog. By default it:

- discovers canonical `SKILL.md` files;
- skips skills under hidden directories such as `.archive/` and
  `.curator_backups/`;
- skips skills whose frontmatter `status` is `retired`, `archived`, or
  `deprecated`.

After retiring or restoring skills, recreate the skills collection so stale
points do not survive an upsert-only run. Verify the boundary with one positive
search for an active skill and one negative search for a retired skill.

## Interpreting point-count drift

Recent-session collections usually behave like rolling windows. A refresh may
drop older chunks and add newer ones, so a small point-count change is not by
itself a repair signal.

Before rebuilding a collection, check three things together:

1. **API health:** the collection exists, is green, and has the expected vector
   size and distance.
2. **Window intent:** the ingest window, maximum session count, and source query
   still match what you meant to index.
3. **Recall quality:** one or two known recent topics return relevant hits.

Treat point counts as supporting evidence, not the source of truth. A tiny count
drop with green status and good recall is usually normal window movement. A large
unexpected drop, a zero-count collection, vector mismatch, or failed smoke search
is a better reason to run the smallest targeted refresh.

## Quiet health checks and restart calibration

The watchdog script is designed for no-news-is-good-news scheduling:

```bash
python3 scripts/qdrant_recall_health_watchdog.py
QDRANT_WATCHDOG_VERBOSE=1 python3 scripts/qdrant_recall_health_watchdog.py
```

A healthy non-verbose run prints nothing. A failing run prints the missing or
unhealthy collections, observed point counts, and vector configuration.

When the failure is simply that a local Docker-backed Qdrant container is not
running yet, use `scripts/qdrant_bounded_start_restart.sh` before heavier repair.
The helper keeps the same quiet-on-success contract: it probes Qdrant over HTTP,
selects a usable Docker CLI, starts the configured container, and performs at
most one restart if health does not return. On WSL it can optionally launch
Docker Desktop through PowerShell when the standard Windows paths are present.

```bash
QDRANT_CONTAINER=qdrant-hermes scripts/qdrant_bounded_start_restart.sh
QDRANT_START_VERBOSE=1 QDRANT_CONTAINER=qdrant-hermes scripts/qdrant_bounded_start_restart.sh
```

Keep this helper focused on service bring-up. Collection rebuilds, re-ingest
jobs, and storage repairs should remain separate commands that you call only
after the watchdog output points to a data or configuration issue.

When Qdrant runs in Docker, a container restart can be a useful moment to
re-validate recall. `scripts/qdrant_restart_calibration.sh` runs the HTTP
watchdog first, then checks Docker `.State.StartedAt` with a bounded timeout so a
slow Docker CLI cannot turn a healthy Qdrant endpoint into a scheduler timeout.
It stores the last seen timestamp under
`${HERMES_HOME:-~/.hermes}/qdrant/state` and updates the marker only after a
healthy calibration.

```bash
QDRANT_CONTAINER=qdrant-hermes scripts/qdrant_restart_calibration.sh
```

Set `QDRANT_REPAIR_CMD` if you want to chain a local repair command after a
failed calibration. Keep repair commands environment-specific; the public helper
only detects, verifies, and delegates repair so it does not make assumptions
about your storage layout or corpus sources.

## Repair strategy

Prefer the smallest repair that matches the watchdog output:

- If the alert only says `MISSING <collection>`, rerun the ingest or refresh job
  for that one collection.
- If Qdrant is unreachable, a collection is not green, or vector size/distance is
  wrong, stop and inspect the server, storage, and embedding configuration before
  rebuilding multiple corpora.
- After any repair, run the watchdog in verbose mode once and then in quiet mode
  to confirm that healthy scheduled runs stay silent.

This keeps local recall maintenance fast and avoids turning a single stale
collection into an unnecessary full rebuild.
