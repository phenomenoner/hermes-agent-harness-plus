# Public Release Checklist

Use this before moving a local harness component into a public repository.

## 1. Classify the component

- Generic adapter, plugin, sidecar, skill, or ops rule: candidate for this repo.
- Private channel, private identity/persona, private memory, local-only project,
  or production operation: do not publish here.

## 2. Stage from a clean directory

Do not publish directly from a private runtime tree. Copy only intended files
into a clean staging repository, add a defensive `.gitignore`, and scan before
`git init` or first commit.

## 3. Replace local assumptions

- Use placeholders for paths.
- Use environment variables for ports, collection names, and storage roots.
- Provide dry-run commands for anything that reads private data.

## 4. Review license obligations

- If the code is original, MIT is fine for this repo.
- If code was copied or adapted from another project, record the source and
  license in `NOTICE` and keep any required notices.
- If a component only imports or calls a third-party project, provide a pointer
  and version note instead of copying its source.

## 5. Human docs first

A reader should understand the value in 30 seconds. Put the story in the README
and website. Put deep details in `docs/technical/`.
