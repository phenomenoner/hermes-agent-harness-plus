# Third-Party Pointers

Harness Plus is a companion toolbox. It does not vendor executable source from
the projects below.

- Hermes Agent: https://github.com/NousResearch/hermes-agent — MIT License.
  Local validation target: v0.14.0, commit `458a94e42`.
- MemPalace: https://github.com/MemPalace/mempalace — MIT License.
  Optional memory sidecar referenced by closeout docs and skills.
- Qdrant: https://github.com/qdrant/qdrant — Apache-2.0 License.
  Local validation target: server 1.17.1.
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk — MIT
  License. Used by optional MCP sidecars.
- FastEmbed: https://github.com/qdrant/fastembed — Apache-2.0 License. Used by
  optional Qdrant indexing/search scripts.
- Baton Fanout Skill: https://github.com/phenomenoner/baton-fanout-skill — MIT
  License. The optional delegation calibrator links to Baton for qualitative
  dispatch, ownership, and validation guidance; no Baton source is vendored.
- Context Canvas Codex skills:
  https://github.com/phenomenoner/Chatgpt-Codex-App-Plus/tree/cf43da0a06b2918b41bb386093f45c1d53eeb683/plugins/context-canvas-codex/skills
  — MIT License. Adapted into Hermes-native checkpoint boundaries and bounded
  reflection guidance; Codex-only hook, reference, and snapshot surfaces are
  excluded. The complete upstream notices are preserved in
  `third-party/context-canvas-codex-LICENSE.txt`.
