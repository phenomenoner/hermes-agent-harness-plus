# Share-Ready Artifact Handoff Checklist

Use this checklist when a Hermes Agent workflow creates files that another person
or channel should open later: images, reports, generated docs, logs, exports, or
small demo bundles.

The goal is simple: hand off the useful artifact, not the private runtime that
produced it.

## 1. Decide what should be shared

- Share the finished artifact or a purpose-built preview.
- Keep raw runtime logs, hidden working directories, local databases, and failed
  intermediate attempts out of the handoff.
- Prefer a short manifest when there are multiple files: what each file is, how
  it was created, and what was checked.

## 2. Make a portable delivery copy

Before publishing or handing off a file, copy it into an intentional output
location for the project or release.

Good patterns:

```text
artifacts/<run-name>/final-report.md
artifacts/<run-name>/preview.jpg
public/assets/<asset-name>.png
```

Avoid exposing machine-specific absolute paths in public docs, captions, or
release notes. If a local path is needed for an operator-only note, keep it in a
private run log rather than the public artifact.

## 3. Create a reader-friendly derivative

Large originals are useful for archiving, but a smaller derivative is often
better for sharing.

- For images: create a web-friendly PNG or JPEG preview and record the final
  dimensions.
- For long reports: provide a concise summary plus a link or path to the full
  artifact.
- For generated bundles: include a manifest with filenames, sizes, and expected
  checks.

## 4. Verify before linking

Check the actual handoff target, not only the source file.

- Confirm the file exists at the shared location.
- Open or render the preview once.
- Confirm any links use repo-relative paths or public URLs.
- If a delivery tool reports success but the item is not visible immediately,
  re-check the target state before creating duplicate artifacts.

## 5. Scan the public surface

Before committing or publishing, scan public files for:

- secrets, tokens, cookies, keys, and credentials;
- account numbers, private datasets, or raw personal data;
- machine-specific absolute paths;
- internal-only notes, failed prompt attempts, or private project names;
- screenshots that reveal hidden tabs, file paths, or account state.

## 6. Record the minimum useful context

A good public handoff note says:

```text
Generated: <date or version>
Files: <short list>
Checks: dimensions/render/link/privacy scan
How to regenerate: <command or doc pointer>
```

Do not include raw session excerpts or private troubleshooting logs unless the
repository is explicitly meant to publish those logs.

## 7. Verify archived notes and search copies separately

A durable note can have an original file, an archive copy, and a searchable
index. Verify each layer independently so a search miss does not make a saved
note look lost, and an accepted write does not become an unearned success claim.

### Preserve one final generation

- Finalize the note and calculate its content hash before archiving it.
- File that exact text; keep later verification receipts outside the note so
  they do not change the generation being checked.
- Retain the write response's logical note identifier and every returned
  physical chunk identifier. Read those exact chunks, not simply the latest
  entry, which another writer may have created.
- Reconcile unique chunk identifiers, order, parent identity, and source metadata
  against the response. Reconstruct the actual returned text using the backend's
  documented chunking rules and compare it with the finalized note. Contiguous
  slices may split inside a word: concatenate them without invented separators.
  Account for documented overlap or normalization explicitly.
- Build the receipt from actual readback data, not expected chunks generated
  from the source. Changed note bytes require a new verified archive generation.

### Check storage before judging recall

- Keep the original note authoritative; an index is a derived search copy.
- For an authorized index write, bind each point to the exact source, full-note
  content hash, and archive provenance. Read the exact returned point identifiers
  and verify their stored text and metadata before claiming storage success.
- Separately run a bounded semantic query using distinctive title or topic
  terms. Confirm the hit belongs to the intended source and generation, not just
  a similarly titled older note. Correct payload metadata alone is not recall
  evidence; a hit need not rank first to be useful.

### Keep optional indexing bounded

If the primary archive verifies but an optional index or recall check does not,
report those verdicts separately. Preserve one continuation record with the
note's source, content hash, archive provenance, target collection, observed
blocker, and remaining acceptance check. Keep private handles in local receipts.

Finish the archive handoff without repeatedly upserting an already verified
payload, restarting a healthy service, rebuilding a collection, or creating a
new maintenance job. Broader repair needs its own evidence and authorization.
