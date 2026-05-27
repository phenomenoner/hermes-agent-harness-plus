# Contributing

Thank you for helping make this harness useful to more Hermes Agent users.

## Share-ready contribution rule

Before opening a pull request, check that the change is useful outside your own
machine or organization. Do not include:

- credentials, API keys, tokens, cookies, or session files;
- raw chat logs, memory exports, or runtime databases;
- account-specific integration details or bot credentials;
- local-only project names, datasets, broker files, or research artifacts;
- absolute machine paths that only work for one operator.

If a component needs local configuration, provide a template and document the
placeholders instead.

## Pull request checklist

- [ ] The README/docs explain why a human would want the change.
- [ ] Install steps are copy-pasteable with placeholders where needed.
- [ ] `python -m pytest -q` passes, or the PR explains why tests are not relevant.
- [ ] New third-party dependencies are recorded in `NOTICE` with license pointers.
- [ ] A share-ready content scan has been run against the files being published.
