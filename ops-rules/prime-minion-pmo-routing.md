# Prime Minion PMO Routing

Use this standing rule only when a PMO-style orchestration stance is active and the host exposes the named routes. It defines routing and authority; it does not grant permission to delegate, modify a workspace, publish, or perform a live effect.

## Authority

- The main Hermes agent is the PM and integrator. It coordinates work, reads the live target, resolves conflicts, runs shared verification, and owns the final claim.
- The operator selects or changes the main-agent model.
- Minions and subagents are bounded execution or review substrates. Their completion is evidence, never acceptance or publication authority by itself.

## Dispatch gate

- Route every meaningful fan-out through the installed dispatch/admission policy, such as Baton, before choosing a worker.
- Delegation does not expand scope, writable ownership, credential authority, or release authority.
- If the required dispatch gate is unavailable, work directly when independence is not required; otherwise stop `INCOMPLETE` rather than inventing an unverified bridge.

## Execution pool

When the host exposes a verified `delegate_minion` tool:

- the `Luna/max` execution lane defaults to a Prime Agent minion for stable bounded implementation, exact-path generation, or low-judgment scouting with cheap falsifiers;
- every invocation declares `provider`, `model`, and `reasoning_effort` explicitly;
- accept the minion result only when the returned effective route matches the requested route; and
- keep final integration and shared verification in the main agent.

Do not route architecture, security, authority, independent review, release judgment, rollback judgment, or irreversible cutover decisions to Luna.

## Review pool

- A `Terra/high` review lane may use either a native Hermes subagent or a Prime Agent minion according to task complexity, context-isolation needs, and the lowest-cost route that preserves the required independence.
- Required independent review uses a distinct reviewer identity and isolated frozen inputs. A renamed main-agent self-review does not satisfy independence.
- Review is read-only unless a separate implementation task is admitted after findings are verified against the live candidate.

## Fallback

- If the verified minion route is absent, unsupported, or fails before useful execution, the dispatch gate may choose an eligible native subagent or direct main-agent execution and records the deviation.
- If the unavailable route was required independent review, select another distinct eligible reviewer or stop `INCOMPLETE`.
- Never silently change provider/model/effort, fabricate a model bridge, expose provider credentials to a worker, or treat a lower route as the requested route.

## Verification boundary

Before the main agent claims completion:

1. read the exact live target or candidate bytes;
2. reconcile worker/reviewer findings;
3. run the smallest shared verification that can falsify the changed claim; and
4. keep external publication, release, and final messaging under main-agent authority.
