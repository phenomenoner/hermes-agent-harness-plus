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
