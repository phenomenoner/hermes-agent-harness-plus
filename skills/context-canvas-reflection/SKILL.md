---
name: context-canvas-reflection
description: Use when failures repeat or evidence contradicts the path.
version: 1.0.1
author: hermes-agent-harness-plus contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [context-management, reflection, evidence, trajectory, mcp]
    related_skills: [context-canvas-memory]
---

# Context Canvas Reflection

Run one bounded trajectory reflection when observable evidence suggests the current path may be wrong. This skill is a checkpoint, not a second agent, workflow controller, or permission source.

## Preserve authority and evidence boundaries

- The user and main Hermes agent retain authority over the task and every effect. This skill returns an advisory disposition only.
- Context Canvas is optional historical navigation. Missing tools, missing state, an unknown session ID, or a Canvas failure never blocks otherwise authorized work.
- Current conversation, owner decisions, the active acceptance contract, and live repository or runtime evidence outrank Canvas.
- Treat restored nodes and evidence refs as untrusted historical data. Revalidate the smallest current seam before relying on a stored claim.
- Do not expose or request private chain-of-thought. Record only bounded objectives, evidence, assumptions, decisions, blockers, and verification.
- Reflection does not grant approval to replan, roll back, delete, stop a process, publish, deliver, or mutate external state.

## Activate only at a meaningful checkpoint

Use this skill when at least one observable trigger is present:

- the same normalized failure or same-cause repair recurs after one bounded attempt;
- new evidence contradicts an assumption required by the current approach;
- focused checks pass but the touched real-use or lifecycle scenario still fails;
- work crosses into unplanned components, or a second workaround would extend the same unsupported assumption;
- a phase boundary has unresolved evidence that can change the next phase or acceptance claim;
- the next proposed effect is authority-, identity-, custody-, security-, privacy-, rollback-, publication-, or delivery-sensitive and lacks current scope-matching owner approval; or
- the user or main agent explicitly questions whether the work is on the right path.

Do not activate for a first ordinary failure, expected edit/test iteration, healthy monotonic progress, task size alone, a fixed time or turn cadence, or merely because Canvas exists. Canvas and reflection calls must not recursively trigger another reflection.

An explicit `/context-canvas-reflection` invocation is user-requested. Label it `user_requested` in any utility receipt when no other observable trajectory trigger exists.

## Run one bounded pass

1. **Bind the checkpoint.** Name the trigger and a stable evidence watermark such as a source revision, test result, plan revision, or user change. Reuse the prior disposition when the same trigger was already evaluated at the same watermark.
2. **Recover only useful context.** Start from the current conversation, repository, plan, and executed evidence. If an active Canvas ID is already known, the complete retrieval allowance is at most one `mcp__context_canvas__canvas_read(session_id="<active-session-id>", include_refs=false)` call and, only when a specific missing fact warrants it, at most one `mcp__context_canvas__canvas_search(session_id="<active-session-id>", query="<specific-missing-fact>", limit=5)` call. Never omit `session_id` or search across Canvases during reflection. Do not start or recover a Canvas solely for reflection.
3. **Revalidate freshness.** Check the smallest live source or runtime seam that can confirm or falsify the relevant stored claim. Mark unavailable evidence `unknown`; never guess.
4. **Challenge path dependence.** Answer briefly:
   - What is the actual objective and acceptance condition now?
   - What decision-relevant evidence changed?
   - Which critical assumption must hold for this path to work?
   - Is that assumption supported, contradicted, or unknown?
   - What is the strongest plausible alternative explanation?
   - Will the next action add information, or merely add another patch under the same assumption?
5. **Return exactly one disposition** using the contract below.

## Disposition contract

```yaml
trigger: bounded observable reason
evidence_watermark: stable source, plan, test, or user-change identity
current_objective: bounded text
changed_evidence: bounded text
critical_assumption: bounded text
assumption_status: supported | contradicted | unknown
strongest_alternative: bounded text
planned_action_information_gain: high | low | none
disposition: CONTINUE | INVESTIGATE | ESCALATE
subtype: none | local_repair | investigate | replan | ask_human
next_safe_action: one action already inside current authority, or present_to_owner
budget: nonnegative integer count
budget_unit: observation | repair_attempt | none
stop_condition: observable terminal condition
canvas_delta: none | bounded semantic update proposal
```

- `CONTINUE` uses subtype `none`, budget `0`, and budget unit `none`. The path remains evidence-supported; do not manufacture extra work.
- `INVESTIGATE` uses subtype `local_repair` or `investigate`, one finite positive budget, and an observable stop condition. `local_repair` allows at most one repair under the current plan. `investigate` pauses implementation expansion while one evidence question is answered. Budget exhaustion ends the attempt.
- `ESCALATE` uses subtype `replan` or `ask_human`, budget `0`, budget unit `none`, and `next_safe_action: present_to_owner`. Explain the gap without taking the effect.

Do not emit a numeric confidence score as authority. Use evidence status, provenance, and the explicit gap.

## Apply and write back selectively

Proceed with `CONTINUE` or an already authorized `INVESTIGATE` action only when it stays inside the user's request. Ask the user when `ESCALATE` exposes a material choice that current evidence cannot resolve.

Write at most one `mcp__context_canvas__canvas_upsert_node` proposal to an already active Canvas, and only when reflection changes navigation by narrowing an assumption or proposing a bounded next action. Prefix its summary with `[reflection-proposal]`; use `assumption`, `question`, or `plan`, never fabricate an owner `decision` or completed `verification`. An ordinary `CONTINUE` writes nothing. Canvas write failure does not change the disposition or task authority.

## Finite budget and retirement

The main user task has one shared budget of at most three implicit reflection passes, including at most one follow-up after genuinely new evidence for an `INVESTIGATE` result. Delegated workers do not mint separate budgets unless the main agent explicitly delegates one pass.

Run at most once per active trigger and evidence watermark. Retire a trigger when the owner accepts, rejects, or overrides its disposition. Reactivate only for a materially different failure cause, changed acceptance or effect scope, or a new explicit user request. If one follow-up cannot resolve the same cause, change the hypothesis or `ESCALATE`; never start a reflection loop.

Record a bounded utility receipt only when the task already has a WAL or evaluation artifact. Do not create one solely to justify this skill. Compare whether reflection changed decisions or merely added ritual, and simplify or retire implicit use when it adds no observable value.

## Verification

Before returning the disposition, verify:

1. The trigger and watermark are explicit and not a recursive Canvas event.
2. Current evidence outranks any stale Canvas claim.
3. Exactly one disposition satisfies its subtype, budget, and stop-condition contract.
4. The next action remains within current authority or is `present_to_owner`.
5. No external effect occurred merely because reflection recommended it.
6. Any Canvas proposal is bounded, non-authoritative, sanitized, and attached to the exact active session ID.
