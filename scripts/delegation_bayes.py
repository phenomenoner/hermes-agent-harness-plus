#!/usr/bin/env python3
"""Small stdlib CLI for Baton's direct-vs-Luna/max calibration."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_SCHEMA = "baton.delegation-task.v1"
OBSERVATION_SCHEMA = "baton.delegation-observation.v1"
POLICY_VERSION = "cbdr-1"
ROUTES = ("direct", "luna_max")
DIMENSIONS = (
    "scope",
    "coupling",
    "ambiguity",
    "consequence",
    "context_load",
    "platform_specificity",
    "repeatability",
    "verification_clarity",
)
COMPLEXITY_DIMENSIONS = DIMENSIONS[:6]
SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Keep the default outside any agent-specific runtime tree.  An operator may
# point at a reviewed, shareable JSONL export with BATON_DELEGATION_STORE.
DEFAULT_STORE = Path("~/.local/state/baton-fanout-skill/delegation-observations.jsonl").expanduser()
PRIORS = {"direct": (3.0, 2.0), "luna_max": (2.0, 2.0)}
NORMAL_80_Z = 1.2815515655446004


def default_store() -> Path:
    """Return the portable per-user observation store.

    Explicit ``BATON_DELEGATION_STORE`` wins.  Otherwise use the XDG state
    directory when available, with a conventional portable user-state fallback.
    """
    override = os.environ.get("BATON_DELEGATION_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "baton-fanout-skill" / "delegation-observations.jsonl"
    return DEFAULT_STORE


def resolve_store(explicit: Path | None) -> Path:
    """Resolve an explicit CLI path or the public default store."""
    return explicit.expanduser() if explicit is not None else default_store()


class ContractError(ValueError):
    """Raised when a task, outcome, or observation violates the compact contract."""


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain one JSON object")
    return value


def bounded_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise ContractError(f"{label} must be in [{low}, {high}]")
    return number


def required_bool(mapping: dict[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ContractError(f"{key} must be boolean")
    return value


def validate_task(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("schema") != TASK_SCHEMA:
        raise ContractError(f"task schema must be {TASK_SCHEMA}")
    family = raw.get("task_family")
    if not isinstance(family, str) or not SAFE_LABEL.fullmatch(family):
        raise ContractError("task_family must be a safe lowercase label")
    runtime_label = raw.get("runtime_label")
    if not isinstance(runtime_label, str) or not SAFE_LABEL.fullmatch(runtime_label):
        raise ContractError("runtime_label must be a safe lowercase label")
    raw_dimensions = raw.get("dimensions")
    if not isinstance(raw_dimensions, dict) or set(raw_dimensions) != set(DIMENSIONS):
        raise ContractError(f"dimensions must contain exactly: {', '.join(DIMENSIONS)}")
    dimensions = {
        name: int(bounded_number(raw_dimensions[name], f"dimensions.{name}", 0, 4))
        for name in DIMENSIONS
    }
    for name, original in raw_dimensions.items():
        if float(original) != dimensions[name]:
            raise ContractError(f"dimensions.{name} must be an integer")

    facts = {
        key: required_bool(raw, key)
        for key in (
            "stable_contract",
            "exclusive_ownership",
            "external_side_effects",
            "secrets_or_private_data",
            "destructive",
            "final_judgment",
        )
    }
    independent = int(
        bounded_number(raw.get("independent_workstreams"), "independent_workstreams", 1, 20)
    )
    if float(raw["independent_workstreams"]) != independent:
        raise ContractError("independent_workstreams must be an integer")
    shared_ratio = bounded_number(raw.get("shared_context_ratio"), "shared_context_ratio", 0, 1)
    estimated = bounded_number(
        raw.get("estimated_direct_minutes"), "estimated_direct_minutes", 0.1, 100000
    )
    return {
        "schema": TASK_SCHEMA,
        "policy_version": POLICY_VERSION,
        "runtime_label": runtime_label,
        "task_family": family,
        "dimensions": dimensions,
        **facts,
        "independent_workstreams": independent,
        "shared_context_ratio": shared_ratio,
        "estimated_direct_minutes": estimated,
    }


def score_task(task: dict[str, Any]) -> dict[str, Any]:
    dimensions = task["dimensions"]
    complexity = sum(dimensions[name] for name in COMPLEXITY_DIMENSIONS) / 24.0
    delegability = (
        dimensions["repeatability"]
        + dimensions["verification_clarity"]
        + (4 - dimensions["coupling"])
        + (4 - dimensions["ambiguity"])
        + (4 - dimensions["context_load"])
    ) / 20.0
    complexity_band = "low" if complexity < 0.34 else "medium" if complexity < 0.67 else "high"
    consequence = dimensions["consequence"]
    consequence_band = "low" if consequence <= 1 else "guarded" if consequence == 2 else "high"
    repeatability = dimensions["repeatability"]
    repeatability_band = "low" if repeatability <= 1 else "medium" if repeatability == 2 else "high"
    bucket = "|".join(
        (task["task_family"], complexity_band, consequence_band, repeatability_band)
    )
    return {
        "complexity": round(complexity, 6),
        "delegability": round(delegability, 6),
        "complexity_band": complexity_band,
        "consequence_band": consequence_band,
        "repeatability_band": repeatability_band,
        "bucket": bucket,
    }


def observation_weight(observation: dict[str, Any], task: dict[str, Any], bucket: str) -> float:
    if observation["bucket"] == bucket:
        return 1.0
    if observation["task_family"] == task["task_family"]:
        return 0.5
    return 0.25


def load_observations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    observations: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid observation JSON at line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict) or value.get("schema") != OBSERVATION_SCHEMA:
            raise ContractError(f"invalid observation schema at line {line_number}")
        if value.get("route") not in ROUTES:
            raise ContractError(f"invalid route at line {line_number}")
        if not isinstance(value.get("policy_version"), str) or not SAFE_LABEL.fullmatch(value["policy_version"]):
            raise ContractError(f"invalid policy_version at line {line_number}")
        if not isinstance(value.get("runtime_label"), str) or not SAFE_LABEL.fullmatch(value["runtime_label"]):
            raise ContractError(f"invalid runtime_label at line {line_number}")
        observations.append(value)
    return observations


def conservative_lower(alpha: float, beta: float) -> float:
    total = alpha + beta
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1.0))
    return max(0.0, min(1.0, mean - NORMAL_80_Z * math.sqrt(variance)))


def posterior(
    route: str,
    observations: list[dict[str, Any]],
    task: dict[str, Any],
    bucket: str,
) -> dict[str, Any]:
    alpha, beta = PRIORS[route]
    effective_n = 0.0
    exact_n = 0
    weighted: list[tuple[float, dict[str, Any]]] = []
    for observation in observations:
        if observation["route"] != route:
            continue
        if not observation.get("update_eligible", True):
            continue
        if observation.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            continue
        if observation.get("runtime_label") != task["runtime_label"]:
            continue
        weight = observation_weight(observation, task, bucket)
        success = bool(observation.get("qualified_success"))
        alpha += weight if success else 0.0
        beta += weight if not success else 0.0
        effective_n += weight
        if observation["bucket"] == bucket:
            exact_n += 1
        weighted.append((weight, observation))
    mean = alpha / (alpha + beta)
    return {
        "route": route,
        "alpha": round(alpha, 6),
        "beta": round(beta, 6),
        "mean": round(mean, 6),
        "lower_80_approx": round(conservative_lower(alpha, beta), 6),
        "effective_n": round(effective_n, 6),
        "exact_n": exact_n,
        "metrics": weighted_metrics(weighted),
    }


def weighted_metrics(weighted: list[tuple[float, dict[str, Any]]]) -> dict[str, Any] | None:
    total = sum(weight for weight, _ in weighted)
    if total <= 0:
        return None

    def average(key: str, transform=lambda value: value) -> float:
        numerator = sum(weight * transform(float(item[key])) for weight, item in weighted)
        return numerator / total

    quality = average("quality_5", lambda value: value / 5.0)
    # Ratios use each observation's own direct estimate or budget.
    time_ratio = sum(
        weight * float(item["elapsed_minutes"]) / float(item["estimated_direct_minutes"])
        for weight, item in weighted
    ) / total
    coord_ratio = sum(
        weight * float(item["coordination_minutes"]) / float(item["estimated_direct_minutes"])
        for weight, item in weighted
    ) / total
    cost_ratio = sum(
        weight * float(item["actual_cost_units"]) / float(item["budget_cost_units"])
        for weight, item in weighted
    ) / total
    return {
        "quality_norm": round(quality, 6),
        "time_ratio": round(time_ratio, 6),
        "coordination_ratio": round(coord_ratio, 6),
        "cost_ratio": round(cost_ratio, 6),
    }


def hard_gate_reasons(task: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if task["external_side_effects"]:
        reasons.append("external_side_effects")
    if task["secrets_or_private_data"]:
        reasons.append("secrets_or_private_data")
    if task["destructive"]:
        reasons.append("destructive")
    if task["final_judgment"]:
        reasons.append("final_judgment")
    if task["dimensions"]["consequence"] >= 3:
        reasons.append("high_consequence")
    return reasons


def expected_utility(post: dict[str, Any]) -> float | None:
    metrics = post["metrics"]
    if metrics is None:
        return None
    value = post["lower_80_approx"] * metrics["quality_norm"]
    utility = (
        value
        - 0.15 * metrics["time_ratio"]
        - 0.20 * metrics["coordination_ratio"]
        - 0.05 * metrics["cost_ratio"]
    )
    return round(utility, 6)


def worker_count(task: dict[str, Any], exploration: bool) -> int:
    if exploration:
        return 1
    dimensions = task["dimensions"]
    if (
        task["independent_workstreams"] >= 2
        and task["exclusive_ownership"]
        and dimensions["coupling"] <= 1
        and task["shared_context_ratio"] <= 0.25
    ):
        return min(3, task["independent_workstreams"])
    return 1


def recommend(task: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    scored = score_task(task)
    hard = hard_gate_reasons(task)
    direct_post = posterior("direct", observations, task, scored["bucket"])
    luna_post = posterior("luna_max", observations, task, scored["bucket"])
    evidence = {"direct": direct_post, "luna_max": luna_post}

    if hard:
        return decision("direct", 0, scored, evidence, ["hard_gate:" + ",".join(hard)])
    if not task["stable_contract"] or not task["exclusive_ownership"]:
        if task["dimensions"]["consequence"] <= 1:
            return decision(
                "luna_max_scout",
                1,
                scored,
                evidence,
                ["read_only_scout_only:contract_or_ownership_unresolved"],
            )
        return decision("direct", 0, scored, evidence, ["contract_or_ownership_unresolved"])
    if scored["delegability"] < 0.45:
        return decision("direct", 0, scored, evidence, ["delegability_below_0.45"])

    exploration = (
        task["dimensions"]["consequence"] <= 1
        and scored["delegability"] >= 0.65
        and luna_post["exact_n"] < 3
    )
    if exploration:
        return decision(
            "luna_max",
            1,
            scored,
            evidence,
            ["bounded_low_risk_exploration"],
            exploration=True,
        )

    direct_utility = expected_utility(direct_post)
    luna_utility = expected_utility(luna_post)
    utilities = {"direct": direct_utility, "luna_max": luna_utility}
    if direct_utility is None or luna_utility is None:
        return decision(
            "direct",
            0,
            scored,
            evidence,
            ["insufficient_comparative_utility_evidence"],
            utilities=utilities,
        )
    if luna_utility > direct_utility + 0.05:
        return decision(
            "luna_max",
            worker_count(task, False),
            scored,
            evidence,
            ["luna_utility_exceeds_direct_by_more_than_0.05"],
            utilities=utilities,
        )
    return decision(
        "direct",
        0,
        scored,
        evidence,
        ["delegation_has_not_earned_coordination_margin"],
        utilities=utilities,
    )


def decision(
    route: str,
    workers: int,
    scored: dict[str, Any],
    evidence: dict[str, Any],
    reasons: list[str],
    *,
    exploration: bool = False,
    utilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "baton.delegation-recommendation.v1",
        "route": route,
        "workers": workers,
        "luna_reasoning_effort": "max" if route.startswith("luna_max") else None,
        "exploration": exploration,
        "score": scored,
        "utilities": utilities,
        "posterior": evidence,
        "reasons": reasons,
        "integration_owner": "main_agent",
    }


def validate_outcome(raw: dict[str, Any]) -> dict[str, Any]:
    route = raw.get("route")
    if route not in ROUTES:
        raise ContractError(f"outcome route must be one of: {', '.join(ROUTES)}")
    booleans = {
        key: required_bool(raw, key)
        for key in (
            "acceptance_met",
            "independently_verified",
            "output_contract_complete",
            "safety_violation",
            "scope_drift",
            "primary_attempt",
        )
    }
    numbers = {
        "quality_5": bounded_number(raw.get("quality_5"), "quality_5", 1, 5),
        "elapsed_minutes": bounded_number(raw.get("elapsed_minutes"), "elapsed_minutes", 0, 100000),
        "coordination_minutes": bounded_number(
            raw.get("coordination_minutes"), "coordination_minutes", 0, 100000
        ),
        "rework_minutes": bounded_number(raw.get("rework_minutes"), "rework_minutes", 0, 100000),
        "actual_cost_units": bounded_number(raw.get("actual_cost_units"), "actual_cost_units", 0.000001, 1e12),
        "budget_cost_units": bounded_number(raw.get("budget_cost_units"), "budget_cost_units", 0.000001, 1e12),
    }
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 12:
        raise ContractError("tags must be a list of at most 12 safe labels")
    if any(not isinstance(tag, str) or not SAFE_LABEL.fullmatch(tag) for tag in tags):
        raise ContractError("every tag must be a safe lowercase label")
    if route == "direct" and numbers["coordination_minutes"] != 0:
        raise ContractError("direct route coordination_minutes must be 0")
    return {"route": route, **booleans, **numbers, "tags": tags}


def task_fingerprint(task: dict[str, Any]) -> str:
    canonical = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def build_observation(task: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    scored = score_task(task)
    qualified = (
        outcome["acceptance_met"]
        and outcome["independently_verified"]
        and outcome["output_contract_complete"]
        and not outcome["safety_violation"]
        and not outcome["scope_drift"]
        and outcome["quality_5"] >= 3.5
    )
    return {
        "schema": OBSERVATION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "runtime_label": task["runtime_label"],
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "task_fingerprint": task_fingerprint(task),
        "route": outcome["route"],
        "task_family": task["task_family"],
        "bucket": scored["bucket"],
        "dimensions": task["dimensions"],
        "primary_attempt": outcome["primary_attempt"],
        "update_eligible": outcome["primary_attempt"],
        "qualified_success": qualified,
        "acceptance_met": outcome["acceptance_met"],
        "independently_verified": outcome["independently_verified"],
        "output_contract_complete": outcome["output_contract_complete"],
        "safety_violation": outcome["safety_violation"],
        "scope_drift": outcome["scope_drift"],
        "quality_5": outcome["quality_5"],
        "elapsed_minutes": outcome["elapsed_minutes"],
        "estimated_direct_minutes": task["estimated_direct_minutes"],
        "coordination_minutes": outcome["coordination_minutes"],
        "rework_minutes": outcome["rework_minutes"],
        "actual_cost_units": outcome["actual_cost_units"],
        "budget_cost_units": outcome["budget_cost_units"],
        "tags": outcome["tags"],
    }


def append_observation(path: Path, observation: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(observation, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def route_report(observations: list[dict[str, Any]]) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route in ROUTES:
        matching = [item for item in observations if item["route"] == route]
        eligible = [
            item
            for item in matching
            if item.get("update_eligible", True)
            and item.get("policy_version", POLICY_VERSION) == POLICY_VERSION
        ]
        successes = sum(bool(item.get("qualified_success")) for item in eligible)
        routes[route] = {
            "count": len(matching),
            "update_eligible_count": len(eligible),
            "qualified_successes": successes,
            "qualified_success_rate": round(successes / len(eligible), 6) if eligible else None,
            "mean_quality_5": round(
                sum(float(item["quality_5"]) for item in eligible) / len(eligible), 6
            ) if eligible else None,
            "mean_time_ratio": round(
                sum(float(item["elapsed_minutes"]) / float(item["estimated_direct_minutes"]) for item in eligible)
                / len(eligible),
                6,
            ) if eligible else None,
            "mean_coordination_ratio": round(
                sum(float(item["coordination_minutes"]) / float(item["estimated_direct_minutes"]) for item in eligible)
                / len(eligible),
                6,
            ) if eligible else None,
        }
    return routes


def global_report(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize current-policy results without mixing runtime labels."""
    by_runtime: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        by_runtime.setdefault(observation["runtime_label"], []).append(observation)
    runtimes = {
        runtime_label: {
            "observations": len(runtime_observations),
            "routes": route_report(runtime_observations),
        }
        for runtime_label, runtime_observations in sorted(by_runtime.items())
    }
    report: dict[str, Any] = {
        "schema": "baton.delegation-report.v1",
        "policy_version": POLICY_VERSION,
        "observations": len(observations),
        "runtimes": runtimes,
    }
    if len(runtimes) == 1:
        runtime_label, runtime_report = next(iter(runtimes.items()))
        report["runtime_label"] = runtime_label
        report["routes"] = runtime_report["routes"]
    elif not runtimes:
        report["runtime_label"] = None
        report["routes"] = route_report([])
    else:
        report["runtime_label"] = None
        report["routes"] = None
    return report


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    score = subparsers.add_parser("score", help="score one task JSON")
    score.add_argument("--task", type=Path, required=True)

    recommendation = subparsers.add_parser("recommend", help="recommend direct or Luna/max")
    recommendation.add_argument("--task", type=Path, required=True)
    recommendation.add_argument(
        "--observations",
        type=Path,
        default=None,
        help="JSONL store (defaults to BATON_DELEGATION_STORE, then XDG user state)",
    )

    record = subparsers.add_parser("record", help="append one verified outcome")
    record.add_argument("--task", type=Path, required=True)
    record.add_argument("--outcome", type=Path, required=True)
    record.add_argument(
        "--observations",
        type=Path,
        default=None,
        help="JSONL store (defaults to BATON_DELEGATION_STORE, then XDG user state)",
    )

    report = subparsers.add_parser("report", help="summarize the observation store")
    report.add_argument(
        "--observations",
        type=Path,
        default=None,
        help="JSONL store (defaults to BATON_DELEGATION_STORE, then XDG user state)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "score":
            emit(score_task(validate_task(read_json(args.task))))
        elif args.command == "recommend":
            task = validate_task(read_json(args.task))
            emit(recommend(task, load_observations(resolve_store(args.observations))))
        elif args.command == "record":
            task = validate_task(read_json(args.task))
            outcome = validate_outcome(read_json(args.outcome))
            observation = build_observation(task, outcome)
            store = resolve_store(args.observations)
            append_observation(store, observation)
            emit(observation)
        elif args.command == "report":
            emit(global_report(load_observations(resolve_store(args.observations))))
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
