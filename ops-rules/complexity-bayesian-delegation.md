# Complexity × Bayesian Delegation Calibrator

This optional calibrator helps a team learn when a bounded worker lane earns its coordination cost. It turns reviewed routing outcomes into a small, inspectable Beta posterior while keeping the routing contract deterministic and shareable.

It complements the [Baton Fanout Skill](https://github.com/phenomenoner/baton-fanout-skill). Baton remains the qualitative dispatch brake and the source of truth for ownership, validation, safety gates, and final integration. This calibrator never replaces Baton or the main agent's final judgment.

## What it decides

The CLI compares two route labels:

- `direct`: keep the work with the main agent;
- `luna_max`: use the policy-pinned maximum-effort worker lane when deterministic gates allow it.

The lane name is a fixed policy label. The calibrator does not tune model settings, permissions, worker authority, or approval boundaries. It may recommend one worker, or a bounded two-to-three-worker fan-out only when the task explicitly proves disjoint ownership and independent workstreams. The main agent remains the integration and verification owner.

## Score observable complexity

Record each dimension as an integer from `0` to `4`. Use the anchors and interpolate only when the task is genuinely between them.

| Dimension | 0 | 2 | 4 |
|---|---|---|---|
| `scope` | one tiny artifact | bounded multi-file surface | many heterogeneous surfaces |
| `coupling` | independent artifact | a few explicit dependencies | shared mutable contracts |
| `ambiguity` | exact contract | some interpretation | contradictory or unresolved goals |
| `consequence` | trivial and reversible | meaningful rework risk | authority, release, destructive, or costly error |
| `context_load` | one small source | several bounded sources | repeated large-context reading |
| `platform_specificity` | platform-neutral | one runtime boundary | native OS/toolchain behavior dominates |
| `repeatability` | one-off judgment | partially templated | mechanical recurring mapping |
| `verification_clarity` | no reliable oracle | review plus focused checks | deterministic independent proof |

The first six dimensions form complexity; the last two contribute to delegability:

```text
complexity = (scope + coupling + ambiguity + consequence
              + context_load + platform_specificity) / 24

delegability = (repeatability + verification_clarity
                 + (4 - coupling) + (4 - ambiguity)
                 + (4 - context_load)) / 20
```

High complexity does not imply high delegability. Coupling, ambiguity, and repeated context can make direct work the better route even when the task is large.

Each task also records non-scored routing facts: a stable contract, exclusive ownership, external side effects, secrets or private data, destructive behavior, final-judgment responsibility, the number of genuinely independent workstreams, and the estimated shared-context ratio.

## Deterministic routing floor

The calibrator applies governance rules before learned evidence:

1. External side effects, secrets or private data, destructive work, final judgment, or consequence `3–4` routes to `direct`.
2. An unstable contract or unresolved write ownership routes to `direct`; a low-consequence task may receive one read-only `luna_max` scout.
3. Delegability below `0.45` routes to `direct`.
4. A fan-out uses at most three workers and only uses more than one when ownership is exclusive, coupling is at most `1`, shared context is at most `0.25`, and there are at least two independent workstreams.
5. Final synthesis, validation, release judgment, and user-facing truth claims stay with the main agent.

These are policy gates, not learned parameters. A posterior cannot override them.

## Bayesian evidence

The route posterior uses qualified acceptance success with conservative priors:

```text
direct    ~ Beta(3, 2)
luna_max  ~ Beta(2, 2)
```

An outcome is a qualified success only when acceptance was met, an independent party verified the result, the output contract was complete, no safety violation or scope drift occurred, and quality was at least `3.5 / 5`. A timeout, lost result, or incomplete output is a failure; it is not silently omitted.

Only the declared `primary_attempt` updates the posterior. Retries and main-agent rescue work remain visible in the normalized record with `update_eligible=false`, so one difficult task cannot cast several correlated votes.

Observations are separated by both `policy_version` and a coarse `runtime_label`. Do not mix records across material policy or runtime changes. Sparse buckets back off once: exact bucket weight `1.0`, same task family `0.5`, route-global fallback `0.25`. The output reports `alpha`, `beta`, posterior mean, effective sample size, exact count, and an approximate conservative lower bound; `report` keeps multiple runtime labels in separate groups.

Time, quality, coordination, and cost are separate utility terms. For direct work, `coordination_minutes` must be `0`; ordinary direct effort is represented by elapsed time. Cost units are operator-defined stable units, not prices.

## Portable observation store

The CLI writes normalized JSONL records only. It does not persist raw prompts, transcript text, repository paths, identities, secrets, or chain-of-thought. Review an export before sharing it.

Store selection is:

1. an explicit `--observations` path;
2. `BATON_DELEGATION_STORE`, when set;
3. `$XDG_STATE_HOME/baton-fanout-skill/delegation-observations.jsonl`, when `XDG_STATE_HOME` is set;
4. the portable user-state fallback `~/.local/state/baton-fanout-skill/delegation-observations.jsonl`.

The default is outside any agent-specific runtime tree. The file is created with user-only permissions; a reviewed copy can be shared explicitly.

## CLI

Prepare a task JSON object using schema `baton.delegation-task.v1` and an outcome JSON object using the fields accepted by the script. Then run:

```bash
python scripts/delegation_bayes.py score --task task.json
python scripts/delegation_bayes.py recommend --task task.json
python scripts/delegation_bayes.py record --task task.json --outcome outcome.json
python scripts/delegation_bayes.py report
```

Use `--observations reviewed/observations.jsonl` for a deliberate export, or set `BATON_DELEGATION_STORE` for a user-state location. `record` prints the normalized observation it appends, making the result inspectable without storing the input documents.

A task's `runtime_label` should be a short, non-identifying label such as `runtime-a`; it is a calibration boundary, not a place to put provider names, account identifiers, or local paths. Keep `task_family` and tags equally coarse and lowercase.

## Review and stop rules

Review route data after a meaningful batch rather than reacting to one result. Return to Baton-only judgment when the task family is unobserved, evidence conflicts with the deterministic contract, the policy or runtime changes materially, two same-cause failures occur, reviewers disagree on quality, or recording the data costs more than the routing signal is worth.

The calibrator succeeds when it reduces unnecessary delegation and repeated steering without adding ceremony. If a hard blocker applies, the answer is direct work regardless of the posterior.
