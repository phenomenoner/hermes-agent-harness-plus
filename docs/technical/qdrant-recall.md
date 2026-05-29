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

## Quiet health checks and restart calibration

The watchdog script is designed for no-news-is-good-news scheduling:

```bash
python3 scripts/qdrant_recall_health_watchdog.py
QDRANT_WATCHDOG_VERBOSE=1 python3 scripts/qdrant_recall_health_watchdog.py
```

A healthy non-verbose run prints nothing. A failing run prints the missing or
unhealthy collections, observed point counts, and vector configuration.

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
