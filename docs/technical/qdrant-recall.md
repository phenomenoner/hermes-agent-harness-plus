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

## Privacy posture

The session indexer:

- indexes only user and assistant text by default;
- skips system prompts and tool outputs unless explicitly enabled;
- truncates long messages;
- redacts common secret-looking patterns;
- supports `--dry-run` so you can preview before writing to Qdrant.

Local recall is useful, but it is still an index of your text. Read the dry-run
output before indexing.
