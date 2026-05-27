# Public-Safety Checklist

Run this checklist before publishing any new adapter, plugin, script, skill, or
documentation page.

## Content boundaries

- [ ] The content is useful to another Hermes Agent user outside your own setup.
- [ ] No private agent identity, persona, relationship map, or private channel is
      present.
- [ ] No local-only project names, datasets, broker files, or private research
      artifacts are present.
- [ ] No credentials, tokens, cookies, session files, or production config are
      present.
- [ ] Absolute paths are replaced with placeholders such as
      `/absolute/path/to/repo` or `/home/you/.hermes/...`.
- [ ] Third-party projects are referenced with source URL, version when known,
      and license pointer.

## Technical boundaries

- [ ] Scripts have safe defaults and support dry-run when they read private data.
- [ ] Indexers skip or redact high-risk fields by default.
- [ ] Plugins are opt-in and fail open if their sidecar is unavailable.
- [ ] Docs explain how to disable or remove the component.

## Human readability

- [ ] The README explains the purpose in about 30 seconds.
- [ ] The website starts with the human story before technical details.
- [ ] Deep technical details live in `docs/technical/`, not the opening pitch.
